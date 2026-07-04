# tesserae-device-pi-bin

A headless Raspberry Pi daemon that subscribes to a [Tesserae] server over MQTT,
downloads pre-packed 4-bpp `.bin` frames, and paints them onto a Pimoroni
[Inky Impression] (Spectra 6 / Waveshare E6) e-ink panel.

It is the consumer end of the Tesserae render pipeline:

```
Tesserae server  ──▶  MQTT broker  ──▶  tesserae-pi-bin-client  ──▶  Inky panel
   (renders +                          (this project, on the Pi)        (SPI)
    packs to 4-bpp)
```

The server does all the dithering, palette mapping, and pixel packing. This
client just downloads bytes and pushes them at the panel — no PIL involved on
the paint path.

## Status

Pre-release; tested against the API of `inky==2.4.0`.

## Hardware

- Raspberry Pi running Raspberry Pi OS Bookworm or later
- Pimoroni Inky Impression — 4" (640×400), 5.7" (600×448), 7.3" (800×480), or
  13.3" (1600×1200). The model is auto-detected via the HAT EEPROM; the
  `panel.model` config field is a fallback + sanity-check.

## Install (on the Pi)

One command from a fresh Raspberry Pi OS Bookworm image. Run it **as your
normal user** (not via sudo — the script invokes sudo internally where needed):

```bash
git clone https://github.com/dmellok/tesserae-device-pi-bin.git
cd tesserae-device-pi-bin
./scripts/install.sh
```

That handles, in order:

1. `apt-get install` build + runtime prerequisites
2. `raspi-config nonint do_spi 0` + `do_i2c 0` and `dtoverlay=spi0-0cs` in
   `config.txt` — enable SPI (pixels) and I2C (HAT EEPROM auto-detect), and
   free GPIO7/8 so `inky` can drive chip-select in software. Enabling either
   interface or adding the overlay flags a **reboot** at the end.
3. `usermod -aG gpio,spi,i2c $USER` — group membership for HAT access;
   `i2c` is needed for the HAT EEPROM read on Inky Impression
4. `python3 -m venv .venv` + `pip install -e .` — pulls the pinned `inky[rpi]`
5. **Prompts** for transport (`rest` default — just the Tesserae server URL
   — or `mqtt` for an existing broker setup), device id, transport-specific
   fields, and panel model, then writes
   `~/.config/tesserae-pi-bin-client/config.toml` (mode 0600). If a config
   already exists it's left alone — pass `--reconfigure` to overwrite, or
   `--non-interactive` to write defaults without prompting.
6. Symlinks the venv binary to `/usr/local/bin/tesserae-pi-bin-client`
7. Installs + enables + starts the systemd unit

After it finishes (the service is already running with your config):

```bash
# If groups were just added, log out + back in (or reboot) first.
sudo systemctl status tesserae-pi-bin-client
sudo journalctl -u tesserae-pi-bin-client -f
```

To change MQTT/panel settings later, edit
`~/.config/tesserae-pi-bin-client/config.toml` and
`sudo systemctl restart tesserae-pi-bin-client`, or re-run
`./scripts/install.sh --reconfigure`.

To verify the hardware path without involving MQTT, run the stripe test:

```bash
tesserae-pi-bin-client --paint-test     # paints six vertical colour bands
```

### `install.sh` flags

```
--no-service        don't install the systemd unit
--paint-test        run --paint-test at the end (only useful if you didn't
                    just get added to gpio/spi/i2c)
--skip-apt          skip apt update + install (assume packages are present)
--non-interactive   never prompt — write a default config if none exists
--reconfigure       re-prompt for MQTT/panel values and overwrite the config
--user USER         user the systemd unit runs as (default: $USER)
```

The script is idempotent — re-run it after `git pull` to upgrade the package
and re-install the unit.

### Just the systemd unit

If you've already installed the package some other way and you only want to
(re-)install the unit:

```bash
sudo ./scripts/install-service.sh "$USER"
```

This expects `tesserae-pi-bin-client` to already be on `PATH` at
`/usr/local/bin/tesserae-pi-bin-client`.

## Configuration

Defaults live at `~/.config/tesserae-pi-bin-client/config.toml`:

