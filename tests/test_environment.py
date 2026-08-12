"""Ambient sensor decoding. The CRC check is the point: a marginal I²C bus
returns plausible-looking garbage, and a wrong temperature is much harder to
notice than a missing one.
"""
from __future__ import annotations

from typing import Any

from tesserae_pi_bin_client.environment import (
    EnvironmentSensor,
    Reading,
    _crc8,
    _decode,
    payload_for,
)


def _framed(t_ticks: int, rh_ticks: int) -> bytes:
    t = t_ticks.to_bytes(2, "big")
    rh = rh_ticks.to_bytes(2, "big")
    return t + bytes([_crc8(t)]) + rh + bytes([_crc8(rh)])


def test_crc8_matches_the_sensirion_check_value() -> None:
    """The datasheet's worked example: CRC(0xBEEF) == 0x92."""
    assert _crc8(bytes([0xBE, 0xEF])) == 0x92


def test_decode_midscale() -> None:
    decoded = _decode(_framed(0x8000, 0x8000))
    assert decoded is not None
    temperature, humidity = decoded
    assert abs(temperature - 42.5) < 0.1
    assert abs(humidity - 56.5) < 0.1


def test_decode_rejects_a_bad_crc() -> None:
    frame = bytearray(_framed(0x8000, 0x8000))
    frame[2] ^= 0xFF
    assert _decode(bytes(frame)) is None


def test_decode_rejects_a_short_frame() -> None:
    assert _decode(b"\x00\x01\x02") is None


def test_humidity_is_clamped_to_the_real_range() -> None:
    """The conversion overshoots slightly at the rails."""
    decoded = _decode(_framed(0xFFFF, 0xFFFF))
    assert decoded is not None
    assert decoded[1] == 100.0

    decoded_low = _decode(_framed(0x0000, 0x0000))
    assert decoded_low is not None
    assert decoded_low[1] == 0.0


def test_payload_shape_matches_the_firmware() -> None:
    payload = payload_for(Reading(21.34, 55.67, "sht4x"))
    assert payload == {
        "temperature_c": 21.3,
        "humidity_pct": 55.7,
        "env_sensor": "sht4x",
    }


def test_no_reading_contributes_no_fields() -> None:
    """A failed read must omit the fields, not report zeros."""
    assert payload_for(None) == {}


def test_read_failure_is_latched_and_never_raises(monkeypatch: Any) -> None:
    """A missing sensor should log once, not once per heartbeat — and must
    never interrupt the frame cycle."""
    sensor = EnvironmentSensor("sht4x")
    calls: list[int] = []

    def boom(self: Any) -> bytes:
        calls.append(1)
        raise OSError("no such device")

    monkeypatch.setattr(EnvironmentSensor, "_read_sht4x", boom)
    assert sensor.read() is None
    assert sensor.read() is None
    assert len(calls) == 1


def test_unknown_sensor_reads_nothing() -> None:
    assert EnvironmentSensor("thermocouple").read() is None


def test_successful_read_returns_a_reading(monkeypatch: Any) -> None:
    sensor = EnvironmentSensor("sht4x")
    monkeypatch.setattr(
        EnvironmentSensor, "_read_sht4x", lambda self: _framed(0x8000, 0x8000)
    )
    reading = sensor.read()
    assert reading is not None
    assert reading.sensor == "sht4x"
    assert abs(reading.temperature_c - 42.5) < 0.1
