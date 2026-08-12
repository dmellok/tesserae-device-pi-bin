"""Optional ambient temperature / humidity telemetry over I²C.

The reTerminal E-Series boards carry an SHT4x (the PhotoPainter an SHTC3) and
report `temperature_c` / `humidity_pct` / `env_sensor` on every heartbeat. A Pi
driving an Inky HAT already has I²C enabled — the HAT EEPROM needs it — so
adding one of these sensors is a two-wire job and the server already knows what
to do with the fields.

Off by default and read best-effort: a failed read omits the fields rather than
interrupting the heartbeat, exactly as the firmware does. Ambient readings are
nice to have; a frame cycle is not something to risk for them.

Deliberately *not* reported here: the Pi's own CPU temperature. The server's
field means ambient room temperature, and a SoC running 30 °C above the room
would quietly poison anything plotted from it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Both parts sit at a fixed address; they cannot be moved, so there is nothing
# to configure beyond which chip is present.
SHT4X_ADDR = 0x44
SHTC3_ADDR = 0x70

SENSORS = {"sht4x", "shtc3"}

# SHT4x: measure with high repeatability, no heater.
_SHT4X_MEASURE = 0xFD
# SHTC3: wake, then measure T-first with clock stretching disabled.
_SHTC3_WAKE = (0x35, 0x17)
_SHTC3_MEASURE = (0x78, 0x66)
_SHTC3_SLEEP = (0xB0, 0x98)


@dataclass(frozen=True)
class Reading:
    temperature_c: float
    humidity_pct: float
    sensor: str


def _crc8(data: bytes) -> int:
    """Sensirion's CRC-8 (poly 0x31, init 0xFF).

    Worth doing rather than trusting the bytes: a marginal I²C bus returns
    plausible-looking garbage, and a wrong temperature is harder to notice
    than a missing one.
    """
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _decode(raw: bytes) -> tuple[float, float] | None:
    """Sensirion's shared 6-byte frame: T msb/lsb/crc, RH msb/lsb/crc."""
    if len(raw) != 6:
        return None
    if _crc8(raw[0:2]) != raw[2] or _crc8(raw[3:5]) != raw[5]:
        log.warning("environment: sensor CRC mismatch; discarding reading")
        return None
    t_ticks = (raw[0] << 8) | raw[1]
    rh_ticks = (raw[3] << 8) | raw[4]
    temperature = -45.0 + 175.0 * (t_ticks / 65535.0)
    humidity = -6.0 + 125.0 * (rh_ticks / 65535.0)
    # The conversion can overshoot slightly at the rails; the datasheet says
    # to clamp rather than report an impossible humidity.
    humidity = max(0.0, min(100.0, humidity))
    return temperature, humidity


class EnvironmentSensor:
    """Best-effort reader. Import of smbus2 is lazy so this module stays
    importable on a dev machine with no I²C."""

    def __init__(self, sensor: str, bus_number: int = 1) -> None:
        self._sensor = sensor
        self._bus_number = bus_number
        self._failed = False

    def read(self) -> Reading | None:
        if self._failed:
            return None
        try:
            if self._sensor == "sht4x":
                raw = self._read_sht4x()
            elif self._sensor == "shtc3":
                raw = self._read_shtc3()
            else:
                return None
        except Exception as exc:  # noqa: BLE001 - never interrupt the heartbeat
            # Latched: a missing sensor should log once, not once per beat.
            self._failed = True
            log.warning(
                "environment: %s read failed (%s: %s); omitting the fields",
                self._sensor, type(exc).__name__, exc,
            )
            return None
        decoded = _decode(raw)
        if decoded is None:
            return None
        return Reading(decoded[0], decoded[1], self._sensor)

    def _read_sht4x(self) -> bytes:
        from smbus2 import SMBus, i2c_msg

        with SMBus(self._bus_number) as bus:
            bus.i2c_rdwr(i2c_msg.write(SHT4X_ADDR, [_SHT4X_MEASURE]))
            time.sleep(0.01)  # high-repeatability conversion time
            read = i2c_msg.read(SHT4X_ADDR, 6)
            bus.i2c_rdwr(read)
            return bytes(read)

    def _read_shtc3(self) -> bytes:
        from smbus2 import SMBus, i2c_msg

        with SMBus(self._bus_number) as bus:
            bus.i2c_rdwr(i2c_msg.write(SHTC3_ADDR, list(_SHTC3_WAKE)))
            time.sleep(0.001)
            bus.i2c_rdwr(i2c_msg.write(SHTC3_ADDR, list(_SHTC3_MEASURE)))
            time.sleep(0.013)
            read = i2c_msg.read(SHTC3_ADDR, 6)
            bus.i2c_rdwr(read)
            bus.i2c_rdwr(i2c_msg.write(SHTC3_ADDR, list(_SHTC3_SLEEP)))
            return bytes(read)


def payload_for(reading: Reading | None) -> dict[str, object]:
    """Heartbeat fields for a reading, or nothing at all."""
    if reading is None:
        return {}
    return {
        "temperature_c": round(reading.temperature_c, 1),
        "humidity_pct": round(reading.humidity_pct, 1),
        "env_sensor": reading.sensor,
    }


__all__ = [
    "SENSORS",
    "SHT4X_ADDR",
    "SHTC3_ADDR",
    "EnvironmentSensor",
    "Reading",
    "payload_for",
]