```toml
transport_mode = "rest"  # mqtt | rest

[mqtt]
host = "192.168.1.10"
port = 1883
username = ""        # optional
password = ""        # optional
client_id = "pi-impression-1"
device_id = "pi_bin" # device id (URL path + MQTT topic prefix — tesserae/<device_id>/...)
keepalive = 60

[rest]
server_url = "http://tesserae.local:8765"
device_token = ""         # auto-populated after pair/discover
pairing_code = ""         # single-use; wiped after first successful register
last_frame_etag = ""      # auto-populated for If-None-Match short-circuit
poll_interval_s = 60      # fallback wake interval if server omits next_poll_s

[panel]
model = "inky_13_3"  # inky_4 | inky_5_7 | inky_7_3 | inky_13_3

[http]
download_timeout_s = 30
max_frame_bytes = 16000000

[logging]
level = "INFO"
```

Pass `--config /path/to/config.toml` to override.

## Transports

The client speaks one of two transports, selected by `transport_mode`:

- **`rest`** (default for fresh installs) — polls the Tesserae server's
  `/api/v1/` directly. No broker needed; one round-trip per wake cycle
  (`GET /frame` + `POST /status`). Out of the box the wake cadence is
  **every 60 s**; the server's `/status` response can push a different
  `next_poll_s` per cycle or a durable `config.sleep_interval_s`, both
  clamped to `[30s, 7d]`.
- **`mqtt`** — subscribes to a broker and reacts to retained frame
  announcements. Stays connected; pushes are near-instant. Requires a
  broker on the LAN.

The installer prompts for transport at the top and only asks for the
relevant fields (REST → server URL + optional pairing code; MQTT →
broker host/port/credentials/client id).

Switching mode later is a config-file edit + `sudo systemctl restart
tesserae-pi-bin-client`. **Existing `config.toml` files** without
`transport_mode` continue to default to **`mqtt`** (no surprise mode
switch on upgrade); only fresh installs get the new REST default.

### REST mode setup (default install)

When the installer asks for transport, hit Enter to accept `rest`,
then either:

1. **Recommended path (zero typing on the device):** start the daemon
   with no pairing code and click **Register** on the discovered row in
   the server's Settings → Devices page. The daemon's next
   `POST /device/discover` claims the token by MAC match; you'll see
   "registered via discover" in the journal.
2. **Strict path (per-device admin approval):** generate a 6-digit
   pairing code in Settings → Devices and enter it at the installer's
   pairing-code prompt. The code is single-use; after a successful
   register the daemon wipes it and saves the issued `device_token`.

To re-pair after the fact (e.g. token was revoked, or the local
`device_token` got wiped), generate a fresh code and run once with the
CLI override:

```bash
tesserae-pi-bin-client --pair 123456
```

`--pair` overrides whatever is in `[rest].pairing_code` for that run
only and is no-op'd if a `device_token` is already saved.

If a 401 ever comes back from the server (token revoked or wiped from
the server side), the daemon clears the local `device_token` and exits.
Restart with `--pair` or rely on the discover loop to recover.

## MQTT contract

Every topic is namespaced by the `device_id` from `[mqtt]` — defaults to
`pi_bin`, which is the prefix the Tesserae server's `pi_bin_client` device
kind publishes on (`tesserae/pi_bin/frame/bin`). Give each physical display
its own id (e.g. `pi_bin_kitchen`) so multiple Pis can share one broker
without colliding.

> **Migration note.** This default was `pi` before the Tesserae server split
> its old `pi_client` kind into `pi_bin_client` / `pi_png_client`. An existing
> `config.toml` with no `device_id` line now resolves to `pi_bin` (not `pi`),
> which is the intended change so a fresh install talks to the server out of
> the box. To keep the legacy `tesserae/pi/...` prefix, set `device_id = "pi"`
> explicitly in `[mqtt]`.

### Subscribes

Topic `tesserae/<device_id>/frame/bin`, payload (not retained):

```json
{ "url": "http://192.168.1.10:8000/renders/<digest>.bin" }
```

The client downloads the URL, validates the byte count matches
`panel_w * panel_h / 2`, and paints. Duplicate-digest frames are skipped.

### Publishes (retained)

Topic `tesserae/<device_id>/status`:

```json
{
  "state": "idle",
  "last_paint_at": 1734567890.123,
  "last_error": null,
  "last_digest": "3f7a91b2c4e5d6f8",
  "uptime_s": 3601,
  "fw_version": "0.1.0",
  "panel": "inky_13_3"
}
```

