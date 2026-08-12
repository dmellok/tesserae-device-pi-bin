from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    Config,
    load_config,
    save_config,
    with_relay_updates,
    with_rest_updates,
)
from .heartbeat import Status, _primary_ip
from .paint import auto_panel, detected_model_or, paint
from .panels import PANEL_DIMS, buffer_size
from .unpack import pack

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _stripe_test_pattern(model: str) -> bytes:
    """Six vertical stripes in the panel's six display colours.

    Used by --paint-test to verify the SPI path and panel orientation
    without needing an MQTT broker or a Tesserae server.
    """
    width, height = PANEL_DIMS[model]
    stripe_nibbles = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]  # skip reserved 0x4
    pixels: list[int] = []
    stripe_w = width // len(stripe_nibbles)
    for _y in range(height):
        for x in range(width):
            band = min(x // stripe_w, len(stripe_nibbles) - 1)
            pixels.append(stripe_nibbles[band])
    return pack(pixels, width, height)


def _detect_panel() -> Any:
    """Open the inky panel, raising a clear error if SPI/I2C/HAT is misconfigured.

    Even though the panel size is configured in config.toml, we still need a
    live inky instance to paint with, and inky.auto() reads the HAT EEPROM over
    I2C to build it. The most common bring-up failure is I2C being disabled.
    """
    try:
        return auto_panel()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "could not open inky panel: "
            f"{type(exc).__name__}: {exc}\n"
            "Troubleshooting:\n"
            "  1. enable BOTH interfaces: raspi-config -> Interface Options ->\n"
            "     SPI -> enable, and I2C -> enable. I2C is how the HAT EEPROM is\n"
            "     read; without it you get 'No EEPROM detected'.\n"
            "  2. reboot after enabling SPI/I2C, then check the EEPROM is visible:\n"
            "     ls /dev/i2c-1 && sudo i2cdetect -y 1   (expect '50' in the grid)\n"
            "  3. the user running the service must be in the 'gpio' and 'spi'\n"
            "     groups (re-run scripts/install.sh, then log out + back in)\n"
            "  4. if i2cdetect shows no '50', the board has no readable EEPROM and\n"
            "     inky.auto() cannot identify it (some Impression/Spectra units)"
        ) from exc


def _do_paint_test(config: Config) -> int:
    model = config.panel.model
    expected = buffer_size(model)
    log.info("building stripe pattern for %s (%d bytes)", model, expected)
    buf = _stripe_test_pattern(model)
    panel = _detect_panel()
    detected = detected_model_or(panel, model)
    if detected != model:
        log.warning(
            "configured model=%s but detected=%s; painting configured size",
            model,
            detected,
        )
    log.info("pushing %d bytes to panel via _update()", len(buf))
    paint(panel, buf, model)
    log.info("paint-test complete")
    return 0


def _build_status_and_painter(config: Config) -> tuple[Status, Any, Any]:
    """Shared by both transports: detect the panel, pre-fill discovery
    fields on Status, return (status, panel, paint_fn)."""
    status = Status(panel=config.panel.model)
    panel = _detect_panel()
    status.panel = detected_model_or(panel, config.panel.model)
    # PANEL_DIMS[name] is (W, H) in the orientation Tesserae renders into and
    # the orientation inky.show() consumes after applying the per-driver
    # rotation — i.e. what the user actually sees on the panel. Surface those
    # in the heartbeat / discovery body so the server pre-fills correctly.
    status.panel_w, status.panel_h = PANEL_DIMS[status.panel]
    # Resolved once at startup — neither value changes between cycles.
    status.ip = _primary_ip()

    def paint_fn(packed: bytes, model: str) -> None:
        paint(panel, packed, model)

    return status, panel, paint_fn


