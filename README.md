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
3. `usermod -aG gpio,spi $USER` — group membership for HAT access
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
                    just get added to gpio/spi)
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
tz = ""                   # auto-populated from the server's config.tz
sse_enabled = true        # hold GET /stream for instant repaints

[panel]
model = "inky_13_3"  # inky_4 | inky_5_7 | inky_7_3 | inky_13_3

[buttons]
enabled = false      # front buttons off by default
chip = ""            # gpiochip name; empty = autodetect
pin_a = 5
pin_b = 6
pin_c = 25           # 16 on the 4"/5.7"/7.3", 25 on the 13.3"
pin_d = 24
action_a = "left"    # refresh | left | right, or "" to unmap
action_b = "refresh"
action_c = "right"
action_d = ""
debounce_ms = 50

[cache]
enabled = true       # deck pages + album frames cached locally
root = ""            # empty = ~/.cache/tesserae-pi-bin-client

[relay]
relay_url = "https://relay.tesserae.ink"
pairing_code = ""    # single-use; from Settings -> Cloud relay
# install_id / device_id / device_token / frame_key / private_key /
# last_frame_etag / config_etag are written by the pairing flow.
poll_interval_s = 60

[ota]
report_enabled = true

[environment]
sensor = ""          # "" | sht4x | shtc3
i2c_bus = 1

[http]
download_timeout_s = 30
max_frame_bytes = 16000000

[logging]
level = "INFO"
```

Pass `--config /path/to/config.toml` to override.

## Transports

The client speaks one of three transports, selected by `transport_mode`:

- **`rest`** (default for fresh installs) — polls the Tesserae server's
  `/api/v1/` directly. No broker needed; one round-trip per wake cycle
  (`GET /frame` + `POST /status`). Out of the box the wake cadence is
  **every 60 s**; the server's `/status` response can push a different
  `next_poll_s` per cycle or a durable `config.sleep_interval_s`, both
  clamped to `[30s, 7d]`. With SSE enabled a push cuts that wait short.
- **`mqtt`** — subscribes to a broker and reacts to retained frame
  announcements. Stays connected; pushes are near-instant. Requires a
  broker on the LAN.
- **`relay`** — for a panel that can't reach the Tesserae server at all.
  See [Cloud relay](#cloud-relay-remote-panels) below.

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
panel, kind, panel_w, panel_h, ip}`), plus, when available, `rssi`, `tz` and
`next_sleep_s`. Each optional field is **omitted rather than zeroed** when it
has no value, so the server can tell "not reported" from "reported as none".
Battery / wake_reason fields stay absent — a mains-powered Pi has neither.

Response: `{status, config: {sleep_interval_s?, tz?}, next_poll_s?,
server_time}`. `config.sleep_interval_s` is durable (persisted to
`[rest].poll_interval_s` for future cycles), `next_poll_s` is one-shot
(only the next sleep duration). Both clamp to `[30s, 7d]`. `config.tz` is
durable too — persisted to `[rest].tz` and echoed back on every beat.

A `429` on either the frame GET or the status POST honours `Retry-After`
(numeric seconds only), clamped to the same `[30s, 7d]` range so a bad
header can't pin the daemon at sub-second retries.

### Push updates — `GET /device/<id>/stream`

Headers: `Authorization: Bearer <token>`, `Accept: text/event-stream`.

The sibling ESP32 firmware gates this behind kiosk mode, because a
deep-sleeping battery device can't hold a socket open. A mains-powered Pi
can, which makes this the one firmware feature that fits *better* here: a
`sync` event cuts the sleep short and the normal frame GET runs immediately,
so a dashboard edit lands in about a second instead of up to a full poll
interval.

Only `sync` is acted on. `values` and `patches` drive on-device overlay
rendering, which needs a partial-refresh panel the Impression doesn't have —
they're ignored by design.

The stream is strictly an optimisation: polling carries the same
information, and every failure path (404 from an older server, dropped
connection, proxy buffering) degrades to plain polling. Reconnects back off
1→60 s with ±25 % jitter, and 60 s of silence — the server sends a keepalive
comment every 25 s — forces a reconnect.

