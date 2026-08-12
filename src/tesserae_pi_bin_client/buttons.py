"""Front-button watcher for the Inky Impression HATs.

The Impression carries four buttons (A-D) wired to BCM GPIO, active-low with
pull-ups, so a press is a FALLING edge. Pin numbers are board-dependent and
live in config: A=5, B=6, D=24 on every Impression, but C is GPIO 16 on the
4"/5.7"/7.3" and GPIO 25 on the 13.3" — a difference Pimoroni documents in
their getting-started guide and encodes in the `inky` examples.

We read the lines with libgpiod v2 (the same API Pimoroni's own examples use,
and what `inky[rpi]` already pulls in) rather than the deprecated sysfs or
RPi.GPIO paths. The import is deliberately lazy: this module must import
cleanly on a dev machine so the pure logic stays testable off-hardware.

Presses feed a ButtonReport, whose delivery discipline (frame-GET dispatch,
status-POST fallback, exactly-once) lives in button_report.py.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .button_report import ButtonReport
from .config import ButtonsConfig

log = logging.getLogger(__name__)

# Tried in order when [buttons].chip is empty and Pimoroni's gpiodevice helper
# isn't importable. Pi 5 routes the header through the RP1 (gpiochip4 on older
# kernels, renumbered to gpiochip0 on newer ones); Pi 4 and earlier use
# gpiochip0. Probing beats hard-coding one and failing on the other.
CHIP_CANDIDATES = ("/dev/gpiochip4", "/dev/gpiochip0")


class ButtonWatcher:
    """Background thread turning FALLING edges into ButtonReport entries.

    Failure to open the GPIO lines is logged and swallowed, never fatal: a
    panel that can't read its buttons must still paint frames.
    """

    def __init__(
        self,
        cfg: ButtonsConfig,
        report: ButtonReport,
        on_press: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._report = report
        self._on_press = on_press
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request: Any = None
        # Populated by _open_lines(); the watch loop maps edge events back to
        # BCM pins through it.
        self._offset_to_pin: dict[int, int] = {}
        # Last accepted press per pin, for the software debounce.
        self._last_edge: dict[int, float] = {}

    def start(self) -> bool:
        """Open the lines and start watching. Returns True when armed."""
        if not self._cfg.enabled:
            log.debug("buttons disabled in config")
            return False
        pin_actions = self._cfg.pin_actions()
        if not pin_actions:
            log.warning("buttons enabled but no pin is mapped to an action")
            return False
        try:
            self._request = self._open_lines(sorted(pin_actions))
        except Exception as exc:  # noqa: BLE001 - never fatal, see class docstring
            log.warning(
                "buttons: could not open GPIO lines (%s: %s); "
                "front buttons disabled for this run",
                type(exc).__name__,
                exc,
            )
            return False
        self._thread = threading.Thread(
            target=self._loop, name="tesserae-buttons", daemon=True
        )
        self._thread.start()
        log.info(
            "buttons armed: %s",
            ", ".join(f"GPIO{pin}->{action}" for pin, action in sorted(pin_actions.items())),
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None
        request = self._request
        if request is not None:
            try:
                request.release()
            except Exception:  # noqa: BLE001 - shutdown path, nothing to recover
                pass
            self._request = None

    # -- gpiod plumbing ---------------------------------------------------

    def _open_lines(self, pins: list[int]) -> Any:
        import gpiod
        from gpiod.line import Bias, Direction, Edge

        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            bias=Bias.PULL_UP,
            edge_detection=Edge.FALLING,
        )
        chip = self._open_chip(gpiod)
        # Offsets are resolved from the chip rather than assumed equal to the
        # BCM number — on the Pi 5's RP1 they are not the same thing.
        offsets = [self._offset_for(chip, pin) for pin in pins]
        self._offset_to_pin = dict(zip(offsets, pins, strict=True))
        return chip.request_lines(
            consumer="tesserae-pi-bin-client",
            config=dict.fromkeys(offsets, settings),
        )

    def _open_chip(self, gpiod: Any) -> Any:
        if self._cfg.chip:
            return gpiod.Chip(self._cfg.chip)
        try:
            import gpiodevice

            return gpiodevice.find_chip_by_platform()
        except Exception:  # noqa: BLE001 - helper is optional, fall back to probing
            pass
        last_exc: Exception | None = None
        for path in CHIP_CANDIDATES:
            try:
                return gpiod.Chip(path)
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                last_exc = exc
        raise RuntimeError(
            f"no usable gpiochip among {', '.join(CHIP_CANDIDATES)} "
            f"(last error: {last_exc}); set [buttons].chip explicitly"
        )

    @staticmethod
    def _offset_for(chip: Any, pin: int) -> int:
        """Resolve a BCM pin to this chip's line offset.

        libgpiod can look the line up by its name (`GPIO5`), which is the
        robust path; a chip that doesn't name its lines falls back to
        treating the BCM number as the offset directly.
        """
        try:
            return int(chip.line_offset_from_id(f"GPIO{pin}"))
        except Exception:  # noqa: BLE001 - unnamed lines, use the raw offset
            return pin

    # -- watch loop -------------------------------------------------------

    def _loop(self) -> None:
        request = self._request
        debounce_s = self._cfg.debounce_ms / 1000.0
        pin_actions = self._cfg.pin_actions()
        while not self._stop.is_set():
            try:
                # Bounded wait so stop() is honoured promptly even on a panel
                # nobody is touching.
                if not request.wait_edge_events(timedelta_or_none(0.5)):
                    continue
                events = request.read_edge_events()
            except Exception as exc:  # noqa: BLE001 - a bad read must not kill paint
                log.warning(
                    "buttons: edge read failed (%s: %s); stopping watcher",
                    type(exc).__name__,
                    exc,
                )
                return
            now = time.monotonic()
            for event in events:
                pin = self._offset_to_pin.get(event.line_offset)
                if pin is None:
                    continue
                if now - self._last_edge.get(pin, 0.0) < debounce_s:
                    continue
                self._last_edge[pin] = now
                action = pin_actions.get(pin)
                if not action:
                    continue
                press = self._report.set(action)
                log.info(
                    "button press: GPIO%d -> %s (event_id=%d)",
                    pin,
                    action,
                    press.event_id,
                )
                if self._on_press is not None:
                    self._on_press(action)


def timedelta_or_none(seconds: float) -> Any:
    """gpiod v2 wants a datetime.timedelta for its wait timeout."""
    from datetime import timedelta

    return timedelta(seconds=seconds)


__all__ = ["CHIP_CANDIDATES", "ButtonWatcher"]