Heartbeat publishes every 60 s and immediately on state change. A retained
last-will of `{"state":"offline"}` is set on connect.

## REST contract

Active when `transport_mode = "rest"`. All endpoints sit under `/api/v1/`
on the configured `server_url`. The client sends both `Authorization:
Bearer <token>` and `X-Tesserae-Token: <token>` on every authenticated
call (cheap belt-and-suspenders against header-stripping middleboxes).

### First boot — `POST /device/discover`

Body: `{device_id, kind: "pi_bin_client", panel_w, panel_h, fw_version, mac}`.

The server responds in one of two shapes:

- `{registered: false, retry_after_s: 30}` — the device shows up in
  Settings → Devices "Discovered" strip; sleep `retry_after_s` and poll
  again.
- `{registered: true, device_token, device_id, server_time}` — admin
  clicked Register; persist the token, **adopt `device_id` from the
  response** (it may differ from what was sent — using the wrong one
  gives 403 on subsequent calls), then enter the wake loop.

### Alternative first boot — `POST /device/register`

Header: `X-Pairing-Code: <6-digit-code>`, body same as discover.
Returns `201 + {device_token, device_id, reused_existing}` on success;
`403` on bad/expired code (process exits — generate a fresh code);
`429 + Retry-After` if rate-limited.

### Wake loop — `GET /device/<id>/frame`

Header: `Authorization: Bearer <token>`, optional `If-None-Match:
<last_frame_etag>`.

- `200` + JSON `{url, format, panel_w, panel_h, render_id, renderer_id}`
  and `ETag: "<sha256>"` — download `url`, paint, save the new ETag.
- `304` — composition unchanged; skip download + paint.
- `204` — server hasn't rendered anything for this device yet.
- `401` — token invalid; the daemon wipes `device_token` from
  `config.toml` and exits.

### Wake loop — `POST /device/<id>/status`

Body: the same heartbeat shape as the MQTT retained `tesserae/<device_id>/status`
payload (`{state, last_paint_at, last_error, last_digest, uptime_s, fw_version,
panel, kind, panel_w, panel_h, ip}`) — battery / RSSI / wake_reason fields
are absent on Pi.

Response: `{status, config: {sleep_interval_s?}, next_poll_s?,
server_time}`. `config.sleep_interval_s` is durable (persisted to
`[rest].poll_interval_s` for future cycles), `next_poll_s` is one-shot
(only the next sleep duration). Both clamp to `[30s, 7d]`.

## Wire format (the `.bin` files)

Headerless, no magic bytes, no length prefix:

- Length is exactly `panel_w * panel_h / 2`.
- Scanline-order, two pixels per byte: high nibble is the even column, low
  nibble the odd.
- Nibble values index the Waveshare E6 palette: `0=black 1=white 2=yellow
  3=red 5=blue 6=green`. `0x4` is reserved by the firmware and is never
  written by Tesserae; the unpack helper renders it as black if it ever shows
  up.

Reference sizes:

| Panel | Pixels | Buffer |
|-------|--------|--------|
| inky_4    | 640×400   | 128 000 B |
| inky_5_7  | 600×448   | 134 400 B |
| inky_7_3  | 800×480   | 192 000 B |
| inky_13_3 | 1600×1200 | 960 000 B |

## Private-API dependency — read this before bumping `inky`

The paint path bypasses `inky`'s public `set_image(pil)` API. That method
PIL-loads our buffer and re-quantises it to the panel palette — wasted CPU,
since Tesserae has already done that work server-side.

In `inky==2.4.0`, `panel.buf` is a 2-D `numpy.uint8` array of
**one byte per pixel**; `panel.show()` flips/rotates, packs pairs of pixels
into nibbles, and calls the private `panel._update(packed_list)` to push over
SPI. We download bytes that are *already* packed nibble pairs, so we skip
`show()` entirely and call `_update()` directly. See [paint.py] for the
relevant comment.

Consequences:

- The server is responsible for baking `h_flip` / `v_flip` / `rotation` into
  the `.bin`. The client no longer applies those transforms.
- `inky` is **pinned to an exact version** in `pyproject.toml`. Pimoroni could
  rename `_update` in a minor release. If you bump the pin:
  1. Re-confirm that `panel._update(list_of_packed_ints)` still pushes the
     correct Spectra-6 init+refresh sequence.
  2. Test on real hardware — there is no software-only way to catch a
     palette-order or pack-order regression.