def _do_run(config: Config, config_path: Path) -> int:
    """Dispatch to the configured transport's wake loop."""
    status, _panel, paint_fn = _build_status_and_painter(config)

    shutdown = threading.Event()

    def _signal_handler(signum: int, _frame: Any) -> None:
        log.info("signal %d received; shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if config.transport_mode == "rest":
        log.info("transport_mode=rest; using HTTP polling against %s", config.rest.server_url)
        from .transports import rest

        return rest.run(config, status, paint_fn, shutdown, config_path)

    if config.transport_mode == "relay":
        log.info(
            "transport_mode=relay; polling the cloud mailbox at %s",
            config.relay.relay_url,
        )
        from .transports import relay

        return relay.run(config, status, paint_fn, shutdown, config_path)

    log.info("transport_mode=mqtt; using MQTT broker at %s:%d", config.mqtt.host, config.mqtt.port)
    from .transports import mqtt

    return mqtt.run(config, status, paint_fn, shutdown, config_path)


def _apply_pair_flag(config: Config, pair_code: str, config_path: Path) -> Config:
    """In-memory override: --pair beats whatever pairing_code was in config.

    The new code is NOT persisted by this function — the REST claim flow
    persists it implicitly by wiping it once the resulting device_token is
    saved. If the user pre-empts the daemon (Ctrl-C before pairing
    completes), the next run starts fresh and they re-supply --pair.
    """
    if config.transport_mode == "relay":
        return _apply_relay_pair_flag(config, pair_code, config_path)
    if config.transport_mode != "rest":
        raise SystemExit(
            "--pair only applies when transport_mode = 'rest' or 'relay' "
            f"(current: {config.transport_mode!r})"
        )
    if config.rest.device_token:
        log.warning(
            "--pair ignored: a device_token is already saved at %s. "
            "Wipe it (set device_token = \"\" in [rest]) and re-run to re-pair.",
            config_path,
        )
        return config
    if config.rest.pairing_code and config.rest.pairing_code != pair_code:
        log.info("--pair overrides config.rest.pairing_code for this run")
    return with_rest_updates(config, pairing_code=pair_code)


def _apply_relay_pair_flag(config: Config, pair_code: str, config_path: Path) -> Config:
    """--pair for the relay transport.

    Persisted, unlike the REST path: relay pairing spans restarts by design
    (the code has no expiry once submitted, and home may take a while to
    complete the exchange), so the code has to survive a reboot mid-pairing.

    A code supplied while a different pairing is already half-done clears the
    stored private key, since that key is bound to the code it was submitted
    with — reusing it against a new code would derive a frame key home never
    agrees with, and the only symptom would be frames that never appear.
    """
    if config.relay.frame_key:
        log.warning(
            "--pair ignored: this panel is already relay-paired (frame_key is "
            "set in %s). Clear [relay].frame_key and re-run to re-pair.",
            config_path,
        )
        return config
    if config.relay.private_key and config.relay.pairing_code != pair_code:
        log.info("--pair supplies a new code; discarding the half-done pairing")
        config = with_relay_updates(config, private_key="")
    config = with_relay_updates(config, pairing_code=pair_code)
    save_config(config, config_path)
    return config


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tesserae-pi-bin-client",
        description="Subscribe to a Tesserae server and paint 4-bpp .bin frames "
        "onto an Inky Impression.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--paint-test",
        action="store_true",
        help="paint a six-colour stripe pattern and exit (no MQTT/REST)",
    )
    parser.add_argument(
        "--pair",
        metavar="CODE",
        default=None,
        help="Pair this panel. In rest mode: present the code to the server on "
        "first run (overrides [rest].pairing_code; ignored once a device_token "
        "is saved). In relay mode: start the cloud rendezvous with a code from "
        "Settings -> Cloud relay (persisted, since pairing can span restarts).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    _setup_logging(config.logging.level)

    if args.paint_test:
        return _do_paint_test(config)
    if args.pair is not None:
        config = _apply_pair_flag(config, args.pair, args.config)
    return _do_run(config, args.config)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
