from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .panels import is_valid_model

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "tesserae-pi-bin-client" / "config.toml"

# Matches what the Tesserae server accepts for instance ids: 2–32 chars,
# lowercase letter first, then letters/digits/_/-. Enforced both here and
# by install.sh so a bad value fails before the config is written.
DEVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

TRANSPORT_MODES = {"mqtt", "rest", "relay"}

# The hosted relay Worker. A self-hosted deployment just sets its own origin,
# so the two are interchangeable.
RELAY_DEFAULT_URL = "https://relay.tesserae.ink"

# The server's front-button vocabulary, shared with the sibling firmware:
# it maps these to refresh / rotate_prev / rotate_next. Sending anything else
# is a silent no-op server-side, so reject it at config-parse time instead.
BUTTON_ACTIONS = {"refresh", "left", "right"}

# Ambient sensors the environment module knows how to read.
ENV_SENSORS = {"sht4x", "shtc3"}

# Battery sources the battery module knows how to read.
BATTERY_SOURCES = {"pisugar3", "pisugar-server"}


def _toml_str(value: str) -> str:
    """Render a TOML basic-string literal with the bare-minimum escaping."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config_toml(
    mqtt_host: str = "192.168.1.10",
    mqtt_port: int = 1883,
    mqtt_username: str = "",
    mqtt_password: str = "",
    mqtt_client_id: str = "pi-impression-1",
    mqtt_keepalive: int = 60,
    device_id: str = "pi_bin",
    panel_model: str = "inky_13_3",
    download_timeout_s: int = 30,
    max_frame_bytes: int = 16_000_000,
    log_level: str = "INFO",
    transport_mode: str = "rest",
    rest_server_url: str = "http://tesserae.local:8765",
    rest_device_token: str = "",
    rest_pairing_code: str = "",
    rest_last_frame_etag: str = "",
    rest_poll_interval_s: int = 60,
    rest_tz: str = "",
    rest_sse_enabled: bool = True,
    buttons_enabled: bool = False,
    buttons_chip: str = "",
    buttons_pin_a: int = 5,
    buttons_pin_b: int = 6,
    buttons_pin_c: int = 25,
    buttons_pin_d: int = 24,
    buttons_action_a: str = "left",
    buttons_action_b: str = "refresh",
    buttons_action_c: str = "right",
    buttons_action_d: str = "",
    buttons_debounce_ms: int = 50,
    relay_url: str = RELAY_DEFAULT_URL,
    relay_pairing_code: str = "",
    relay_install_id: str = "",
    relay_device_id: str = "",
    relay_device_token: str = "",
    relay_frame_key: str = "",
    relay_private_key: str = "",
    relay_last_frame_etag: str = "",
    relay_config_etag: str = "",
    relay_poll_interval_s: int = 60,
    cache_enabled: bool = True,
    cache_root: str = "",
    ota_report_enabled: bool = True,
    env_sensor: str = "",
    env_i2c_bus: int = 1,
    battery_source: str = "",
    battery_i2c_bus: int = 1,
) -> str:
    """Build a config.toml body from arbitrary overrides.

    Used by install.sh (via `python -m tesserae_pi_bin_client.bootstrap_config`)
    when the user is prompted for MQTT and panel values during first-time
    setup. Calling with no arguments yields the same defaults as DEFAULT_TOML.
    """
    return (
        f"transport_mode = {_toml_str(transport_mode)}  # mqtt | rest\n"
        "\n"
        "[mqtt]\n"
        f"host = {_toml_str(mqtt_host)}\n"
        f"port = {mqtt_port}\n"
        f"username = {_toml_str(mqtt_username)}\n"
        f"password = {_toml_str(mqtt_password)}\n"
        f"client_id = {_toml_str(mqtt_client_id)}\n"
        f"device_id = {_toml_str(device_id)}"
        "  # MQTT topic prefix: tesserae/<device_id>/...\n"
        f"keepalive = {mqtt_keepalive}\n"
        "\n"
        "[rest]\n"
        f"server_url = {_toml_str(rest_server_url)}"
        "      # e.g. http://tesserae.local:8765 (rest mode only)\n"
        f"device_token = {_toml_str(rest_device_token)}"
        "    # auto-populated after pair/discover; do not edit by hand\n"
        f"pairing_code = {_toml_str(rest_pairing_code)}"
        "    # single-use; wiped after first successful register\n"
        f"last_frame_etag = {_toml_str(rest_last_frame_etag)}"
        " # auto-populated for If-None-Match short-circuit\n"
        f"poll_interval_s = {rest_poll_interval_s}"
        "          # fallback wake interval if server omits next_poll_s\n"
        f"tz = {_toml_str(rest_tz)}"
        "               # auto-populated from the server's config.tz\n"
        f"sse_enabled = {'true' if rest_sse_enabled else 'false'}"
        "        # hold GET /stream for instant repaints (polling stays the fallback)\n"
        "\n"
        "[relay]\n"
        "# Cloud relay: run this panel somewhere the Tesserae server isn't\n"
        "# reachable from. Both ends talk outbound to a zero-knowledge mailbox.\n"
        "# Everything below except relay_url and poll_interval_s is written by\n"
        "# the daemon during pairing — do not edit by hand.\n"
        f"relay_url = {_toml_str(relay_url)}\n"
        f"pairing_code = {_toml_str(relay_pairing_code)}"
        "    # single-use; from Settings -> Cloud relay\n"
        f"install_id = {_toml_str(relay_install_id)}\n"
        f"device_id = {_toml_str(relay_device_id)}\n"
        f"device_token = {_toml_str(relay_device_token)}\n"
        f"frame_key = {_toml_str(relay_frame_key)}"
        "       # derived locally; never transmitted\n"
        f"private_key = {_toml_str(relay_private_key)}"
        "     # wiped once frame_key exists\n"
        f"last_frame_etag = {_toml_str(relay_last_frame_etag)}\n"
        f"config_etag = {_toml_str(relay_config_etag)}\n"
        f"poll_interval_s = {relay_poll_interval_s}\n"
        "\n"
        "[panel]\n"
        f"model = {_toml_str(panel_model)}"
        "  # inky_4 | inky_5_7 | inky_7_3 | inky_13_3\n"
        "\n"
        "[buttons]\n"
        "# The Impression HATs carry four front buttons (A-D). Pin numbers are\n"
        "# BCM GPIO and differ by model: A=5 B=6 D=24 on every Impression, but\n"
        "# C is GPIO 16 on the 4\"/5.7\"/7.3\" and GPIO 25 on the 13.3\".\n"
        "# Actions are the server's vocabulary: refresh | left | right (left and\n"
        "# right map to rotate_prev / rotate_next). Empty = button unmapped.\n"
        f"enabled = {'true' if buttons_enabled else 'false'}\n"
        f"chip = {_toml_str(buttons_chip)}"
        "            # gpiochip name; empty = autodetect\n"
        f"pin_a = {buttons_pin_a}\n"
        f"pin_b = {buttons_pin_b}\n"
        f"pin_c = {buttons_pin_c}"
        "               # 16 on the 4\"/5.7\"/7.3\", 25 on the 13.3\"\n"
        f"pin_d = {buttons_pin_d}\n"
        f"action_a = {_toml_str(buttons_action_a)}\n"
        f"action_b = {_toml_str(buttons_action_b)}\n"
        f"action_c = {_toml_str(buttons_action_c)}\n"
        f"action_d = {_toml_str(buttons_action_d)}\n"
        f"debounce_ms = {buttons_debounce_ms}\n"
        "\n"
        "[cache]\n"
        "# Local frame cache: deck pages navigate without a server round trip,\n"
        "# and a bound album keeps playing while the server is down.\n"
        "# Advertising the capability is what makes the server offer them.\n"
        f"enabled = {'true' if cache_enabled else 'false'}\n"
        f"root = {_toml_str(cache_root)}"
        "                # empty = ~/.cache/tesserae-pi-bin-client\n"
        "\n"
        "[ota]\n"
        "# Reports update-attempt state to the server's Devices UI. This client\n"
        "# does not install anything itself — whatever does (apt/pip/git) records\n"
        "# its progress here. Advertising the schema is what lets the server\n"
        "# offer an update at all.\n"
        f"report_enabled = {'true' if ota_report_enabled else 'false'}\n"
        "\n"
        "[environment]\n"
        "# Optional ambient sensor on the HAT's existing I2C bus.\n"
        f"sensor = {_toml_str(env_sensor)}"
        "             # \"\" | sht4x | shtc3\n"
        f"i2c_bus = {env_i2c_bus}\n"
        "\n"
        "[battery]\n"
        "# Optional PiSugar UPS. pisugar3 reads the gauge over I2C directly;\n"
        "# pisugar-server asks PiSugar's power manager on 127.0.0.1:8423.\n"
        f"source = {_toml_str(battery_source)}"
        "             # \"\" | pisugar3 | pisugar-server\n"
        f"i2c_bus = {battery_i2c_bus}\n"
        "\n"
        "[http]\n"
        f"download_timeout_s = {download_timeout_s}\n"
        f"max_frame_bytes = {max_frame_bytes}\n"
        "\n"
        "[logging]\n"
        f"level = {_toml_str(log_level)}\n"
    )


DEFAULT_TOML = render_config_toml()


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str
    password: str
    client_id: str
    keepalive: int
    device_id: str = "pi_bin"


@dataclass(frozen=True)
class PanelConfig:
    model: str


@dataclass(frozen=True)
class HttpConfig:
    download_timeout_s: int
    max_frame_bytes: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class RestConfig:
    """REST-transport state. Most fields are runtime-managed: device_token
    and last_frame_etag are written back by the daemon, pairing_code is
    single-use and wiped after a successful register. Only server_url and
    poll_interval_s are user-facing defaults."""
    server_url: str = ""
    device_token: str = ""
    pairing_code: str = ""
    last_frame_etag: str = ""
    poll_interval_s: int = 60
    # Adopted from the /status response's config.tz and persisted, so the
    # timezone the server assigned survives a restart and rides every beat
    # back (the sibling firmware keeps it in NVS for the same reason).
    tz: str = ""
    # SSE is an optimisation over polling, never a correctness requirement —
    # turn it off if a reverse proxy buffers the stream route.
    sse_enabled: bool = True


@dataclass(frozen=True)
class OtaConfig:
    """OTA lifecycle reporting. Reporting only — nothing here installs."""

    report_enabled: bool = True

    def state_path(self, cache_root: Path) -> Path:
        return cache_root / "ota-state.json"


@dataclass(frozen=True)
class EnvironmentConfig:
    """Optional ambient sensor on the HAT's I²C bus."""

    sensor: str = ""
    i2c_bus: int = 1