**Reverse proxies must not buffer this route.** The server sets
`X-Accel-Buffering: no`, which nginx honours; Caddy needs
`flush_interval -1`. If your proxy buffers anyway, set
`[rest].sse_enabled = false` and the client polls as before.

### Front buttons — `?button=` on the frame GET

The Impression's four front buttons are read over libgpiod (active-low with
pull-ups, falling edge) and reported to the server, which maps
`refresh` / `left` / `right` onto refresh / rotate_prev / rotate_next.

Pin numbers differ by model: **A=5, B=6, D=24 on every Impression, but C is
GPIO 16 on the 4"/5.7"/7.3" and GPIO 25 on the 13.3"** — a difference
Pimoroni documents in their getting-started guide. `install.sh` picks the
right default from the panel model you choose; `[buttons].pin_c` overrides it.

Delivery follows the firmware's discipline so one press produces exactly one
server event:

1. A press rides the **frame GET** as `?button=<name>&button_event_id=<id>`,
   so the server dispatches the action *before* composing its response — the
   frame that comes back already reflects the press.
2. `200`, `204` **or `304`** all prove the request arrived, so the press is
   cleared before the status POST. (`304` counts: "nothing changed" is still
   an acknowledgement.)
3. Anything else — network error, `429`, `5xx` — retains it, and the status
   POST delivers `{button, button_event_id}` as the fallback path. The server
   dedups on the event id, so the double path can't double-fire.

`button_event_id` is seeded randomly at start-up rather than from zero: the
server dedups against the last id it saw, so a client that restarted and
replayed `0, 1, 2, …` would have its first presses silently swallowed.

Buttons are off by default. A HAT without them, or a Pi without libgpiod,
logs a warning and keeps painting frames — failing to read a button is never
allowed to stop the panel working.

## Local frame cache (decks + offline albums)

