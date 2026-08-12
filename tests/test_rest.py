"""REST transport tests — exercise the discover/register flows, frame ETag
short-circuit, status POST config-merge, and the per-status-code error paths
against a hand-rolled FakeSession (avoids pulling in `responses`/`requests-mock`).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from tesserae_pi_bin_client.button_report import ButtonReport
from tesserae_pi_bin_client.config import (
    Config,
    HttpConfig,
    LoggingConfig,
    MqttConfig,
    PanelConfig,
    RestConfig,
    parse_toml,
    save_config,
)
from tesserae_pi_bin_client.heartbeat import Status
from tesserae_pi_bin_client.transports import rest
from tesserae_pi_bin_client.transports.rest import (
    API_PREFIX,
    RestClient,
    _apply_status_response,
    _auth_headers,
    _claim_via_discover,
    _claim_via_register,
    _wake_cycle,
)

# --- test fakes ----------------------------------------------------------------


class _RaiseOnCall:
    """Sentinel queued in FakeSession to make .post/.get raise instead of return."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        if isinstance(body, dict):
            self.content = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            self.content = body
        else:
            self.content = b""

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size: int = 64 * 1024) -> Any:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class FakeSession:
    """Queue of responses keyed by (METHOD, URL). Tests assert on .calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: dict[tuple[str, str], list[FakeResponse | _RaiseOnCall]] = {}

    def queue(self, method: str, url: str, *responses: FakeResponse | _RaiseOnCall) -> None:
        self._queue.setdefault((method.upper(), url), []).extend(responses)

    def _consume(self, method: str, url: str) -> FakeResponse:
        key = (method.upper(), url)
        if key not in self._queue or not self._queue[key]:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self._queue[key].pop(0)
        if isinstance(item, _RaiseOnCall):
            raise item.exc
        return item

    def post(
        self,
        url: str,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": dict(headers or {}),
                "data": data,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        return self._consume("POST", url)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        stream: bool = False,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(headers or {}),
                # Queued by URL alone, so a button press changes `params`
                # without changing the key a test queues against.
                "params": dict(params or {}),
                "timeout": timeout,
                "allow_redirects": allow_redirects,
                "stream": stream,
            }
        )
        return self._consume("GET", url)


class FakeShutdown:
    """Drop-in for threading.Event whose .wait() returns immediately so the
    discover/register/wake loops don't block the test in real time."""

    def __init__(self, stop_after_waits: int | None = None) -> None:
        self._set = False
        self._waits = 0
        self._stop_after = stop_after_waits
        self.wait_calls: list[float | None] = []

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self._waits += 1
        self.wait_calls.append(timeout)
        if self._stop_after is not None and self._waits >= self._stop_after:
            self._set = True
        return self._set


# --- fixtures ------------------------------------------------------------------


SERVER_URL = "http://srv:8765"
BASE = SERVER_URL + API_PREFIX


def _config(
    *,
    device_id: str = "pi_bin",
    device_token: str = "",
    pairing_code: str = "",
    last_frame_etag: str = "",
    poll_interval_s: int = 60,
    tz: str = "",
) -> Config:
    return Config(
        mqtt=MqttConfig(
            host="h", port=1883, username="", password="",
            client_id="cid", keepalive=60, device_id=device_id,
        ),
        panel=PanelConfig(model="inky_4"),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
        transport_mode="rest",
        rest=RestConfig(
            server_url=SERVER_URL,
            device_token=device_token,
            pairing_code=pairing_code,
            last_frame_etag=last_frame_etag,
            poll_interval_s=poll_interval_s,
            tz=tz,
        ),
    )


def _status() -> Status:
    s = Status(panel="inky_4")
    s.panel_w, s.panel_h = 640, 400
    s.ip = "10.0.0.5"
    return s


# --- auth + URL shape ----------------------------------------------------------


def test_auth_headers_send_both_forms() -> None:
    headers = _auth_headers("TOK123")
    assert headers["Authorization"] == "Bearer TOK123"
    assert headers["X-Tesserae-Token"] == "TOK123"


