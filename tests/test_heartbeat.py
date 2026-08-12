from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tesserae_pi_bin_client.heartbeat import (
    KIND,
    OFFLINE_WILL_PAYLOAD,
    STATUS_TOPIC_LEGACY,
    Heartbeat,
    Status,
    _fw_version,
    _wifi_rssi,
    status_topic,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes, int, bool]] = []

    def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any:
        self.publishes.append((topic, payload, qos, retain))


def test_status_topic_builds_per_device_topic() -> None:
    assert status_topic("pi") == STATUS_TOPIC_LEGACY
    assert status_topic("pi_kitchen") == "tesserae/pi_kitchen/status"


def test_publish_now_emits_retained_status_on_legacy_prefix() -> None:
    status = Status(panel="inky_4")
    pub = RecordingPublisher()
    hb = Heartbeat(
        status=status, publisher=pub, status_topic=STATUS_TOPIC_LEGACY, interval=999
    )
    hb.publish_now()
    assert len(pub.publishes) == 1
    topic, payload, qos, retain = pub.publishes[0]
    assert topic == STATUS_TOPIC_LEGACY
    assert qos == 1 and retain is True
    parsed = json.loads(payload.decode())
    assert parsed["state"] == "idle"
    assert parsed["panel"] == "inky_4"
    assert parsed["fw_version"]


def test_publish_now_emits_retained_status_on_custom_prefix() -> None:
    status = Status(panel="inky_4")
    pub = RecordingPublisher()
    custom = status_topic("pi_kitchen")
    hb = Heartbeat(status=status, publisher=pub, status_topic=custom, interval=999)
    hb.publish_now()
    assert len(pub.publishes) == 1
    topic, _payload, _qos, _retain = pub.publishes[0]
    assert topic == "tesserae/pi_kitchen/status"


def test_publish_offline_emits_will_payload() -> None:
    status = Status(panel="inky_4")
    pub = RecordingPublisher()
    hb = Heartbeat(
        status=status, publisher=pub, status_topic=STATUS_TOPIC_LEGACY, interval=999
    )
    hb.publish_offline()
    assert len(pub.publishes) == 1
    topic, payload, qos, retain = pub.publishes[0]
    assert topic == STATUS_TOPIC_LEGACY
    assert payload == OFFLINE_WILL_PAYLOAD
    assert retain is True


def test_status_payload_includes_required_fields() -> None:
    status = Status(panel="inky_13_3")
    status.last_digest = "abcd"
    status.last_error = None
    payload = status.payload()
    for key in (
        "state",
        "last_paint_at",
        "last_error",
        "last_digest",
        "uptime_s",
        "fw_version",
        "panel",
    ):
        assert key in payload
    assert payload["panel"] == "inky_13_3"
    assert payload["last_digest"] == "abcd"


def test_status_payload_includes_discovery_fields() -> None:
    # Drives Tesserae's Settings → Devices one-click Register pre-fill.
    status = Status(
        panel="inky_13_3", panel_w=1600, panel_h=1200, ip="192.168.1.42"
    )
    raw = status.to_json()
    parsed = json.loads(raw.decode())
    assert parsed["kind"] == KIND == "pi_bin_client"
    assert parsed["panel_w"] == 1600
    assert parsed["panel_h"] == 1200
    assert parsed["ip"] == "192.168.1.42"
    # fw_version is populated by the default_factory; must be non-empty so
    # the server doesn't show a blank firmware chip on the discovered row.
    assert isinstance(parsed["fw_version"], str) and parsed["fw_version"]


# --- telemetry fields added for firmware parity --------------------------------


def test_optional_telemetry_omitted_when_unset() -> None:
    """Omitted, not zeroed: the server must be able to tell "not reported"
    from "reported as none"."""
    payload = Status(panel="inky_13_3").payload()
    for key in ("rssi", "tz", "next_sleep_s", "button", "button_event_id"):
        assert key not in payload


def test_telemetry_included_when_set() -> None:
    status = Status(panel="inky_13_3")
    status.rssi = -56
    status.tz = "Australia/Melbourne"
    status.next_sleep_s = 300
    payload = status.payload()
    assert payload["rssi"] == -56
    assert payload["tz"] == "Australia/Melbourne"
    assert payload["next_sleep_s"] == 300


def test_button_needs_both_name_and_event_id() -> None:
    """A half-populated press would be dropped server-side anyway; not
    sending it keeps the body honest."""
    status = Status(panel="inky_13_3")
    status.button = "refresh"
    assert "button" not in status.payload()
    status.button_event_id = 12
    assert status.payload()["button_event_id"] == 12


def test_fw_version_is_plain_semver() -> None:
    """A PEP 440 local segment (+unknown) does not parse as SemVer, so the
    server's update comparison would reject it."""
    assert "+" not in _fw_version()


def test_wifi_rssi_parses_proc_net_wireless(tmp_path: Path, monkeypatch: Any) -> None:
    proc = tmp_path / "wireless"
    proc.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        " wlan0: 0000   54.  -56.  -256        0      0      0\n",
        encoding="utf-8",
    )
    real_open = open

    def fake_open(path: Any, *a: Any, **kw: Any) -> Any:
        if path == "/proc/net/wireless":
            return real_open(proc, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert _wifi_rssi() == -56


def test_wifi_rssi_none_without_wireless_interface(monkeypatch: Any) -> None:
    def fake_open(path: Any, *a: Any, **kw: Any) -> Any:
        raise OSError("no such file")

    monkeypatch.setattr("builtins.open", fake_open)
    assert _wifi_rssi() is None