Both features are **capability-gated**: the client advertises
`deck_cache` / `frame_cache` (with the cache filesystem's free space) on
each heartbeat, and that is what makes the server offer manifests. Turn
`[cache].enabled` off and the wire body is byte-identical to a client
that never had a cache.

**Decks.** `GET /device/<id>/deck` returns pages with 16-hex sha256
digests, byte sizes, TTLs and a button/zone link graph. Frames land in
`~/.cache/tesserae-pi-bin-client/decks/<deck_id>/`, and a button press
the graph can answer paints straight from disk — no server round trip.

A locally-served press is deliberately **not** reported as a button
action: the server saw no request, so reporting it would double-count a
press already resolved on-device. What *is* reported is
`deck_page_id` + `deck_version`, so the server's navigation state stays
truthful.

**Albums.** `GET /device/<id>/collection` returns a schema-1 album:
frames with positions, digests and URLs, plus a playback block
(`sequential`/`shuffle` × `loop`/`reshuffle`/`once`, and an
`interval_s` the client clamps to `[60s, 24h]` — e-ink takes seconds to
refresh and has a rated refresh count). Playback runs from the local
cache, so the panel keeps cycling photos while the server is down.

A one-off dashboard frame interrupts the album until its **next**
scheduled deadline, without pushing that deadline further out — else a
chatty dashboard would starve playback indefinitely.

Every cached frame is verified — exact byte length *and* digest — before
it is painted, and the writer re-hashes before storing. Any miss,
mismatch or corruption falls back to the network path silently. The
cache is an optimisation, never a requirement.

Unlike the firmware there is no 32-frame limit; that was an ESP32 memory
constraint. The server still decides what to mark cacheable from the
capacity we advertise.

## Cloud relay (remote panels)

For a panel that lives where the Tesserae server isn't reachable — a
different house, a phone hotspot, behind CGNAT. Both ends talk
**outbound** to a small Cloudflare Worker acting as a per-device
mailbox: home seals each rendered frame and `PUT`s it, this client polls
and decrypts. Nothing at home is exposed to the internet.

The relay is **zero-knowledge**. At pairing the panel and the home
instance exchange X25519 *public* keys through it and each derive the
same AES-256-GCM frame key locally; the key is never transmitted. The
relay only ever holds ciphertext, two public keys and a token hash.

```
frame_key = HKDF-SHA256(X25519(priv, their_pub),
                        info="tesserae-relay-frame-key-v1", L=32)
sealed    = nonce(12) || AES-256-GCM(frame, frame_key, nonce, aad="")
```

Setup:

```toml
transport_mode = "relay"
```

```sh
tesserae-pi-bin-client --pair CODE   # code from Settings -> Cloud relay
```

Unlike REST pairing, the relay code is **persisted** — the rendezvous
can span restarts by design, since home may take a while to complete the
exchange. The `POST /v1/pair` happens exactly once (the stored private
key is the latch): only that route enforces the code's expiry, so
re-posting later would 404 a code that is still perfectly good for
polling. Once the frame key is derived the private scalar is wiped.

Two safety rules the implementation holds to:

- **A failed GCM authentication tag is never painted.** It means the
  mailbox doesn't match this panel's key, and painting it would render
  whatever an attacker or a corrupted store supplied.
- **The decrypted length is validated against the configured panel
  geometry**, not against the relay's plaintext `X-Tesserae-*` headers —
  the relay could have altered those.

A `401` on any device-token route means the pairing was **revoked**
(a revoked device's mailbox and token record are deleted, so a freshly
paired panel with nothing published sees `204`, never `401`). The client
drops its stored pairing rather than retrying a dead token.

Buttons work differently here: a relay panel's frame GET terminates at
the relay, so the `?button=` query can't reach home. The press rides the
**status body** instead, and is repeated on *every* status post of the
wake — the relay's status slot is latest-only, so an idle beat could
otherwise overwrite a button-carrying post before home pulls it. Home
dedups on the event id, so repeating is safe.

The wire contract is `docs/relay/contract.md` in the Tesserae repo, and
it is authoritative for all three sides. Its golden vectors are pinned
in [tests/test_relay_crypto.py](tests/test_relay_crypto.py) — a
derivation that is merely *plausible* round-trips against itself while
never agreeing with home, and the only field symptom is that frames
silently never appear.

## OTA state reporting

This client **does not install anything**. What it does is report update
attempts to the server's Devices UI, using the same `ota` object the
firmware sends:

```json
{ "ota": { "schema": 1, "phase": "confirmed", "reason": "ok",
           "target_fw": "1.6.0", "attempt_id": "a3f19c" } }
```

`schema` alone means idle, and is the capability advertisement that
makes the server willing to offer an update at all. Whatever performs
the update (apt, pip, git, a systemd timer) records progress through
`OtaReporter`; state is persisted, because the transition worth having —
`pending_confirm` → `confirmed`/`rolled_back` — spans the restart it is
waiting on. Set `[ota].report_enabled = false` to stop advertising.

## Environment telemetry

Optional ambient temperature/humidity from an **SHT4x** or **SHTC3** on
the HAT's existing I²C bus (already enabled for the HAT EEPROM):

```toml
[environment]
sensor = "sht4x"
```

Reports `temperature_c`, `humidity_pct` and `env_sensor` — the same
fields the reTerminal E-Series sends. Readings are CRC-checked (a
marginal I²C bus returns plausible-looking garbage, and a wrong
temperature is harder to notice than a missing one), and a failed read
omits the fields rather than interrupting the heartbeat.

The Pi's own CPU temperature is deliberately **not** reported here: the
server's field means ambient room temperature, and a SoC running 30 °C
above the room would quietly poison anything plotted from it.

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
- **Permission errors opening SPI.** The user running the daemon needs
  membership in the `gpio` and `spi` groups. Re-check, then log out and back
  in.
- **`--paint-test` fails to import inky.** You're not on a Pi or `inky[rpi]`
  didn't install its RPi-specific deps; that's expected off-Pi.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

[Tesserae]: https://github.com/dmellok/tesserae
[Inky Impression]: https://shop.pimoroni.com/products/inky-impression
[paint.py]: src/tesserae_pi_bin_client/paint.py