def test_restclient_builds_api_v1_url() -> None:
    session = FakeSession()
    session.queue("POST", f"{BASE}/device/discover", FakeResponse(200, {"registered": False}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    client.discover({"device_id": "x"})
    assert session.calls[0]["url"] == f"{BASE}/device/discover"
    assert session.calls[0]["headers"]["Content-Type"] == "application/json"
    assert session.calls[0]["allow_redirects"] is False


# --- discover→claim ------------------------------------------------------------


def test_discover_not_registered_then_registered_claims_token(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST", f"{BASE}/device/discover",
        FakeResponse(200, {"status": "ok", "registered": False, "retry_after_s": 0}),
        FakeResponse(200, {
            "status": "ok",
            "registered": True,
            "device_token": "TOK_FROM_DISCOVER",
            "device_id": "pi_bin",
            "server_time": 1781941884.34,
        }),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)

    assert updated is not None
    assert updated.rest.device_token == "TOK_FROM_DISCOVER"
    assert updated.mqtt.device_id == "pi_bin"
    # Saved to disk.
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == "TOK_FROM_DISCOVER"


def test_discover_adopts_server_canonical_device_id(tmp_path: Path) -> None:
    """If the admin renamed the device server-side, the discover response
    returns the canonical id and we must adopt it — otherwise subsequent
    /device/<id>/frame calls 403."""
    cfg = _config(device_id="pi_bin")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST", f"{BASE}/device/discover",
        FakeResponse(200, {
            "status": "ok",
            "registered": True,
            "device_token": "TOK",
            "device_id": "pi_bin_renamed",
        }),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.mqtt.device_id == "pi_bin_renamed"
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.mqtt.device_id == "pi_bin_renamed"


def test_discover_retries_on_connection_error(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST", f"{BASE}/device/discover",
        _RaiseOnCall(requests.ConnectionError("refused")),
        FakeResponse(200, {"registered": True, "device_token": "TOK", "device_id": "pi_bin"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_discover(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.rest.device_token == "TOK"


def test_discover_shutdown_stops_loop(tmp_path: Path) -> None:
    cfg = _config()
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    # Queue infinite not-registered responses; shutdown stops it.
    for _ in range(20):
        session.queue(
            "POST", f"{BASE}/device/discover",
            FakeResponse(200, {"registered": False, "retry_after_s": 0}),
        )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    shutdown = FakeShutdown(stop_after_waits=2)

    result = _claim_via_discover(client, cfg, _status(), shutdown, cfg_path)
    assert result is None


# --- register (pairing-code path) ----------------------------------------------


def test_register_201_saves_token_and_wipes_pairing_code(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST", f"{BASE}/device/register",
        FakeResponse(201, {
            "status": "ok",
            "device_token": "PAIRED_TOK",
            "device_id": "pi_bin",
            "reused_existing": False,
        }),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    updated = _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)
    assert updated is not None
    assert updated.rest.device_token == "PAIRED_TOK"
    assert updated.rest.pairing_code == ""

    # Pairing code sent as X-Pairing-Code header (not body).
    assert session.calls[0]["headers"].get("X-Pairing-Code") == "ABC123"

    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == "PAIRED_TOK"
    assert persisted.rest.pairing_code == ""


def test_register_403_exits(tmp_path: Path) -> None:
    cfg = _config(pairing_code="STALE")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("POST", f"{BASE}/device/register", FakeResponse(403, {"error": "expired"}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    with pytest.raises(SystemExit):
        _claim_via_register(client, cfg, _status(), FakeShutdown(), cfg_path)


def test_register_429_honors_retry_after(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "POST", f"{BASE}/device/register",
        FakeResponse(429, None, headers={"Retry-After": "7"}),
        FakeResponse(201, {"device_token": "TOK", "device_id": "pi_bin"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    shutdown = FakeShutdown()

    updated = _claim_via_register(client, cfg, _status(), shutdown, cfg_path)
    assert updated is not None
    # The first wait honored Retry-After=7.
    assert 7 in shutdown.wait_calls


# --- frame GET (ETag) ----------------------------------------------------------


def test_wake_cycle_304_skips_paint_and_keeps_etag(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", last_frame_etag='"e1"')
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {"next_poll_s": 120}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    paints: list[tuple[bytes, str]] = []
    def paint_fn(packed: bytes, model: str) -> None:
        paints.append((packed, model))

    new_cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), paint_fn, cfg_path)
    assert not should_exit
    assert next_poll == 120
    assert paints == []
    # If-None-Match was sent.
    assert session.calls[0]["headers"]["If-None-Match"] == '"e1"'
    # etag unchanged on disk.
    assert new_cfg.rest.last_frame_etag == '"e1"'


def test_wake_cycle_200_paints_and_persists_new_etag(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", last_frame_etag="")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    frame_payload = b"\xaa\xbb\xcc"
    session = FakeSession()
    session.queue(
        "GET", f"{BASE}/device/pi_bin/frame",
        FakeResponse(
            200,
            {"url": "http://srv:8765/renders/abc123.bin",
             "format": "bin", "render_id": "abc123",
             "panel_w": 640, "panel_h": 400},
            headers={"ETag": '"newetag"'},
        ),
    )
    session.queue(
        "GET", "http://srv:8765/renders/abc123.bin",
        FakeResponse(200, frame_payload),
    )
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {"next_poll_s": 60}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    paints: list[tuple[bytes, str]] = []
    def paint_fn(packed: bytes, model: str) -> None:
        paints.append((packed, model))

    new_cfg, next_poll, should_exit = _wake_cycle(client, cfg, _status(), paint_fn, cfg_path)
    assert not should_exit
    assert next_poll == 60
    assert paints == [(frame_payload, "inky_4")]
    assert new_cfg.rest.last_frame_etag == '"newetag"'
    # Etag round-trips quotes verbatim through the saved config.
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.last_frame_etag == '"newetag"'


def test_wake_cycle_204_no_frame_yet_does_not_paint(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(204))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {"next_poll_s": 30}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    paints: list[Any] = []
    new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: paints.append((p, m)), cfg_path
    )
    assert not should_exit
    assert paints == []
    assert next_poll == 30


# --- error handling ------------------------------------------------------------


def test_wake_cycle_401_wipes_token_and_exits(tmp_path: Path) -> None:
    cfg = _config(device_token="STALE_TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(401))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    new_cfg, _next, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path
    )
    assert should_exit
    assert new_cfg.rest.device_token == ""
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.device_token == ""


def test_wake_cycle_403_exits_without_wiping_token(tmp_path: Path) -> None:
    """403 from /frame means token/id mismatch — typically because the local
    device_id is stale. Token isn't necessarily wrong, so we don't wipe it
    (re-pair would re-mint anyway); we just exit so the user notices."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(403))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, _next, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path
    )
    assert should_exit


def test_wake_cycle_500_falls_back_to_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=42)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(503))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path
    )
    assert not should_exit
    assert next_poll == 42


def test_wake_cycle_timeout_falls_back_to_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=77)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    session = FakeSession()
    session.queue(
        "GET", f"{BASE}/device/pi_bin/frame",
        _RaiseOnCall(requests.Timeout("read timed out")),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]
    _new_cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path
    )
    assert not should_exit
    assert next_poll == 77


# --- /status response merge ----------------------------------------------------


def test_apply_status_response_merges_sleep_interval_durably(tmp_path: Path) -> None:
    cfg = _config(poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    new_cfg, next_poll = _apply_status_response(
        cfg, {"config": {"sleep_interval_s": 300}, "next_poll_s": 300}, cfg_path
    )
    assert new_cfg.rest.poll_interval_s == 300
    assert next_poll == 300
    persisted = parse_toml(cfg_path.read_text())
    assert persisted.rest.poll_interval_s == 300


def test_apply_status_response_clamps_next_poll() -> None:
    cfg = _config(poll_interval_s=60)
    # next_poll_s under floor (30) → clamped up.
    _new_cfg, next_poll = _apply_status_response(cfg, {"next_poll_s": 5}, Path("/dev/null"))
    assert next_poll == rest.POLL_INTERVAL_MIN_S
    # next_poll_s over ceiling (week) → clamped down.
    _new_cfg, next_poll = _apply_status_response(
        cfg, {"next_poll_s": 99_999_999}, Path("/dev/null")
    )
    assert next_poll == rest.POLL_INTERVAL_MAX_S


def test_apply_status_response_ignores_out_of_range_sleep_interval(tmp_path: Path) -> None:
    """Server pushes a daft value (1 second) — the durable field stays put."""
    cfg = _config(poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)
    new_cfg, _next = _apply_status_response(
        cfg, {"config": {"sleep_interval_s": 1}}, cfg_path
    )
    assert new_cfg.rest.poll_interval_s == 60


# --- run() entry-point sanity --------------------------------------------------


def test_run_aborts_without_server_url(tmp_path: Path) -> None:
    cfg = Config(
        mqtt=MqttConfig(host="h", port=1883, username="", password="",
                        client_id="cid", keepalive=60, device_id="pi_bin"),
        panel=PanelConfig(model="inky_4"),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
        transport_mode="rest",
        rest=RestConfig(server_url=""),  # missing!
    )
    rc = rest.run(cfg, _status(), lambda p, m: None, threading.Event(), tmp_path / "x.toml")
    assert rc == 2


# --- button delivery through the wake cycle -----------------------------------


def _paint_noop(packed: bytes, model: str) -> None:
    del packed, model


def test_button_press_rides_the_frame_query(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=77)
    report.set("refresh")

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    assert session.calls[0]["params"] == {
        "button": "refresh",
        "button_event_id": "77",
    }
    # 304 acknowledged it, so /status must NOT carry it too.
    status_body = json.loads(session.calls[1]["data"])
    assert "button" not in status_body
    assert report.pending() is None


def test_unacknowledged_frame_falls_back_to_status_delivery(tmp_path: Path) -> None:
    """A 500 on the frame GET does not prove dispatch — /status delivers it."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(500))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=5)
    report.set("left")
    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    # The 500 path returns before /status, so the press is still pending.
    assert report.status_fields() == {"button": "left", "button_event_id": 5}


def test_status_fallback_body_carries_the_press(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    # Connection error on the frame GET -> not acknowledged, but the cycle
    # returns early. Drive the fallback directly through a second cycle where
    # the frame GET fails but the status POST succeeds.
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(404))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    report = ButtonReport(seed=9)
    report.set("right")
    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path, report)

    status_body = json.loads(session.calls[1]["data"])
    assert status_body["button"] == "right"
    assert status_body["button_event_id"] == 9
    # Delivered; it must not re-fire on the next cycle.
    assert report.pending() is None


def test_no_press_sends_no_button_fields(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)

    assert session.calls[0]["params"] == {}
    status_body = json.loads(session.calls[1]["data"])
    assert "button" not in status_body
    assert "button_event_id" not in status_body


# --- Retry-After + tz ---------------------------------------------------------


def test_frame_429_honours_retry_after(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=60)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_bin/frame",
        FakeResponse(429, headers={"Retry-After": "300"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, should_exit = _wake_cycle(
        client, cfg, _status(), _paint_noop, cfg_path
    )
    assert not should_exit
    assert next_poll == 300


def test_frame_429_without_header_uses_poll_interval(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=90)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(429))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, _exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert next_poll == 90


def test_frame_429_retry_after_is_clamped(tmp_path: Path) -> None:
    """A hostile header must not pin the daemon at sub-second retries."""
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET",
        f"{BASE}/device/pi_bin/frame",
        FakeResponse(429, headers={"Retry-After": "1"}),
    )
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _cfg, next_poll, _exit = _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)
    assert next_poll == rest.POLL_INTERVAL_MIN_S


def test_status_adopts_and_persists_tz(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    new_cfg, _next = _apply_status_response(
        cfg, {"config": {"tz": "Australia/Melbourne"}}, cfg_path
    )
    assert new_cfg.rest.tz == "Australia/Melbourne"
    assert parse_toml(cfg_path.read_text()).rest.tz == "Australia/Melbourne"


def test_status_body_reports_tz_and_next_sleep(tmp_path: Path) -> None:
    cfg = _config(device_token="TOK", poll_interval_s=120, tz="Australia/Perth")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", f"{BASE}/device/pi_bin/frame", FakeResponse(304))
    session.queue("POST", f"{BASE}/device/pi_bin/status", FakeResponse(200, {}))
    client = RestClient(SERVER_URL, session=session)  # type: ignore[arg-type]

    _wake_cycle(client, cfg, _status(), _paint_noop, cfg_path)

    body = json.loads(session.calls[1]["data"])
    assert body["tz"] == "Australia/Perth"
    assert body["next_sleep_s"] == 120
