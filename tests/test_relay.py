"""Cloud-relay transport: the pairing state machine and the poll cycle.

The rules under test are the ones whose failure mode is silent rather than
loud — a re-POSTed pairing code, a painted frame that failed its auth tag, a
button press lost to the latest-only status slot.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tesserae_pi_bin_client.button_report import ButtonReport
from tesserae_pi_bin_client.config import (
    ButtonsConfig,
    Config,
    HttpConfig,
    LoggingConfig,
    MqttConfig,
    PanelConfig,
    RelayConfig,
    parse_toml,
    save_config,
)
from tesserae_pi_bin_client.heartbeat import Status
from tesserae_pi_bin_client.panels import buffer_size
from tesserae_pi_bin_client.relay_crypto import (
    b64url_encode,
    derive_frame_key,
    public_key_for,
    seal,
)
from tesserae_pi_bin_client.transports import relay
from tesserae_pi_bin_client.transports.relay import (
    USER_AGENT,
    RelayClient,
    _pair,
    _wake_cycle,
)

RELAY_URL = "https://relay.example"
HOME_PRIV = bytes([0x01]) * 32
PANEL_MODEL = "inky_4"

# --- fakes ---------------------------------------------------------------------


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


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: dict[tuple[str, str], list[FakeResponse]] = {}

    def queue(self, method: str, url: str, *responses: FakeResponse) -> None:
        self._queue.setdefault((method.upper(), url), []).extend(responses)

    def _consume(self, method: str, url: str) -> FakeResponse:
        key = (method.upper(), url)
        if key not in self._queue or not self._queue[key]:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self._queue[key].pop(0)

    def post(self, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kw})
        return self._consume("POST", url)

    def get(self, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kw})
        return self._consume("GET", url)


class FakeShutdown:
    def __init__(self, stop_after_waits: int | None = None) -> None:
        self._set = False
        self._waits = 0
        self._stop_after = stop_after_waits

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        self._waits += 1
        if self._stop_after is not None and self._waits >= self._stop_after:
            self._set = True
        return self._set


def _config(**relay_fields: Any) -> Config:
    defaults: dict[str, Any] = {
        "relay_url": RELAY_URL,
        "poll_interval_s": 60,
    }
    defaults.update(relay_fields)
    return Config(
        mqtt=MqttConfig(
            host="h", port=1883, username="", password="",
            client_id="cid", keepalive=60, device_id="pi_bin",
        ),
        panel=PanelConfig(model=PANEL_MODEL),
        http=HttpConfig(download_timeout_s=5, max_frame_bytes=10_000_000),
        logging=LoggingConfig(level="INFO"),
        transport_mode="relay",
        buttons=ButtonsConfig(),
        relay=RelayConfig(**defaults),
    )


def _status() -> Status:
    s = Status(panel=PANEL_MODEL)
    s.panel_w, s.panel_h = 640, 400
    return s


def _paired_config(frame_key: bytes, **extra: Any) -> Config:
    return _config(
        install_id="inst1",
        device_id="dev1",
        device_token="TOK",
        frame_key=b64url_encode(frame_key),
        **extra,
    )


def _frame_url() -> str:
    return f"{RELAY_URL}/v1/i/inst1/d/dev1/frame"


def _status_url() -> str:
    return f"{RELAY_URL}/v1/i/inst1/d/dev1/status"


# --- pairing -------------------------------------------------------------------


def test_pairing_submits_then_polls_to_ready(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("POST", f"{RELAY_URL}/v1/pair", FakeResponse(202, {"status": "pending"}))
    session.queue(
        "GET",
        f"{RELAY_URL}/v1/pair/ABC123",
        FakeResponse(200, {"status": "pending"}),
        FakeResponse(200, {
            "status": "ready",
            "install_id": "inst1",
            "device_id": "dev1",
            "device_token": "TOK",
            "home_pubkey": b64url_encode(public_key_for(HOME_PRIV)),
            "config": {"sleep_interval_s": 300},
        }),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    out = _pair(client, cfg, _status(), FakeShutdown(), cfg_path)  # type: ignore[arg-type]
    assert out is not None
    assert out.relay.install_id == "inst1"
    assert out.relay.device_token == "TOK"
    # The private scalar has done its only job and must be gone.
    assert out.relay.private_key == ""
    # The code is single-use; nothing should re-submit it.
    assert out.relay.pairing_code == ""
    # Pairing-time config was applied.
    assert out.relay.poll_interval_s == 300


def test_derived_key_matches_what_home_would_derive(tmp_path: Path) -> None:
    """The whole point: neither side ever transmits the key."""
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("POST", f"{RELAY_URL}/v1/pair", FakeResponse(202, {}))
    session.queue(
        "GET",
        f"{RELAY_URL}/v1/pair/ABC123",
        FakeResponse(200, {
            "status": "ready",
            "install_id": "inst1",
            "device_id": "dev1",
            "device_token": "TOK",
            "home_pubkey": b64url_encode(public_key_for(HOME_PRIV)),
        }),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]
    out = _pair(client, cfg, _status(), FakeShutdown(), cfg_path)  # type: ignore[arg-type]
    assert out is not None

    panel_pub_b64 = json.loads(session.calls[0]["data"])["panel_pubkey"]
    from tesserae_pi_bin_client.relay_crypto import b64url_decode

    home_derived = derive_frame_key(HOME_PRIV, b64url_decode(panel_pub_b64))
    assert b64url_encode(home_derived) == out.relay.frame_key


def test_pairing_post_happens_exactly_once_across_restarts(tmp_path: Path) -> None:
    """Only the POST enforces the code's expiry, so re-POSTing later would 404
    a code that is still perfectly good for polling."""
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("POST", f"{RELAY_URL}/v1/pair", FakeResponse(202, {}))
    session.queue("GET", f"{RELAY_URL}/v1/pair/ABC123", FakeResponse(200, {"status": "pending"}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    # First run: submits, then gives up waiting (simulating a restart).
    _pair(client, cfg, _status(), FakeShutdown(stop_after_waits=1), cfg_path)  # type: ignore[arg-type]
    resumed = parse_toml(cfg_path.read_text())
    assert resumed.relay.private_key != ""

    # Second run: must NOT post again — only poll.
    session2 = FakeSession()
    session2.queue(
        "GET",
        f"{RELAY_URL}/v1/pair/ABC123",
        FakeResponse(200, {
            "status": "ready",
            "install_id": "inst1",
            "device_id": "dev1",
            "device_token": "TOK",
            "home_pubkey": b64url_encode(public_key_for(HOME_PRIV)),
        }),
    )
    client2 = RelayClient(RELAY_URL, session=session2)  # type: ignore[arg-type]
    out = _pair(client2, resumed, _status(), FakeShutdown(), cfg_path)  # type: ignore[arg-type]
    assert out is not None
    assert all(c["method"] == "GET" for c in session2.calls)


def test_expired_code_on_submit_gives_up(tmp_path: Path) -> None:
    cfg = _config(pairing_code="DEAD")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "POST",
        f"{RELAY_URL}/v1/pair",
        FakeResponse(404, {"error": {"code": "pairing_expired"}}),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    assert _pair(client, cfg, _status(), FakeShutdown(), cfg_path) is None  # type: ignore[arg-type]


def test_pairing_sends_descriptive_user_agent(tmp_path: Path) -> None:
    """The hosted relay is behind bot protection that 403s python-requests
    before the request ever reaches the Worker."""
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("POST", f"{RELAY_URL}/v1/pair", FakeResponse(404, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]
    _pair(client, cfg, _status(), FakeShutdown(), cfg_path)  # type: ignore[arg-type]

    ua = session.calls[0]["headers"]["User-Agent"]
    assert ua == USER_AGENT
    assert "python-requests" not in ua.lower()


def test_pairing_self_reports_geometry_and_kind(tmp_path: Path) -> None:
    cfg = _config(pairing_code="ABC123")
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("POST", f"{RELAY_URL}/v1/pair", FakeResponse(404, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]
    _pair(client, cfg, _status(), FakeShutdown(), cfg_path)  # type: ignore[arg-type]

    body = json.loads(session.calls[0]["data"])
    assert body["panel_w"] == 640
    assert body["panel_h"] == 400
    assert body["model"] == "pi_bin_client"


# --- poll cycle ----------------------------------------------------------------


def _good_frame() -> bytes:
    return bytes(buffer_size(PANEL_MODEL))


def test_sealed_frame_is_decrypted_and_painted(tmp_path: Path) -> None:
    panel_priv = bytes([0x02]) * 32
    key = derive_frame_key(panel_priv, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    frame = _good_frame()
    session = FakeSession()
    session.queue(
        "GET", _frame_url(),
        FakeResponse(200, seal(frame, key), headers={"ETag": '"abc"'}),
    )
    session.queue("POST", _status_url(), FakeResponse(200, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    painted: list[bytes] = []
    _wake_cycle(
        client, cfg, _status(), lambda p, m: painted.append(p),
        cfg_path, ButtonReport(seed=1),
    )
    assert painted == [frame]


def test_bad_auth_tag_is_never_painted(tmp_path: Path) -> None:
    """A tag failure means the mailbox doesn't match our key. Painting it
    would render whatever an attacker or a corrupted store supplied."""
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    wrong_key = derive_frame_key(bytes([0x03]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET", _frame_url(),
        FakeResponse(200, seal(_good_frame(), wrong_key), headers={"ETag": '"x"'}),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    painted: list[bytes] = []
    st = _status()
    _wake_cycle(
        client, cfg, st, lambda p, m: painted.append(p), cfg_path, ButtonReport(seed=1)
    )
    assert painted == []
    assert st.state == "error"


def test_wrong_decrypted_length_is_never_painted(tmp_path: Path) -> None:
    """Validating the DECRYPTED length beats trusting the relay's plaintext
    X-Tesserae-Panel-* headers, which the relay could have altered."""
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue(
        "GET", _frame_url(),
        FakeResponse(
            200,
            seal(b"far too short", key),
            headers={"ETag": '"x"', "X-Tesserae-Panel-W": "640"},
        ),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    painted: list[bytes] = []
    _wake_cycle(
        client, cfg, _status(), lambda p, m: painted.append(p),
        cfg_path, ButtonReport(seed=1),
    )
    assert painted == []


def test_304_keeps_the_current_image(tmp_path: Path) -> None:
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key, last_frame_etag='"abc"')
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", _frame_url(), FakeResponse(304))
    session.queue("POST", _status_url(), FakeResponse(200, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    painted: list[bytes] = []
    _wake_cycle(
        client, cfg, _status(), lambda p, m: painted.append(p),
        cfg_path, ButtonReport(seed=1),
    )
    assert painted == []
    assert session.calls[0]["headers"]["If-None-Match"] == '"abc"'


def test_204_means_nothing_published_yet(tmp_path: Path) -> None:
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", _frame_url(), FakeResponse(204))
    session.queue("POST", _status_url(), FakeResponse(200, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    st = _status()
    _wake_cycle(client, cfg, st, lambda p, m: None, cfg_path, ButtonReport(seed=1))
    assert st.state == "idle"
    assert st.last_error is None


def test_401_drops_the_pairing(tmp_path: Path) -> None:
    """401 is unambiguous: a revoked device's token record is deleted, while a
    freshly paired panel with nothing published sees 204."""
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    session = FakeSession()
    session.queue("GET", _frame_url(), FakeResponse(401))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    new_cfg, _next, should_exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path, ButtonReport(seed=1)
    )
    assert should_exit
    assert new_cfg.relay.device_token == ""
    assert new_cfg.relay.frame_key == ""
    assert new_cfg.relay.install_id == ""
    # Persisted, so a restart doesn't retry the dead token.
    assert parse_toml(cfg_path.read_text()).relay.frame_key == ""


def test_button_rides_every_status_post_of_the_wake(tmp_path: Path) -> None:
    """The status slot is latest-only, so an idle beat could overwrite a
    button-carrying post before home pulls it. Repeats are deduped on the
    event id, so repeating is the safe behaviour — unlike the REST path,
    where the frame GET acknowledges the press."""
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    report = ButtonReport(seed=31)
    report.set("refresh")

    session = FakeSession()
    session.queue("GET", _frame_url(), FakeResponse(304), FakeResponse(304))
    session.queue("POST", _status_url(), FakeResponse(200, {}), FakeResponse(200, {}))
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    for _ in range(2):
        _wake_cycle(client, cfg, _status(), lambda p, m: None, cfg_path, report)

    posts = [json.loads(c["data"]) for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 2
    for body in posts:
        assert body["button"] == "refresh"
        assert body["button_event_id"] == 31


def test_config_mailbox_pull_on_etag_change(tmp_path: Path) -> None:
    key = derive_frame_key(bytes([0x02]) * 32, public_key_for(HOME_PRIV))
    cfg = _paired_config(key)
    cfg_path = tmp_path / "config.toml"
    save_config(cfg, cfg_path)

    doc = json.dumps({"sleep_interval_s": 900}).encode("utf-8")
    session = FakeSession()
    session.queue("GET", _frame_url(), FakeResponse(304))
    session.queue("POST", _status_url(), FakeResponse(200, {"config_etag": "cfg1"}))
    session.queue(
        "GET",
        f"{RELAY_URL}/v1/i/inst1/d/dev1/config",
        FakeResponse(200, seal(doc, key), headers={"ETag": "cfg1"}),
    )
    client = RelayClient(RELAY_URL, session=session)  # type: ignore[arg-type]

    new_cfg, next_poll, _exit = _wake_cycle(
        client, cfg, _status(), lambda p, m: None, cfg_path, ButtonReport(seed=1)
    )
    assert new_cfg.relay.poll_interval_s == 900
    assert new_cfg.relay.config_etag == "cfg1"
    assert next_poll == 900


def test_run_aborts_without_relay_url(tmp_path: Path) -> None:
    cfg = _config(relay_url="", pairing_code="X")
    rc = relay.run(
        cfg, _status(), lambda p, m: None, threading.Event(), tmp_path / "x.toml"
    )
    assert rc == 2


def test_run_aborts_without_key_or_code(tmp_path: Path) -> None:
    cfg = _config()
    rc = relay.run(
        cfg, _status(), lambda p, m: None, threading.Event(), tmp_path / "x.toml"
    )
    assert rc == 2
