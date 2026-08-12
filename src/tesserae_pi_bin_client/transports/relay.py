"""Cloud-relay transport — a panel that lives where the Tesserae server isn't.

A different house, a phone hotspot, behind CGNAT: both ends talk *outbound* to
a small Worker that acts as a per-device mailbox. Home seals each rendered
frame and PUTs it; this daemon polls and decrypts. Nothing needs to be exposed
to the internet, and the relay itself is zero-knowledge (crypto in
relay_crypto.py).

    unconfigured --set relay_url + code--> pairing --ready--> polling

Pairing may span restarts, so its state is persisted: the X25519 private
scalar is kept only until the frame key is derived, then wiped. The POST to
/v1/pair happens EXACTLY ONCE — the stored private key is the latch — because
only that route enforces the code's expiry, so re-POSTing later would 404 a
code that is still perfectly good for polling. The poll has no expiry and may
span as many cycles as home needs.

Wire contract: docs/relay/contract.md in the Tesserae repo, authoritative for
home, Worker and client alike.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

from ..button_report import ButtonReport
from ..config import Config, save_config, with_relay_updates
from ..heartbeat import Status, _wifi_rssi
from ..mqtt_loop import PaintFn
from ..panels import buffer_size
from ..relay_crypto import (
    RelayCryptoError,
    b64url_decode,
    b64url_encode,
    derive_frame_key,
    generate_private_key,
    public_key_for,
    unseal,
)

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 20.0

# The hosted relay sits behind Cloudflare bot protection, which blocks known-bot
# user agents (`python-requests/…` among them) with a 403 before the request
# ever reaches the Worker. Any descriptive UA passes; home sends
# `tesserae/relay`. Without this the daemon fails with a 403 that looks like an
# auth problem but is not one.
USER_AGENT = "tesserae-pi-bin-client/relay"

PAIR_RETRY_S = 15
TRANSIENT_FAILURE_BACKOFF_S = 30

# The device kind home falls back to when the pairing slot names none. Sent as
# a self-report so the operator doesn't have to pre-enter geometry.
DEVICE_MODEL = "pi_bin_client"


class RelayClient:
    """Thin wrapper over requests.Session for the /v1 relay routes."""

    def __init__(
        self,
        base_url: str,
        session: requests.Session | None = None,
        timeout_s: float = HTTP_TIMEOUT_S,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout_s

    def _headers(self, token: str = "") -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def submit_pairing(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        resp = self._session.post(
            f"{self._base}/v1/pair",
            data=json.dumps(body),
            headers={**self._headers(), "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        return resp.status_code, _maybe_json(resp)

    def poll_pairing(self, code: str) -> tuple[int, dict[str, Any] | None]:
        resp = self._session.get(
            f"{self._base}/v1/pair/{code}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        return resp.status_code, _maybe_json(resp)

    def get_frame(
        self, install_id: str, device_id: str, token: str, if_none_match: str = ""
    ) -> tuple[int, bytes, str]:
        headers = self._headers(token)
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        resp = self._session.get(
            f"{self._base}/v1/i/{install_id}/d/{device_id}/frame",
            headers=headers,
            timeout=self._timeout,
            stream=False,
        )
        etag = resp.headers.get("ETag", "") or ""
        body = resp.content if resp.status_code == 200 else b""
        return resp.status_code, body, etag

    def post_status(
        self, install_id: str, device_id: str, token: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        resp = self._session.post(
            f"{self._base}/v1/i/{install_id}/d/{device_id}/status",
            data=json.dumps(body, default=str),
            headers={**self._headers(token), "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        return resp.status_code, _maybe_json(resp)

    def get_config(
        self, install_id: str, device_id: str, token: str, if_none_match: str = ""
    ) -> tuple[int, bytes, str]:
        headers = self._headers(token)
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        resp = self._session.get(
            f"{self._base}/v1/i/{install_id}/d/{device_id}/config",
            headers=headers,
            timeout=self._timeout,
        )
        etag = resp.headers.get("ETag", "") or ""
        body = resp.content if resp.status_code == 200 else b""
        return resp.status_code, body, etag


def _maybe_json(resp: requests.Response) -> dict[str, Any] | None:
    if not resp.content:
        return None
    try:
        obj = resp.json()
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# --- pairing -------------------------------------------------------------------


def _pair(
    client: RelayClient,
    config: Config,
    status: Status,
    shutdown: threading.Event,
    config_path: Path,
) -> Config | None:
    """Advance the rendezvous until the frame key exists. None on shutdown."""
    relay = config.relay

    # The POST is latched on the stored private key: generate-and-submit
    # happens once, then every later attempt is a poll. Re-POSTing would 404
    # a code that is still valid for polling (only the POST enforces expiry).
    if not relay.private_key:
        private_raw = generate_private_key()
        body = {
            "code": relay.pairing_code,
            "panel_pubkey": b64url_encode(public_key_for(private_raw)),
            "panel_w": status.panel_w,
            "panel_h": status.panel_h,
            "model": DEVICE_MODEL,
        }
        while not shutdown.is_set():
            try:
                code, resp = client.submit_pairing(body)
            except (requests.ConnectionError, requests.Timeout) as exc:
                log.warning(
                    "relay pair submit failed (%s: %s); retrying in %ds",
                    type(exc).__name__, exc, TRANSIENT_FAILURE_BACKOFF_S,
                )
                shutdown.wait(TRANSIENT_FAILURE_BACKOFF_S)
                continue
            if code in (200, 202):
                config = with_relay_updates(
                    config, private_key=b64url_encode(private_raw)
                )
                save_config(config, config_path)
                log.info("relay: pairing submitted; waiting for home to complete it")
                break
            if code == 404:
                log.error(
                    "relay pair 404: code %r is unknown or expired — "
                    "generate a fresh one in Settings -> Cloud relay",
                    relay.pairing_code,
                )
                return None
            if code == 403:
                log.error(
                    "relay pair 403: rejected by the relay (entitlement, or a "
                    "bot-protection block on the User-Agent)"
                )
                return None
            log.warning(
                "relay pair: unexpected status %d (body=%r); retrying in %ds",
                code, resp, TRANSIENT_FAILURE_BACKOFF_S,
            )
            shutdown.wait(TRANSIENT_FAILURE_BACKOFF_S)
        if shutdown.is_set():
            return None
        relay = config.relay

    # Poll until home completes the exchange. No expiry on this route.
    while not shutdown.is_set():
        try:
            code, resp = client.poll_pairing(relay.pairing_code)
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning(
                "relay pair poll failed (%s: %s); retrying in %ds",
                type(exc).__name__, exc, TRANSIENT_FAILURE_BACKOFF_S,
            )
            shutdown.wait(TRANSIENT_FAILURE_BACKOFF_S)
            continue

        if code == 404:
            log.error(
                "relay pair poll 404: the code expired before home completed "
                "it — clearing local pairing state, generate a fresh code"
            )
            config = with_relay_updates(config, private_key="", pairing_code="")
            save_config(config, config_path)
            return None
        if code != 200 or resp is None:
            log.warning("relay pair poll returned %d; retrying in %ds", code, PAIR_RETRY_S)
            shutdown.wait(PAIR_RETRY_S)
            continue

        if resp.get("status") != "ready":
            log.info(
                "relay: waiting for home to complete pairing "
                "(approve the panel in Settings -> Cloud relay)"
            )
            shutdown.wait(PAIR_RETRY_S)
            continue

        try:
            home_pub = b64url_decode(str(resp.get("home_pubkey", "")))
            private_raw = b64url_decode(relay.private_key)
            frame_key = derive_frame_key(private_raw, home_pub)
        except RelayCryptoError as exc:
            log.error("relay pairing completed but key derivation failed: %s", exc)
            return None

        install_id = str(resp.get("install_id", ""))
        device_id = str(resp.get("device_id", ""))
        device_token = str(resp.get("device_token", ""))
        if not (install_id and device_id and device_token):
            log.error(
                "relay pairing ready but missing install_id/device_id/token "
                "(body=%r)", resp,
            )
            return None

        # The private scalar has done its only job. Wipe it: from here on only
        # the derived key is needed, and a key at rest is a smaller target
        # than the scalar that generates it.
        config = with_relay_updates(
            config,
            install_id=install_id,
            device_id=device_id,
            device_token=device_token,
            frame_key=b64url_encode(frame_key),
            private_key="",
            pairing_code="",
        )
        save_config(config, config_path)
        log.info(
            "relay paired: install=%s device=%s (frame key derived locally; "
            "it never travelled)", install_id, device_id,
        )
        # Home ships the initial device config in the pairing response, so a
        # panel starts on the operator's cadence rather than the default and
        # only later discovers it from the config mailbox.
        config, _interval = _apply_relay_config(
            resp.get("config"), config, config_path
        )
        return config
    return None


def _apply_relay_config(
    doc: Any, config: Config, config_path: Path
) -> tuple[Config, int | None]:
    """Apply a config document (pairing-time or from the config mailbox).

    Same shape as a REST /status response's `config` block. Returns the
    (possibly rebound) config and the sleep interval it asked for, if any.
    """
    if not isinstance(doc, dict):
        return config, None
    interval = doc.get("sleep_interval_s")
    if isinstance(interval, int) and interval > 0:
        if interval != config.relay.poll_interval_s:
            log.info(
                "relay config: poll_interval_s %d -> %d",
                config.relay.poll_interval_s, interval,
            )
            config = with_relay_updates(config, poll_interval_s=interval)
            save_config(config, config_path)
        return config, interval
    return config, None


# --- wake loop -----------------------------------------------------------------


def run(
    config: Config,
    status: Status,
    paint_fn: PaintFn,
    shutdown: threading.Event,
    config_path: Path,
) -> int:
    """Relay wake loop. Returns process exit code (0 = clean shutdown)."""
    if not config.relay.relay_url:
        log.error("transport_mode=relay but [relay].relay_url is empty")
        return 2

    session = requests.Session()
    client = RelayClient(config.relay.relay_url, session=session)

    if not config.relay.frame_key:
        if not config.relay.pairing_code:
            log.error(
                "transport_mode=relay but no frame key and no pairing code — "
                "run with --pair <CODE> using a code from "
                "Settings -> Cloud relay -> Add a remote panel"
            )
            return 2
        paired = _pair(client, config, status, shutdown, config_path)
        if paired is None:
            return 0 if shutdown.is_set() else 2
        config = paired

    report = ButtonReport()
    watcher = None
    wake = threading.Event()
    if config.buttons.enabled:
        from ..buttons import ButtonWatcher

        watcher = ButtonWatcher(config.buttons, report, on_press=lambda _a: wake.set())
        watcher.start()

    try:
        while not shutdown.is_set():
            config, next_poll, should_exit = _wake_cycle(
                client, config, status, paint_fn, config_path, report
            )
            if should_exit:
                return 2
            log.info("sleeping %ds until next relay poll", next_poll)
            if _sleep_until_wake(shutdown, wake, next_poll):
                break
    finally:
        if watcher is not None:
            watcher.stop()
    return 0


def _sleep_until_wake(
    shutdown: threading.Event, wake: threading.Event, seconds: int
) -> bool:
    """Sleep, returning True only for shutdown. A button press cuts it short."""
    deadline = time.monotonic() + seconds
    while True:
        if shutdown.is_set():
            return True
        if wake.is_set():
            wake.clear()
            log.info("button press; polling the relay now")
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if shutdown.wait(min(0.25, remaining)):
            return True


def _drop_pairing(config: Config, config_path: Path) -> Config:
    """A 401 on any device-token route means the pairing was revoked.

    The contract is explicit: drop the stored pairing rather than retrying the
    token — a revoked device's mailbox is deleted along with its token record,
    so 401 is unambiguous (a freshly paired panel with nothing published sees
    204, not 401).
    """
    config = with_relay_updates(
        config,
        install_id="",
        device_id="",
        device_token="",
        frame_key="",
        private_key="",
        last_frame_etag="",
        config_etag="",
    )
    save_config(config, config_path)
    return config


def _wake_cycle(
    client: RelayClient,
    config: Config,
    status: Status,
    paint_fn: PaintFn,
    config_path: Path,
    report: ButtonReport,
) -> tuple[Config, int, bool]:
    """One poll: frame GET, optional paint, status POST, optional config pull."""
    relay = config.relay
    install_id, device_id = relay.install_id, relay.device_id
    token = relay.device_token
    try:
        frame_key = b64url_decode(relay.frame_key)
    except RelayCryptoError as exc:
        log.error("stored frame key is unreadable (%s); re-pair to recover", exc)
        return _drop_pairing(config, config_path), 0, True

    status.rssi = _wifi_rssi()

    # --- frame GET ---
    try:
        code, sealed, etag = client.get_frame(
            install_id, device_id, token, if_none_match=relay.last_frame_etag
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("relay frame GET failed (%s: %s)", type(exc).__name__, exc)
        status.state = "error"
        status.last_error = f"{type(exc).__name__}: {exc}"
        return config, relay.poll_interval_s, False

    if code == 401:
        log.error(
            "relay frame GET 401: this pairing was revoked — dropping stored "
            "pairing; re-pair with a fresh code to recover"
        )
        return _drop_pairing(config, config_path), 0, True
    if code == 403:
        log.error("relay frame GET 403: token is not valid for this mailbox")
        return config, 0, True
    if code == 304:
        log.info("relay frame unchanged (304)")
        status.state = "idle"
        status.last_error = None
    elif code == 204:
        log.info("relay mailbox empty; home has not published a frame yet")
        status.state = "idle"
        status.last_error = None
    elif code == 200:
        status.state = "rendering"
        try:
            frame = unseal(sealed, frame_key)
        except RelayCryptoError as exc:
            # Never paint an unauthenticated frame.
            log.error("relay frame rejected: %s", exc)
            status.state = "error"
            status.last_error = str(exc)
            return config, relay.poll_interval_s, False

        # Validate the DECRYPTED length against geometry we know at build
        # time. This is a stronger check than trusting the relay's plaintext
        # X-Tesserae-Panel-* headers, which the relay could have altered.
        expected = buffer_size(config.panel.model)
        if len(frame) != expected:
            log.error(
                "relay frame decrypted to %d bytes; expected %d for %s — "
                "refusing to paint",
                len(frame), expected, config.panel.model,
            )
            status.state = "error"
            status.last_error = f"frame size {len(frame)} != {expected}"
            return config, relay.poll_interval_s, False

        try:
            paint_fn(frame, config.panel.model)
        except (ValueError, OSError) as exc:
            log.warning("relay paint failed: %s", exc)
            status.state = "error"
            status.last_error = f"{type(exc).__name__}: {exc}"
            return config, relay.poll_interval_s, False
        status.state = "idle"
        status.last_error = None
        status.last_paint_at = time.time()
        if etag:
            status.last_digest = etag.strip('"')
            if etag != relay.last_frame_etag:
                config = with_relay_updates(config, last_frame_etag=etag)
                save_config(config, config_path)
    else:
        log.warning("relay frame GET unexpected status=%d", code)

    # --- status POST ---
    # The status slot is latest-only, so a button-carrying post could be
    # overwritten by a later idle beat before home pulls it. The contract's
    # answer: repeat button + button_event_id on EVERY post of the same wake.
    # Home dedups on the event id, so repeats are safe. This is deliberately
    # different from the REST path, where the frame GET acknowledges a press.
    press = report.pending()
    status.button = press.name if press is not None else ""
    status.button_event_id = press.event_id if press is not None else None
    status.next_sleep_s = config.relay.poll_interval_s

    try:
        post_code, post_body = client.post_status(
            install_id, device_id, token, status.payload()
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("relay status POST failed (%s: %s)", type(exc).__name__, exc)
        return config, config.relay.poll_interval_s, False

    if post_code == 401:
        log.error("relay status POST 401: pairing revoked — dropping stored pairing")
        return _drop_pairing(config, config_path), 0, True

    next_poll = config.relay.poll_interval_s
    if isinstance(post_body, dict):
        remote_etag = post_body.get("config_etag")
        if isinstance(remote_etag, str) and remote_etag != config.relay.config_etag:
            config, pushed = _pull_config(
                client, config, config_path, frame_key, remote_etag
            )
            if pushed is not None:
                next_poll = pushed

    return config, next_poll, False


def _pull_config(
    client: RelayClient,
    config: Config,
    config_path: Path,
    frame_key: bytes,
    remote_etag: str,
) -> tuple[Config, int | None]:
    """Fetch + unseal the config mailbox after its etag moved."""
    relay = config.relay
    try:
        code, sealed, etag = client.get_config(
            relay.install_id,
            relay.device_id,
            relay.device_token,
            if_none_match=relay.config_etag,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("relay config GET failed (%s: %s)", type(exc).__name__, exc)
        return config, None
    if code in (204, 304):
        return config, None
    if code != 200:
        log.warning("relay config GET returned %d", code)
        return config, None
    try:
        doc = json.loads(unseal(sealed, frame_key).decode("utf-8"))
    except (RelayCryptoError, ValueError, UnicodeDecodeError) as exc:
        log.error("relay config rejected: %s", exc)
        return config, None

    config = with_relay_updates(config, config_etag=etag or remote_etag)
    save_config(config, config_path)
    log.info("relay config adopted (etag=%s)", etag or remote_etag)
    return _apply_relay_config(doc, config, config_path)


__all__ = ["DEVICE_MODEL", "USER_AGENT", "RelayClient", "run"]