@dataclass(frozen=True)
class BatteryConfig:
    """Optional PiSugar UPS, read over I²C or via pisugar-server."""

    source: str = ""
    i2c_bus: int = 1


@dataclass(frozen=True)
class CacheConfig:
    """Local frame cache for deck pages and album frames."""

    enabled: bool = True
    root: str = ""

    def root_path(self) -> Path:
        """Where cached frames live. Empty config means the XDG-ish default."""
        from .frame_cache import DEFAULT_CACHE_ROOT

        return Path(self.root).expanduser() if self.root else DEFAULT_CACHE_ROOT


@dataclass(frozen=True)
class RelayConfig:
    """Cloud-relay transport state.

    Only relay_url and poll_interval_s are user-facing; everything else is
    written by the pairing flow. `private_key` exists solely between the
    pairing POST and the key derivation, then is wiped — it is also the latch
    that makes the POST happen exactly once.
    """

    relay_url: str = RELAY_DEFAULT_URL
    pairing_code: str = ""
    install_id: str = ""
    device_id: str = ""
    device_token: str = ""
    frame_key: str = ""
    private_key: str = ""
    last_frame_etag: str = ""
    config_etag: str = ""
    poll_interval_s: int = 60


@dataclass(frozen=True)
class ButtonsConfig:
    """Physical front-button wiring. Off by default: a HAT without buttons,
    or a Pi where libgpiod isn't available, must keep working untouched."""

    enabled: bool = False
    chip: str = ""
    pin_a: int = 5
    pin_b: int = 6
    pin_c: int = 25
    pin_d: int = 24
    action_a: str = "left"
    action_b: str = "refresh"
    action_c: str = "right"
    action_d: str = ""
    debounce_ms: int = 50

    def pin_actions(self) -> dict[int, str]:
        """BCM pin -> server action, skipping unmapped buttons.

        Built as a dict so two buttons cannot both claim a pin: a duplicate
        pin in config collapses here rather than producing two watchers on
        one line, which libgpiod would refuse anyway.
        """
        pairs = (
            (self.pin_a, self.action_a),
            (self.pin_b, self.action_b),
            (self.pin_c, self.action_c),
            (self.pin_d, self.action_d),
        )
        return {pin: action for pin, action in pairs if action}


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    panel: PanelConfig
    http: HttpConfig
    logging: LoggingConfig
    transport_mode: str = "mqtt"
    rest: RestConfig = RestConfig()
    buttons: ButtonsConfig = ButtonsConfig()
    relay: RelayConfig = RelayConfig()
    cache: CacheConfig = CacheConfig()
    ota: OtaConfig = OtaConfig()
    environment: EnvironmentConfig = EnvironmentConfig()
    battery: BatteryConfig = BatteryConfig()