## Development

```bash
pip install -e '.[dev]'
pytest                                  # tests use fakes — no broker, no panel
ruff check .
mypy src/                               # strict on contract modules
```

The tests cover: unpack/pack round-trips, malformed-length rejection, config
parsing, frame-payload parsing, the message handler with a stub dispatcher,
and the dispatcher with fake download + paint. No test touches real hardware
or a real broker.

## Troubleshooting

- **`could not open inky panel` / `No EEPROM detected! You must manually
  initialise your Inky board`.** I2C is disabled. The HAT stores its panel ID
  in an EEPROM that `inky.auto()` reads over **I2C** (not SPI), so SPI alone
  isn't enough. Enable both and reboot:
  `sudo raspi-config nonint do_spi 0 && sudo raspi-config nonint do_i2c 0 && sudo reboot`
  (re-running `./scripts/install.sh` does this for you). After reboot, confirm
  the EEPROM is visible: `ls /dev/i2c-1 && sudo i2cdetect -y 1` — expect `50`
  in the grid. If there's no `50`, the board has no readable EEPROM (some
  Impression/Spectra units) and auto-detect can't identify it.
- **`Woah there, some pins we need are in use!` / `Chip Select: (line 8,
  GPIO8) currently claimed by spi0 CS0`.** The panel is detected but paint
  fails: the kernel SPI driver reserves GPIO8 as hardware CS0, while recent
  `inky` drives chip-select in software. Free the pin with the zero-chip-select
  overlay, then reboot (the installer adds this line automatically):
  ```bash
  CONFIG=/boot/firmware/config.txt; [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
  grep -q '^dtoverlay=spi0-0cs' "$CONFIG" || echo 'dtoverlay=spi0-0cs' | sudo tee -a "$CONFIG"
  sudo reboot
  ```
- **Panel stays blank, no logs.** Is the daemon running? `systemctl status
  tesserae-pi-bin-client`. Check `journalctl -u tesserae-pi-bin-client`.
- **Daemon connects but never paints (MQTT).** Confirm the broker is
  reachable from the Pi (`mosquitto_sub -h <host> -t 'tesserae/<device_id>/frame/bin'`,
  or `'tesserae/#'` to watch every device). Confirm the URL in a message is
  reachable from the Pi (`curl -I <url>`).
- **REST: `not registered yet — admin needs to click Register on the server`
  loops forever.** The daemon's `POST /device/discover` is reaching the
  server, but no one has clicked Register in Settings → Devices. Either
  click it (the next discover poll claims the token) or mint a 6-digit
  pairing code and rerun with `--pair <code>` for the strict path.
- **REST: `frame GET 401: token invalid`.** The server revoked the device or
  the local `device_token` is corrupt. The daemon wipes it from
  `config.toml` and exits — re-pair (`--pair`) or wait for the discover
  loop to re-claim.
- **REST: `frame GET 403: token not valid for this device`.** The local
  `[mqtt].device_id` no longer matches the server's canonical id (admin
  renamed the device). The discover-claim flow normally adopts the new
  id; if you got here, re-pair to refresh both id and token.
- **REST: log shows `server_time skew=...s; check NTP`.** The Pi's clock
  has drifted more than a minute from the server's. We don't `settimeofday`
  (NTP owns that on Linux); check that `chronyd`/`systemd-timesyncd` is
  running.
- **"frame is N bytes; expected M" errors.** The configured `panel.model`
  doesn't match the panel that Tesserae is rendering for. Make them agree.
- **Permission errors opening SPI or `No EEPROM detected`.** The user running
  the daemon needs membership in the `gpio`, `spi`, and `i2c` groups (`i2c`
  is required for the HAT EEPROM read that `inky.auto()` does at startup, not
  just the SPI pixel path). Re-check with `groups`, then log out and back in.
- **`--paint-test` fails to import inky.** You're not on a Pi or `inky[rpi]`
  didn't install its RPi-specific deps; that's expected off-Pi.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

[Tesserae]: https://github.com/dmellok/tesserae
[Inky Impression]: https://shop.pimoroni.com/products/inky-impression
[paint.py]: src/tesserae_pi_bin_client/paint.py
