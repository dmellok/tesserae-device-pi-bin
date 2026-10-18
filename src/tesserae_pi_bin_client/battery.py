"""Optional battery telemetry from a PiSugar UPS.

A Pi has no battery of its own, so the heartbeat leaves `battery_pct` /
`battery_mv` out and the server shows the device as mains-powered. Add a
PiSugar 3 (the Zero-sized UPS, or the 3 Plus for a full-size Pi) and the
panel keeps painting through a power cut — and the server already knows what
to do with the fields: the battery tile, the history chart, the
device_battery widget and the Home Assistant battery / low-battery sensors
all key off the same two names the ESP32 firmware sends.

Two ways to read it, picked by `[battery].source`:

* `pisugar3` — straight off the I²C bus. The PiSugar 3 carries its own fuel
  gauge, so this is three register reads at a fixed address and needs
  nothing installed beyond `smbus2`, which the environment sensor already
  pulls in. The HAT EEPROM keeps I²C enabled on any Inky setup.
* `pisugar-server` — ask PiSugar's own power manager over its local TCP
  socket. Use this if the manager is installed anyway (it is what serves the
  PiSugar web UI) and you would rather one process owned the bus.

The gauge's own percentage is what gets reported, never a figure derived
from voltage: PiSugar's curve is the one its charge-protection cap (about
80 % when enabled) is measured against, and the server's linear LiPo
estimate would disagree with it.

Off by default and read best-effort, exactly like the environment sensor: a
failed or implausible read omits the fields rather than interrupting the
heartbeat, and the server's tile falls back to "Mains".
"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

log = logging.getLogger(__name__)

SOURCES = {"pisugar3", "pisugar-server"}

# PiSugar 3 I²C datasheet (docs.pisugar.com, "PiSugar3 I2C Datasheet").
# The address is configurable via register 0x50 but 0x57 is the shipped
# default and what pisugar-server assumes too.
PISUGAR3_ADDR = 0x57
# Bit 7: external power present. Bit 6: charging enabled.
_REG_POWER_STATUS = 0x02
# Big-endian millivolts across the pair: 0x10 0x1F -> 0x101F = 4127 mV.
_REG_VOLTAGE_HIGH = 0x22
_REG_VOLTAGE_LOW = 0x23
# The gauge's own 0-100 figure.
_REG_PERCENT = 0x2A

# pisugar-server's command socket. It listens on 0.0.0.0:8423 by default;
# localhost is the only sensible peer for a reading that goes into our own
# heartbeat.
PISUGAR_SERVER_ADDR = ("127.0.0.1", 8423)
_SERVER_TIMEOUT_S = 2.0

# Plausibility window for a single LiPo cell. Outside it the bytes are more
# likely a bus hiccup than a battery state, and a wrong battery figure is
# harder to notice than a missing one.
_MV_MIN = 2500
_MV_MAX = 4600


@dataclass(frozen=True)
class Reading:
    pct: int
    mv: int
    # None when the source cannot say (an older pisugar-server build).
    power_plugged: bool | None
    source: str


def _decode_pisugar3(power_status: int, v_high: int, v_low: int, pct: int) -> Reading | None:
    """Turn the four register bytes into a Reading, or None when they fail
    the plausibility check (a bus hiccup reads as 0x00 or 0xFF across the
    board, both of which land outside the voltage window)."""
    mv = ((v_high & 0xFF) << 8) | (v_low & 0xFF)
    pct = pct & 0xFF
    if not (_MV_MIN <= mv <= _MV_MAX) or pct > 100:
        log.warning(
            "battery: implausible PiSugar 3 read (%d mV, %d %%); discarding", mv, pct
        )
        return None
    return Reading(
        pct=pct,
        mv=mv,
        power_plugged=bool(power_status & 0x80),
        source="pisugar3",
    )


def _parse_server_reply(line: str, key: str) -> str | None:
    """`get battery` answers `battery: 85.5`; anything else is a miss."""
    head, sep, value = line.strip().partition(":")
    if not sep or head.strip() != key:
        return None
    return value.strip()


def _decode_server(replies: dict[str, str]) -> Reading | None:
    """Readings from a batch of pisugar-server answers keyed by field."""
    try:
        pct = int(round(float(replies["battery"])))
        mv = int(round(float(replies["battery_v"]) * 1000))
    except (KeyError, ValueError):
        return None
    if not (_MV_MIN <= mv <= _MV_MAX) or not (0 <= pct <= 100):
        log.warning(
            "battery: implausible pisugar-server read (%d mV, %d %%); discarding", mv, pct
        )
        return None
    plugged: bool | None = None
    raw_plugged = replies.get("battery_power_plugged")
    if raw_plugged in ("true", "false"):
        plugged = raw_plugged == "true"
    return Reading(pct=pct, mv=mv, power_plugged=plugged, source="pisugar-server")


class BatteryMonitor:
    """Best-effort reader. Imports of smbus2 are lazy so this module stays
    importable on a dev machine with no I²C.

    Failures are not latched the way the environment sensor's are: the
    PiSugar manager polls the same bus, and one collision should cost one
    beat, not every beat until restart. The first failure logs at warning,
    repeats at debug, and a recovery logs once so the journal shows the gap.
    """

    def __init__(self, source: str, bus_number: int = 1) -> None:
        self._source = source
        self._bus_number = bus_number
        self._failing = False

    def read(self) -> Reading | None:
        try:
            if self._source == "pisugar3":
                reading = self._read_pisugar3()
            elif self._source == "pisugar-server":
                reading = self._read_server()
            else:
                return None
        except Exception as exc:  # noqa: BLE001 - never interrupt the heartbeat
            level = logging.DEBUG if self._failing else logging.WARNING
            self._failing = True
            log.log(
                level,
                "battery: %s read failed (%s: %s); omitting the fields",
                self._source, type(exc).__name__, exc,
            )
            return None
        if reading is not None and self._failing:
            self._failing = False
            log.info("battery: %s reads again", self._source)
        return reading

    def _read_pisugar3(self) -> Reading | None:
        from smbus2 import SMBus

        with SMBus(self._bus_number) as bus:
            power_status = bus.read_byte_data(PISUGAR3_ADDR, _REG_POWER_STATUS)
            v_high = bus.read_byte_data(PISUGAR3_ADDR, _REG_VOLTAGE_HIGH)
            v_low = bus.read_byte_data(PISUGAR3_ADDR, _REG_VOLTAGE_LOW)
            pct = bus.read_byte_data(PISUGAR3_ADDR, _REG_PERCENT)
        return _decode_pisugar3(power_status, v_high, v_low, pct)

    def _read_server(self) -> Reading | None:
        replies: dict[str, str] = {}
        with socket.create_connection(PISUGAR_SERVER_ADDR, timeout=_SERVER_TIMEOUT_S) as sock:
            stream = sock.makefile("rw", encoding="utf-8", newline="\n")
            for key in ("battery", "battery_v", "battery_power_plugged"):
                stream.write(f"get {key}\n")
                stream.flush()
                value = _parse_server_reply(stream.readline(), key)
                if value is not None:
                    replies[key] = value
        return _decode_server(replies)


def payload_for(reading: Reading | None) -> dict[str, object]:
    """Heartbeat fields for a reading, or nothing at all.

    `battery_pct` and `battery_mv` are the names the server plots.
    `power_plugged` is extra: the server shows it on the device card and
    uses it to word the battery tile, and it is what tells a UPS sitting at
    100 % apart from a panel running down.
    """
    if reading is None:
        return {}
    out: dict[str, object] = {
        "battery_pct": reading.pct,
        "battery_mv": reading.mv,
        "battery_source": reading.source,
    }
    if reading.power_plugged is not None:
        out["power_plugged"] = reading.power_plugged
    return out


__all__ = [
    "PISUGAR3_ADDR",
    "PISUGAR_SERVER_ADDR",
    "SOURCES",
    "BatteryMonitor",
    "Reading",
    "payload_for",
]