def _require(section: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in section:
        raise ValueError(f"missing [{where}].{key}")
    value = section[key]
    if not isinstance(value, kind):
        raise ValueError(
            f"[{where}].{key} must be {kind.__name__}, got {type(value).__name__}"
        )
    return value


def _parse(raw: dict[str, Any]) -> Config:
    mqtt_section = raw.get("mqtt", {})
    if not isinstance(mqtt_section, dict):
        raise ValueError("[mqtt] must be a table")
    # device_id is optional in config.toml. It defaults to "pi_bin" to match
    # the Tesserae server's pi_bin_client topic prefix (tesserae/pi_bin/...).
    # An existing config that omits the line now resolves to "pi_bin" too —
    # the intended migration after the server split pi_client into
    # pi_bin_client / pi_png_client. Set device_id = "pi" explicitly to keep
    # the legacy tesserae/pi/... prefix.
    device_id = mqtt_section.get("device_id", "pi_bin")
    if not isinstance(device_id, str):
        raise ValueError(
            f"[mqtt].device_id must be str, got {type(device_id).__name__}"
        )
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError(
            f"[mqtt].device_id invalid: {device_id!r} "
            "(2–32 chars, lowercase letter first, then [a-z0-9_-])"
        )
    mqtt = MqttConfig(
        host=_require(mqtt_section, "host", str, "mqtt"),
        port=_require(mqtt_section, "port", int, "mqtt"),
        username=mqtt_section.get("username", ""),
        password=mqtt_section.get("password", ""),
        client_id=_require(mqtt_section, "client_id", str, "mqtt"),
        keepalive=_require(mqtt_section, "keepalive", int, "mqtt"),
        device_id=device_id,
    )
    if not 1 <= mqtt.port <= 65535:
        raise ValueError(f"[mqtt].port out of range: {mqtt.port}")
    if mqtt.keepalive <= 0:
        raise ValueError(f"[mqtt].keepalive must be positive, got {mqtt.keepalive}")

    panel_section = raw.get("panel", {})
    if not isinstance(panel_section, dict):
        raise ValueError("[panel] must be a table")
    panel = PanelConfig(model=_require(panel_section, "model", str, "panel"))
    if not is_valid_model(panel.model):
        raise ValueError(f"[panel].model unknown: {panel.model!r}")

    http_section = raw.get("http", {})
    if not isinstance(http_section, dict):
        raise ValueError("[http] must be a table")
    http = HttpConfig(
        download_timeout_s=_require(http_section, "download_timeout_s", int, "http"),
        max_frame_bytes=_require(http_section, "max_frame_bytes", int, "http"),
    )
    if http.download_timeout_s <= 0:
        raise ValueError("[http].download_timeout_s must be positive")
    if http.max_frame_bytes <= 0:
        raise ValueError("[http].max_frame_bytes must be positive")

    log_section = raw.get("logging", {})
    if not isinstance(log_section, dict):
        raise ValueError("[logging] must be a table")
    logging_cfg = LoggingConfig(level=log_section.get("level", "INFO"))
    if logging_cfg.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"[logging].level unknown: {logging_cfg.level!r}")

    # transport_mode is a top-level key so it can switch out the whole
    # transport without touching the section it belongs to. Default "mqtt"
    # preserves existing behaviour for configs predating the REST split.
    transport_mode = raw.get("transport_mode", "mqtt")
    if not isinstance(transport_mode, str):
        raise ValueError(
            f"transport_mode must be str, got {type(transport_mode).__name__}"
        )
    if transport_mode not in TRANSPORT_MODES:
        raise ValueError(
            f"transport_mode unknown: {transport_mode!r} "
            f"(expected one of {sorted(TRANSPORT_MODES)})"
        )

    rest_section = raw.get("rest", {})
    if not isinstance(rest_section, dict):
        raise ValueError("[rest] must be a table")
    rest = RestConfig(
        server_url=rest_section.get("server_url", ""),
        device_token=rest_section.get("device_token", ""),
        pairing_code=rest_section.get("pairing_code", ""),
        last_frame_etag=rest_section.get("last_frame_etag", ""),
        poll_interval_s=rest_section.get("poll_interval_s", 60),
        tz=rest_section.get("tz", ""),
        sse_enabled=rest_section.get("sse_enabled", True),
    )
    if not isinstance(rest.sse_enabled, bool):
        raise ValueError(
            f"[rest].sse_enabled must be bool, got {type(rest.sse_enabled).__name__}"
        )
    for name, value in (
        ("server_url", rest.server_url),
        ("device_token", rest.device_token),
        ("pairing_code", rest.pairing_code),
        ("last_frame_etag", rest.last_frame_etag),
        ("tz", rest.tz),
    ):
        if not isinstance(value, str):
            raise ValueError(
                f"[rest].{name} must be str, got {type(value).__name__}"
            )
    if not isinstance(rest.poll_interval_s, int) or rest.poll_interval_s <= 0:
        raise ValueError("[rest].poll_interval_s must be a positive integer")
    if transport_mode == "rest" and not rest.server_url:
        raise ValueError(
            "[rest].server_url is required when transport_mode = 'rest' "
            "(e.g. server_url = \"http://tesserae.local:8765\")"
        )

    relay_section = raw.get("relay", {})
    if not isinstance(relay_section, dict):
        raise ValueError("[relay] must be a table")
    relay = RelayConfig(
        relay_url=relay_section.get("relay_url", RELAY_DEFAULT_URL),
        pairing_code=relay_section.get("pairing_code", ""),
        install_id=relay_section.get("install_id", ""),
        device_id=relay_section.get("device_id", ""),
        device_token=relay_section.get("device_token", ""),
        frame_key=relay_section.get("frame_key", ""),
        private_key=relay_section.get("private_key", ""),
        last_frame_etag=relay_section.get("last_frame_etag", ""),
        config_etag=relay_section.get("config_etag", ""),
        poll_interval_s=relay_section.get("poll_interval_s", 60),
    )
    for name, value in (
        ("relay_url", relay.relay_url),
        ("pairing_code", relay.pairing_code),
        ("install_id", relay.install_id),
        ("device_id", relay.device_id),
        ("device_token", relay.device_token),
        ("frame_key", relay.frame_key),
        ("private_key", relay.private_key),
        ("last_frame_etag", relay.last_frame_etag),
        ("config_etag", relay.config_etag),
    ):
        if not isinstance(value, str):
            raise ValueError(
                f"[relay].{name} must be str, got {type(value).__name__}"
            )
    if not isinstance(relay.poll_interval_s, int) or relay.poll_interval_s <= 0:
        raise ValueError("[relay].poll_interval_s must be a positive integer")
    if transport_mode == "relay" and not relay.relay_url:
        raise ValueError(
            "[relay].relay_url is required when transport_mode = 'relay'"
        )
    # "Paired or has a code to pair with" is deliberately NOT enforced here.
    # Dropping a revoked pairing legitimately produces a config with neither,
    # and that file must round-trip — a daemon that writes state it cannot
    # read back on restart is worse than one that reports the problem at
    # startup, which transports/relay.run() does with an actionable message.

    buttons_section = raw.get("buttons", {})
    if not isinstance(buttons_section, dict):
        raise ValueError("[buttons] must be a table")
    buttons = ButtonsConfig(
        enabled=buttons_section.get("enabled", False),
        chip=buttons_section.get("chip", ""),
        pin_a=buttons_section.get("pin_a", 5),
        pin_b=buttons_section.get("pin_b", 6),
        pin_c=buttons_section.get("pin_c", 25),
        pin_d=buttons_section.get("pin_d", 24),
        action_a=buttons_section.get("action_a", "left"),
        action_b=buttons_section.get("action_b", "refresh"),
        action_c=buttons_section.get("action_c", "right"),
        action_d=buttons_section.get("action_d", ""),
        debounce_ms=buttons_section.get("debounce_ms", 50),
    )
    if not isinstance(buttons.enabled, bool):
        raise ValueError(
            f"[buttons].enabled must be bool, got {type(buttons.enabled).__name__}"
        )
    if not isinstance(buttons.chip, str):
        raise ValueError(
            f"[buttons].chip must be str, got {type(buttons.chip).__name__}"
        )
    for name, pin in (
        ("pin_a", buttons.pin_a),
        ("pin_b", buttons.pin_b),
        ("pin_c", buttons.pin_c),
        ("pin_d", buttons.pin_d),
    ):
        if not isinstance(pin, int) or isinstance(pin, bool) or not 0 <= pin <= 63:
            raise ValueError(f"[buttons].{name} must be a BCM pin number 0-63")
    for name, action in (
        ("action_a", buttons.action_a),
        ("action_b", buttons.action_b),
        ("action_c", buttons.action_c),
        ("action_d", buttons.action_d),
    ):
        if not isinstance(action, str):
            raise ValueError(
                f"[buttons].{name} must be str, got {type(action).__name__}"
            )
        if action and action not in BUTTON_ACTIONS:
            raise ValueError(
                f"[buttons].{name} unknown: {action!r} "
                f"(expected one of {sorted(BUTTON_ACTIONS)}, or \"\" to unmap)"
            )
    if (
        not isinstance(buttons.debounce_ms, int)
        or isinstance(buttons.debounce_ms, bool)
        or buttons.debounce_ms < 0
    ):
        raise ValueError("[buttons].debounce_ms must be a non-negative integer")
    if buttons.enabled and not buttons.pin_actions():
        raise ValueError(
            "[buttons].enabled is true but every action_* is empty — "
            "map at least one button or set enabled = false"
        )

    cache_section = raw.get("cache", {})
    if not isinstance(cache_section, dict):
        raise ValueError("[cache] must be a table")
    cache = CacheConfig(
        enabled=cache_section.get("enabled", True),
        root=cache_section.get("root", ""),
    )
    if not isinstance(cache.enabled, bool):
        raise ValueError(
            f"[cache].enabled must be bool, got {type(cache.enabled).__name__}"
        )
    if not isinstance(cache.root, str):
        raise ValueError(
            f"[cache].root must be str, got {type(cache.root).__name__}"
        )

    ota_section = raw.get("ota", {})
    if not isinstance(ota_section, dict):
        raise ValueError("[ota] must be a table")
    ota = OtaConfig(report_enabled=ota_section.get("report_enabled", True))
    if not isinstance(ota.report_enabled, bool):
        raise ValueError(
            f"[ota].report_enabled must be bool, got {type(ota.report_enabled).__name__}"
        )

    env_section = raw.get("environment", {})
    if not isinstance(env_section, dict):
        raise ValueError("[environment] must be a table")
    environment = EnvironmentConfig(
        sensor=env_section.get("sensor", ""),
        i2c_bus=env_section.get("i2c_bus", 1),
    )
    if not isinstance(environment.sensor, str):
        raise ValueError(
            f"[environment].sensor must be str, got {type(environment.sensor).__name__}"
        )
    if environment.sensor and environment.sensor not in ENV_SENSORS:
        raise ValueError(
            f"[environment].sensor unknown: {environment.sensor!r} "
            f"(expected one of {sorted(ENV_SENSORS)}, or \"\" for none)"
        )
    if (
        not isinstance(environment.i2c_bus, int)
        or isinstance(environment.i2c_bus, bool)
        or environment.i2c_bus < 0
    ):
        raise ValueError("[environment].i2c_bus must be a non-negative integer")

    battery_section = raw.get("battery", {})
    if not isinstance(battery_section, dict):
        raise ValueError("[battery] must be a table")
    battery = BatteryConfig(
        source=battery_section.get("source", ""),
        i2c_bus=battery_section.get("i2c_bus", 1),
    )
    if not isinstance(battery.source, str):
        raise ValueError(
            f"[battery].source must be str, got {type(battery.source).__name__}"
        )
    if battery.source and battery.source not in BATTERY_SOURCES:
        raise ValueError(
            f"[battery].source unknown: {battery.source!r} "
            f"(expected one of {sorted(BATTERY_SOURCES)}, or \"\" for none)"
        )
    if (
        not isinstance(battery.i2c_bus, int)
        or isinstance(battery.i2c_bus, bool)
        or battery.i2c_bus < 0
    ):
        raise ValueError("[battery].i2c_bus must be a non-negative integer")

    return Config(
        mqtt=mqtt,
        panel=panel,
        http=http,
        logging=logging_cfg,
        transport_mode=transport_mode,
        rest=rest,
        buttons=buttons,
        relay=relay,
        cache=cache,
        ota=ota,
        environment=environment,
        battery=battery,
    )


