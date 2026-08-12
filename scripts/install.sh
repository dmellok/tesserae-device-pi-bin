#!/usr/bin/env bash
# One-shot installer for tesserae-pi-bin-client on a fresh Raspberry Pi OS.
#
# Run this AS YOUR NORMAL USER (NOT root, not via sudo). The script calls
# sudo internally for the privileged bits (apt, raspi-config, usermod, ln,
# systemd unit install) and runs the unprivileged bits (venv, pip install)
# as the invoking user — so the venv ends up in that user's home and the
# service runs as them too.
#
# What it does, idempotently:
#   1. apt-get install build + runtime prerequisites
#   2. raspi-config do_spi 0 + do_i2c 0, and dtoverlay=spi0-0cs in config.txt
#      (SPI bus for pixel data, I2C for the HAT EEPROM auto-detect reads, and
#       freeing GPIO7/8 so inky can drive chip-select in software)
#   3. usermod -aG gpio,spi,i2c $USER (group membership for HAT access;
#                                      i2c is needed for the EEPROM read)
#   4. python3 -m venv .venv          (in the repo directory)
#   5. .venv/bin/pip install -e .     (project + inky[rpi])
#   6. interactive prompt → write ~/.config/.../config.toml
#      (skipped if the file already exists, unless --reconfigure)
#   7. ln -sf .venv/bin/tesserae-pi-bin-client /usr/local/bin/...
#   8. scripts/install-service.sh $USER          (systemd unit + enable + start,
#                                                  unless --no-service)
#
# The group change in step 3 only takes effect on the user's next login.
# If you've just been added to gpio/spi/i2c, log out + back in (or reboot)
# before running --paint-test or relying on the service.

set -euo pipefail

INSTALL_SERVICE=true
RUN_PAINT_TEST=false
SKIP_APT=false
NON_INTERACTIVE=false
RECONFIGURE=false
SERVICE_USER="${USER:-$(id -un)}"

usage() {
    cat <<USAGE
usage: $0 [options]

  --no-service        don't install the systemd unit
  --paint-test        run --paint-test after install (needs fresh login if
                      the gpio/spi/i2c groups were just added)
  --skip-apt          skip apt-get update + install
  --non-interactive   never prompt — write a default config if none exists
  --reconfigure       prompt for MQTT/panel values even if a config exists
                      (will overwrite the existing file)
  --user USER         user the systemd unit runs as (default: \$USER)
  -h, --help          show this message
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-service) INSTALL_SERVICE=false; shift ;;
        --paint-test) RUN_PAINT_TEST=true; shift ;;
        --skip-apt) SKIP_APT=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --reconfigure) RECONFIGURE=true; shift ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
BIN_LINK="/usr/local/bin/tesserae-pi-bin-client"
CONFIG_PATH="${HOME}/.config/tesserae-pi-bin-client/config.toml"

if [[ "$(id -u)" -eq 0 ]]; then
    echo "error: run as your normal user, NOT root or via sudo." >&2
    echo "       (the script invokes sudo internally where needed)" >&2
    exit 1
fi

if [[ ! -f "${REPO_DIR}/pyproject.toml" ]]; then
    echo "error: ${REPO_DIR}/pyproject.toml not found — wrong repo layout?" >&2
    exit 1
fi

