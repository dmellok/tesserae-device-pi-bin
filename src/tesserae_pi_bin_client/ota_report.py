"""OTA lifecycle state reporting (docs/ota/contract.md, "State reporting").

The ESP32 firmware's A/B slot machinery does not port — a Pi has no partition
table to flip and no bootloader rollback. What *does* port is the reporting
half: the server's Devices UI tracks update attempts through the `ota` object
on the heartbeat, and a Pi updating itself via pip or git can populate exactly
the same fields.

    { "ota": { "schema": 1, "phase": …, "reason": …,
               "target_fw": …, "attempt_id": … } }

`schema` alone is the capability advertisement and is always present; the
server only offers an update to a device that advertised a compatible schema.
The other fields describe the latest attempt, and the server dedups repeated
identical reports — so the daemon simply keeps sending the current state
rather than tracking what it has already sent.

State is persisted, for the same reason the firmware keeps it in NVS rather
than RTC memory: the interesting transitions are precisely the ones that span
a restart (`pending_confirm` → `confirmed` or `rolled_back`), so in-memory
state would lose exactly the report worth having.

This module reports; it does not update. Deciding to install, and installing,
is the operator's business (apt, pip, git, a systemd timer) — this just gives
whatever does it a place to record what happened.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OTA_SCHEMA = 1

# Contract phases. `idle` is represented by having no stored report at all,
# matching the firmware, so a device that has never attempted an update sends
# just the capability.
PHASES = {
    "idle",
    "downloading",
    "validating",
    "pending_confirm",
    "confirmed",
    "rejected",
    "failed",
    "rolled_back",
}

TERMINAL_PHASES = {"confirmed", "rejected", "failed", "rolled_back"}

# Reason vocabulary, meaningful only on a terminal phase.
REASONS = {
    "ok",
    # rejected
    "bad_signature", "malformed_descriptor", "malformed_manifest",
    "schema_version", "kind_mismatch", "already_current",
    "battery_low", "battery_unknown",
    # failed
    "download_error", "size_mismatch", "digest_mismatch",
    "image_invalid", "flash_error",
    # rolled_back
    "boot_failed",
}


@dataclass(frozen=True)
class OtaState:
    phase: str = "idle"
    reason: str = ""
    target_fw: str = ""
    attempt_id: str = ""


class OtaReporter:
    """Persisted OTA attempt state, rendered into the heartbeat."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state = self._load()

    def _load(self) -> OtaState:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return OtaState()
        if not isinstance(raw, dict):
            return OtaState()
        phase = raw.get("phase")
        return OtaState(
            phase=phase if phase in PHASES else "idle",
            reason=str(raw.get("reason", "") or ""),
            target_fw=str(raw.get("target_fw", "") or ""),
            attempt_id=str(raw.get("attempt_id", "") or ""),
        )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "phase": self._state.phase,
                        "reason": self._state.reason,
                        "target_fw": self._state.target_fw,
                        "attempt_id": self._state.attempt_id,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("ota: could not persist state (%s)", exc)

    @property
    def state(self) -> OtaState:
        return self._state

    def set(
        self,
        phase: str,
        reason: str = "",
        target_fw: str = "",
        attempt_id: str = "",
    ) -> None:
        """Record an attempt state and persist it immediately.

        Immediately, because the very next event may be the restart this
        report exists to survive.
        """
        if phase not in PHASES:
            raise ValueError(f"unknown OTA phase: {phase!r}")
        if reason and reason not in REASONS:
            raise ValueError(f"unknown OTA reason: {reason!r}")
        self._state = OtaState(
            phase=phase, reason=reason, target_fw=target_fw, attempt_id=attempt_id
        )
        self._save()

    def clear(self) -> None:
        self._state = OtaState()
        try:
            self._path.unlink()
        except OSError:
            pass

    def resolve_boot(self, running_fw: str) -> None:
        """Turn a cross-restart `pending_confirm` into its terminal state.

        Call once at startup. Running the version we were installing means the
        update took; running anything else means it didn't and the old version
        came back — which on a Pi is a failed/reverted install rather than a
        bootloader rollback, but reports identically so the server's Devices
        UI reads the same for every device kind.
        """
        if self._state.phase != "pending_confirm":
            return
        if self._state.target_fw and running_fw == self._state.target_fw:
            log.info("ota: update to %s confirmed", running_fw)
            self.set(
                "confirmed", "ok", self._state.target_fw, self._state.attempt_id
            )
        else:
            log.warning(
                "ota: expected %s after update but running %s; reporting rolled_back",
                self._state.target_fw or "?", running_fw,
            )
            self.set(
                "rolled_back",
                "boot_failed",
                self._state.target_fw,
                self._state.attempt_id,
            )

    def payload(self) -> dict[str, Any]:
        """The heartbeat's `ota` object.

        `schema` alone means idle — the capability advertisement is what makes
        the server willing to offer an update at all, so it is never omitted.
        """
        out: dict[str, Any] = {"schema": OTA_SCHEMA}
        if self._state.phase == "idle":
            return out
        out["phase"] = self._state.phase
        if self._state.reason and self._state.phase in TERMINAL_PHASES:
            out["reason"] = self._state.reason
        if self._state.target_fw:
            out["target_fw"] = self._state.target_fw
        if self._state.attempt_id:
            out["attempt_id"] = self._state.attempt_id
        return out


__all__ = [
    "OTA_SCHEMA",
    "PHASES",
    "REASONS",
    "TERMINAL_PHASES",
    "OtaReporter",
    "OtaState",
]