def parse_toml(text: str) -> Config:
    return _parse(tomllib.loads(text))


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML, encoding="utf-8")
    return parse_toml(path.read_text(encoding="utf-8"))


def render_from_config(cfg: Config) -> str:
    """Re-render a Config back to TOML using the canonical layout.

    Used by save_config() when the daemon writes back runtime state
    (device_token after first pair, last_frame_etag after each paint).
    Round-trips every field; user comments are not preserved.
    """
    return render_config_toml(
        mqtt_host=cfg.mqtt.host,
        mqtt_port=cfg.mqtt.port,
        mqtt_username=cfg.mqtt.username,
        mqtt_password=cfg.mqtt.password,
        mqtt_client_id=cfg.mqtt.client_id,
        mqtt_keepalive=cfg.mqtt.keepalive,
        device_id=cfg.mqtt.device_id,
        panel_model=cfg.panel.model,
        download_timeout_s=cfg.http.download_timeout_s,
        max_frame_bytes=cfg.http.max_frame_bytes,
        log_level=cfg.logging.level,
        transport_mode=cfg.transport_mode,
        rest_server_url=cfg.rest.server_url,
        rest_device_token=cfg.rest.device_token,
        rest_pairing_code=cfg.rest.pairing_code,
        rest_last_frame_etag=cfg.rest.last_frame_etag,
        rest_poll_interval_s=cfg.rest.poll_interval_s,
        rest_tz=cfg.rest.tz,
        rest_sse_enabled=cfg.rest.sse_enabled,
        buttons_enabled=cfg.buttons.enabled,
        buttons_chip=cfg.buttons.chip,
        buttons_pin_a=cfg.buttons.pin_a,
        buttons_pin_b=cfg.buttons.pin_b,
        buttons_pin_c=cfg.buttons.pin_c,
        buttons_pin_d=cfg.buttons.pin_d,
        buttons_action_a=cfg.buttons.action_a,
        buttons_action_b=cfg.buttons.action_b,
        buttons_action_c=cfg.buttons.action_c,
        buttons_action_d=cfg.buttons.action_d,
        buttons_debounce_ms=cfg.buttons.debounce_ms,
        relay_url=cfg.relay.relay_url,
        relay_pairing_code=cfg.relay.pairing_code,
        relay_install_id=cfg.relay.install_id,
        relay_device_id=cfg.relay.device_id,
        relay_device_token=cfg.relay.device_token,
        relay_frame_key=cfg.relay.frame_key,
        relay_private_key=cfg.relay.private_key,
        relay_last_frame_etag=cfg.relay.last_frame_etag,
        relay_config_etag=cfg.relay.config_etag,
        relay_poll_interval_s=cfg.relay.poll_interval_s,
        cache_enabled=cfg.cache.enabled,
        cache_root=cfg.cache.root,
        ota_report_enabled=cfg.ota.report_enabled,
        env_sensor=cfg.environment.sensor,
        env_i2c_bus=cfg.environment.i2c_bus,
        battery_source=cfg.battery.source,
        battery_i2c_bus=cfg.battery.i2c_bus,
    )


def save_config(cfg: Config, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Atomically persist Config back to disk (temp file + rename).

    The REST loop calls this after first-pair (to save device_token) and
    after each successful paint (to save last_frame_etag). Atomic write
    means a power-cut mid-write leaves the previous config intact rather
    than a truncated half-written file.
    """
    body = render_from_config(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory so the rename is atomic
    # within a single filesystem.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def with_rest_updates(cfg: Config, **rest_fields: Any) -> Config:
    """Return a new Config with the named RestConfig fields replaced."""
    return replace(cfg, rest=replace(cfg.rest, **rest_fields))


def with_relay_updates(cfg: Config, **relay_fields: Any) -> Config:
    """Return a new Config with the named RelayConfig fields replaced."""
    return replace(cfg, relay=replace(cfg.relay, **relay_fields))
