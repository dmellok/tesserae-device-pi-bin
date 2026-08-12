from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

# Default-prefix topic kept around for tests that want to assert against
# the back-compat shape. Production callers MUST derive their topic from
# config via status_topic(device_id) — main.py wires this through.
STATUS_TOPIC_LEGACY = "tesserae/pi/status"
OFFLINE_WILL_PAYLOAD = json.dumps({"state": "offline"}).encode("utf-8")
HEARTBEAT_INTERVAL_S = 60.0

# Discovery hint Tesserae's Settings → Devices uses to pre-fill the kind chip
# when this client publishes from an unregistered device id.
KIND = "pi_bin_client"


def status_topic(device_id: str) -> str:
    """Build the per-device retained-status topic."""
    return f"tesserae/{device_id}/status"


def _fw_version() -> str:
    """Resolve the installed package version as plain SemVer.

    The server compares this against the latest available build to decide
    whether an update can be offered, so it must parse as SemVer — a PEP 440
    local-version segment (the `+unknown` we used to emit when running from a
    source checkout) does not. Strip anything after `+`, matching the sibling
    firmware, which reports the release tag with the leading `v` removed.
    """
    try:
        raw = version("tesserae-pi-bin-client")
    except PackageNotFoundError:
        return "0.0.0"
    return raw.split("+", 1)[0]


def _wifi_rssi() -> int | None:
    """Best-effort WiFi signal level in dBm, or None when not on WiFi.

    Read from /proc/net/wireless (column 4, "level"), which the kernel keeps
    for every associated wireless interface. A wired Pi has no such interface
    and gets None — the field is then omitted from the heartbeat rather than
    reported as a misleading 0.
    """
    try:
        with open("/proc/net/wireless", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    # Two header lines, then one row per interface: "wlan0: 0000 54. -56. ..."
    for line in lines[2:]:
        _, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            return int(float(fields[2].rstrip(".")))
        except ValueError:
            continue
    return None


def _primary_ip() -> str:
    """Best-effort: the IP the OS would use for an outbound packet.

    Opens a UDP socket against a well-known address — no packets are
    actually sent; the kernel just resolves the source IP for the route.
    Returns empty string if no network is configured.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        return ""


class Publisher(Protocol):
    def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> Any: ...


@dataclass
class Status:
    state: str = "idle"
    last_paint_at: float | None = None
    last_error: str | None = None
    last_digest: str | None = None
    panel: str = "unknown"
    started_at: float = field(default_factory=time.time)
    # Discovery fields read by Tesserae's Settings → Devices to pre-fill the
    # one-click Register form. Tesserae merges these with whatever it already
    # knows about the device id, so extra keys here are forward-compatible.
    kind: str = KIND
    panel_w: int = 0
    panel_h: int = 0
    fw_version: str = field(default_factory=_fw_version)
    ip: str = ""
    # Telemetry the sibling firmware reports on every beat. Each is omitted
    # from the payload when unset rather than sent as a zero/empty sentinel,
    # so the server can tell "not reported" from "reported as none".
    rssi: int | None = None
    tz: str = ""
    # The interval this client intends to sleep for after the current beat.
    # Lets the server predict the next contact without inferring it from the
    # cadence it pushed.
    next_sleep_s: int | None = None
    # Pending physical-button press, delivered here only when the frame GET
    # could not carry it (see button_report.BUTTON_REPORT_FALLBACK_NOTE).
    button: str = ""
    button_event_id: int | None = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "state": self.state,
            "last_paint_at": self.last_paint_at,
            "last_error": self.last_error,
            "last_digest": self.last_digest,
            "uptime_s": time.time() - self.started_at,
            "fw_version": self.fw_version,
            "panel": self.panel,
            "kind": self.kind,
            "panel_w": self.panel_w,
            "panel_h": self.panel_h,
            "ip": self.ip,
        }
        if self.rssi is not None:
            body["rssi"] = self.rssi
        if self.tz:
            body["tz"] = self.tz
        if self.next_sleep_s is not None:
            body["next_sleep_s"] = self.next_sleep_s
        if self.button and self.button_event_id is not None:
            body["button"] = self.button
            body["button_event_id"] = self.button_event_id
        return body

    def to_json(self) -> bytes:
        return json.dumps(self.payload()).encode("utf-8")


def _ignore_status(s: Status) -> None:  # pragma: no cover - default no-op
    del s


class Heartbeat:
    """Background thread that re-publishes Status retained every interval.

    External callers (mqtt_loop / dispatcher) mutate the Status object and
    call kick() to flush immediately after a state change. The thread loop
    flushes on its own at least every HEARTBEAT_INTERVAL_S so the broker
    sees a fresh retained message even during long idle periods.
    """

    def __init__(
        self,
        status: Status,
        publisher: Publisher,
        status_topic: str,
        interval: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._status = status
        self._publisher = publisher
        self._interval = interval
        self._topic = status_topic
        self._stop_event = threading.Event()
        self._kick_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="tesserae-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._kick_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def publish_now(self) -> None:
        with self._lock:
            self._publisher.publish(
                self._topic, self._status.to_json(), qos=1, retain=True
            )

    def publish_offline(self) -> None:
        with self._lock:
            self._publisher.publish(
                self._topic, OFFLINE_WILL_PAYLOAD, qos=1, retain=True
            )

    def kick(self) -> None:
        self._kick_event.set()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.publish_now()
            self._kick_event.wait(timeout=self._interval)
            self._kick_event.clear()


def status_summary(status: Status) -> str:
    """Compact rendering for logs."""
    payload = status.payload()
    return (
        f"state={payload['state']} "
        f"digest={payload['last_digest']} "
        f"err={payload['last_error']} "
        f"uptime={payload['uptime_s']:.0f}s"
    )


# Re-export dataclass helpers for tests
__all__ = [
    "KIND",
    "STATUS_TOPIC_LEGACY",
    "OFFLINE_WILL_PAYLOAD",
    "HEARTBEAT_INTERVAL_S",
    "Status",
    "Heartbeat",
    "Publisher",
    "status_summary",
    "status_topic",
    "asdict",
]