is_rpi=false
if [[ -f /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    is_rpi=true
fi
if ! $is_rpi; then
    echo "warning: this doesn't look like a Raspberry Pi — continuing anyway."
    echo "         (SPI enable + group add will be no-ops on a normal Linux box.)"
fi

# ----- prompt helpers (only fire when stdin is a TTY and we're interactive) -----
prompt_default() {
    # $1=var name to assign, $2=question, $3=default
    local __var="$1" __q="$2" __default="$3" __input
    if [[ -n "$__default" ]]; then
        read -r -p "${__q} [${__default}]: " __input
    else
        read -r -p "${__q}: " __input
    fi
    printf -v "$__var" '%s' "${__input:-$__default}"
}

prompt_secret() {
    # $1=var name to assign, $2=question
    local __var="$1" __q="$2" __input
    read -r -s -p "${__q} (input hidden; press Enter for none): " __input
    echo
    printf -v "$__var" '%s' "$__input"
}

prompt_choice() {
    # $1=var name, $2=question, $3=default index (1-based), $4..=choices
    local __var="$1" __q="$2" __default_idx="$3"
    shift 3
    local __choices=("$@") __i __input
    echo "${__q}"
    for __i in "${!__choices[@]}"; do
        local n=$((__i + 1))
        if [[ "$n" == "$__default_idx" ]]; then
            echo "    ${n}) ${__choices[$__i]}  [default]"
        else
            echo "    ${n}) ${__choices[$__i]}"
        fi
    done
    read -r -p "choice [press Enter for default]: " __input
    if [[ -z "$__input" ]]; then
        __input="$__default_idx"
    fi
    if ! [[ "$__input" =~ ^[0-9]+$ ]] || \
        (( __input < 1 || __input > ${#__choices[@]} )); then
        echo "    invalid choice; using default" >&2
        __input="$__default_idx"
    fi
    printf -v "$__var" '%s' "${__choices[$((__input - 1))]}"
}

echo "==> caching sudo credentials"
sudo -v

# ----- 1. apt -----
if $SKIP_APT; then
    echo "==> skipping apt (--skip-apt)"
else
    echo "==> apt-get update + install"
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-venv \
        python3-pip \
        python3-dev \
        build-essential \
        libopenjp2-7 \
        libtiff6
fi

# ----- 2. SPI + I2C + chip-select overlay -----
needs_reboot=false
# SPI carries pixel data to the panel; I2C is how inky.auto() reads the HAT
# ID EEPROM to identify the board (without it: "No EEPROM detected! You must
# manually initialise your Inky board"). The spi0-0cs overlay tells the kernel
# SPI driver to claim ZERO hardware chip-select pins, freeing GPIO7/8 so inky
# can drive CS in software — without it inky aborts at paint time with
# "Chip Select: (line 8, GPIO8) currently claimed by spi0 CS0".
if $is_rpi; then
    if command -v raspi-config >/dev/null 2>&1; then
        # get_spi/get_i2c echo 1 when disabled, 0 when enabled — only flip
        # (and flag a reboot) when actually needed so re-runs stay quiet.
        if [[ "$(sudo raspi-config nonint get_spi 2>/dev/null || echo 1)" == "1" ]]; then
            echo "==> enabling SPI via raspi-config"
            sudo raspi-config nonint do_spi 0
            needs_reboot=true
        else
            echo "==> SPI already enabled"
        fi
        if [[ "$(sudo raspi-config nonint get_i2c 2>/dev/null || echo 1)" == "1" ]]; then
            echo "==> enabling I2C via raspi-config (needed for HAT EEPROM detection)"
            sudo raspi-config nonint do_i2c 0
            needs_reboot=true
        else
            echo "==> I2C already enabled"
        fi
    else
        echo "==> raspi-config not found; enable SPI + I2C yourself"
    fi
    boot_config=/boot/firmware/config.txt
    [[ -f "$boot_config" ]] || boot_config=/boot/config.txt
    if [[ -f "$boot_config" ]]; then
        if grep -q '^dtoverlay=spi0-0cs' "$boot_config"; then
            echo "==> spi0-0cs overlay already present in $boot_config"
        else
            echo "==> adding 'dtoverlay=spi0-0cs' to $boot_config (frees CS pin for inky)"
            echo 'dtoverlay=spi0-0cs' | sudo tee -a "$boot_config" >/dev/null
            needs_reboot=true
        fi
    else
        echo "==> no boot config.txt found; add 'dtoverlay=spi0-0cs' yourself"
    fi
else
    echo "==> skipping SPI/I2C/overlay setup (not on a Pi)"
fi

# ----- 3. groups -----
needs_relogin=false
for group in gpio spi i2c; do
    if ! getent group "$group" >/dev/null 2>&1; then
        echo "==> group $group does not exist on this system; skipping"
        continue
    fi
    if id -nG "$USER" | tr ' ' '\n' | grep -qx "$group"; then
        echo "==> $USER already in $group"
    else
        echo "==> adding $USER to $group"
        sudo usermod -aG "$group" "$USER"
        needs_relogin=true
    fi
done

# ----- 4-5. venv + pip install -----
if [[ ! -d "$VENV_DIR" ]]; then
    echo "==> creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
echo "==> upgrading pip in venv"
"$VENV_DIR/bin/pip" install -q --upgrade pip
echo "==> installing package (this pulls inky[rpi] and can take a few minutes)"
"$VENV_DIR/bin/pip" install -e "$REPO_DIR"

# ----- 6. config -----
collect_config_via_prompts() {
    echo
    echo "==> Tesserae client configuration"
    echo "    Press Enter at any prompt to accept the default in brackets."
    echo
    echo "    Two transports are available:"
    echo "      rest  poll the Tesserae server's REST API directly"
    echo "            (default — simpler; just needs the server URL)"
    echo "      mqtt  subscribe to an MQTT broker"
    echo "            (existing setups — push-driven; needs a broker on the LAN)"
    prompt_choice transport_mode "Transport" 1 \
        "rest (poll the Tesserae server directly)" \
        "mqtt (subscribe to a broker)"
    # Strip the trailing "(...)" — keep just the mode id.
    transport_mode="${transport_mode%% *}"
    echo

    echo "    A device id identifies this Pi to the Tesserae server."
    echo "    The default 'pi_bin' matches the server's pi_bin_client kind;"
    echo "    pick something like 'pi_bin_kitchen' if you're running more"
    echo "    than one .bin Pi against the same server."
    prompt_default device_id       "Device id"          "pi_bin"
    # Client-side sanity check; the parser also enforces this regex.
    if ! [[ "$device_id" =~ ^[a-z][a-z0-9_-]{1,31}$ ]]; then
        echo "    invalid device id; falling back to 'pi_bin'" >&2
        device_id="pi_bin"
    fi

    if [[ "$transport_mode" == "rest" ]]; then
        echo
        echo "    REST mode — point at the Tesserae server's base URL."
        prompt_default rest_server_url "Tesserae server URL" \
            "http://tesserae.local:8765"
        echo
        echo "    A pairing code is OPTIONAL. Leave it blank and the daemon will"
        echo "    appear in Settings -> Devices for one-click Register on first"
        echo "    boot. Set one only if your admin requires explicit per-device"
        echo "    approval before any instance is created server-side."
        prompt_default rest_pairing_code \
            "Pairing code (optional, blank to skip)" ""
        # MQTT-broker fields stay empty; render_config_toml fills their defaults
        # but they're inert when transport_mode = "rest".
        mqtt_host=""
        mqtt_port=""
        mqtt_username=""
        mqtt_password=""
        mqtt_client_id=""
    else
        echo
        prompt_default mqtt_host       "MQTT broker host"   "192.168.1.10"
        prompt_default mqtt_port       "MQTT broker port"   "1883"
        prompt_default mqtt_username   "MQTT username (blank for anonymous)" ""
        if [[ -n "$mqtt_username" ]]; then
            prompt_secret mqtt_password "MQTT password"
        else
            mqtt_password=""
        fi
        prompt_default mqtt_client_id  "MQTT client id"     "pi-impression-1"
        rest_server_url=""
        rest_pairing_code=""
    fi

    echo
    prompt_choice  panel_model     "Panel model" 4 \
        "inky_4 (640x400)" "inky_5_7 (600x448)" "inky_7_3 (800x480)" "inky_13_3 (1600x1200)"
    # Strip the "(WxH)" suffix from the choice — keep just the model id.
    panel_model="${panel_model%% *}"
    echo

    echo "    The Impression's four front buttons can drive the panel:"
    echo "      A = previous page, B = refresh, C = next page."
    echo "    Enable only if your HAT has them wired and libgpiod is present;"
    echo "    frames paint the same either way."
    prompt_default buttons_enabled "Enable front buttons? (y/N)" "n"
    case "${buttons_enabled,,}" in
        y|yes) buttons_enabled="1" ;;
        *)     buttons_enabled="" ;;
    esac
    echo
}

write_config() {
    # $1 = "1" to overwrite an existing file
    env \
        T_CONFIG_PATH="$CONFIG_PATH" \
        T_TRANSPORT_MODE="${transport_mode:-}" \
        T_REST_SERVER_URL="${rest_server_url:-}" \
        T_REST_PAIRING_CODE="${rest_pairing_code:-}" \
        T_MQTT_HOST="${mqtt_host:-}" \
        T_MQTT_PORT="${mqtt_port:-}" \
        T_MQTT_USERNAME="${mqtt_username:-}" \
        T_MQTT_PASSWORD="${mqtt_password:-}" \
        T_MQTT_CLIENT_ID="${mqtt_client_id:-}" \
        T_DEVICE_ID="${device_id:-}" \
        T_PANEL_MODEL="${panel_model:-}" \
        T_BUTTONS_ENABLED="${buttons_enabled:-}" \
        T_OVERWRITE="$1" \
        "$VENV_DIR/bin/python" -m tesserae_pi_bin_client.bootstrap_config
}

config_existed_before=false
if [[ -f "$CONFIG_PATH" ]]; then
    config_existed_before=true
fi

if $config_existed_before && ! $RECONFIGURE; then
    echo "==> config already exists at $CONFIG_PATH — leaving it alone"
    echo "    (re-run with --reconfigure to overwrite)"
elif $NON_INTERACTIVE; then
    echo "==> writing default config (--non-interactive)"
    write_config "$($RECONFIGURE && echo 1 || echo 0)" >/dev/null
    echo "    wrote $CONFIG_PATH"
elif [[ ! -t 0 ]]; then
    echo "==> stdin is not a TTY — writing default config without prompting"
    write_config "$($RECONFIGURE && echo 1 || echo 0)" >/dev/null
    echo "    wrote $CONFIG_PATH"
else
    collect_config_via_prompts
    write_config "$($RECONFIGURE && echo 1 || echo 0)" >/dev/null
    echo "==> wrote $CONFIG_PATH"
fi

# ----- 7. /usr/local/bin symlink -----
echo "==> linking $BIN_LINK -> $VENV_DIR/bin/tesserae-pi-bin-client"
sudo ln -sf "$VENV_DIR/bin/tesserae-pi-bin-client" "$BIN_LINK"

# ----- 8. systemd unit (optional) -----
if $INSTALL_SERVICE; then
    echo "==> installing systemd unit (user=$SERVICE_USER)"
    sudo "$REPO_DIR/scripts/install-service.sh" "$SERVICE_USER"
else
    echo "==> skipping systemd unit install (--no-service)"
fi

# ----- 9. optional paint test -----
if $RUN_PAINT_TEST; then
    if $needs_relogin || $needs_reboot; then
        echo "==> NOT running --paint-test — SPI/I2C and/or gpio/spi/i2c group"
        echo "    membership were just changed and won't take effect until you"
        echo "    reboot (or at least log out + back in)."
    else
        echo "==> running --paint-test"
        "$VENV_DIR/bin/tesserae-pi-bin-client" --paint-test || \
            echo "    (paint-test failed — see logs above)"
    fi
fi

echo
echo "================================================================"
echo "  install complete"
echo "================================================================"
echo
echo "  config:  $CONFIG_PATH"
if $config_existed_before && ! $RECONFIGURE; then
    echo "           (existing file kept; re-run with --reconfigure to change)"
fi
echo
if $needs_reboot; then
    echo "  REBOOT:  SPI and/or I2C were just enabled — REBOOT NOW so the panel"
    echo "           can be auto-detected:  sudo reboot"
    echo "           (the service is enabled and will start cleanly after boot;"
    echo "           before rebooting it fails with 'No EEPROM detected'.)"
    echo
elif $needs_relogin; then
    echo "  groups:  $USER was added to gpio/spi/i2c — LOG OUT + BACK IN (or reboot)"
    echo "           before running --paint-test or relying on the service."
    echo
fi
if $INSTALL_SERVICE; then
    echo "  service: sudo systemctl status tesserae-pi-bin-client"
    echo "           sudo journalctl -u tesserae-pi-bin-client -f"
    echo "           sudo systemctl restart tesserae-pi-bin-client  # after config edits"
else
    echo "  service: not installed (re-run without --no-service to install)"
fi
echo
echo "  manual:  tesserae-pi-bin-client --paint-test   # paints a 6-colour stripe"
echo "           tesserae-pi-bin-client                # run in foreground"
echo
