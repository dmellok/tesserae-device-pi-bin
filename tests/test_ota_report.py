"""OTA lifecycle reporting against docs/ota/contract.md.

This client reports; it never installs. The transition worth the persistence
is pending_confirm -> confirmed/rolled_back, which by definition spans the
restart it is waiting on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tesserae_pi_bin_client.ota_report import OTA_SCHEMA, OtaReporter


def _reporter(tmp_path: Path) -> OtaReporter:
    return OtaReporter(tmp_path / "ota-state.json")


def test_idle_reports_only_the_capability(tmp_path: Path) -> None:
    """schema alone means idle — and it is the capability advertisement that
    makes the server willing to offer an update at all, so it is never
    omitted."""
    assert _reporter(tmp_path).payload() == {"schema": OTA_SCHEMA}


def test_in_progress_phase_is_reported(tmp_path: Path) -> None:
    rep = _reporter(tmp_path)
    rep.set("downloading", target_fw="1.6.0", attempt_id="a3f19c")
    payload = rep.payload()
    assert payload["phase"] == "downloading"
    assert payload["target_fw"] == "1.6.0"
    assert payload["attempt_id"] == "a3f19c"
    # reason is meaningful only on a terminal phase.
    assert "reason" not in payload


def test_terminal_phase_carries_its_reason(tmp_path: Path) -> None:
    rep = _reporter(tmp_path)
    rep.set("failed", "download_error", "1.6.0", "a3f19c")
    payload = rep.payload()
    assert payload["phase"] == "failed"
    assert payload["reason"] == "download_error"


def test_state_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "ota-state.json"
    OtaReporter(path).set("pending_confirm", target_fw="1.6.0", attempt_id="a3f19c")

    revived = OtaReporter(path)
    assert revived.state.phase == "pending_confirm"
    assert revived.state.target_fw == "1.6.0"


def test_resolve_boot_confirms_when_running_the_target(tmp_path: Path) -> None:
    path = tmp_path / "ota-state.json"
    OtaReporter(path).set("pending_confirm", target_fw="1.6.0", attempt_id="a3f19c")

    rep = OtaReporter(path)
    rep.resolve_boot("1.6.0")
    assert rep.payload()["phase"] == "confirmed"
    assert rep.payload()["reason"] == "ok"


def test_resolve_boot_rolls_back_when_running_something_else(tmp_path: Path) -> None:
    path = tmp_path / "ota-state.json"
    OtaReporter(path).set("pending_confirm", target_fw="1.6.0", attempt_id="a3f19c")

    rep = OtaReporter(path)
    rep.resolve_boot("1.5.0")  # the update didn't take
    assert rep.payload()["phase"] == "rolled_back"
    assert rep.payload()["reason"] == "boot_failed"
    # Correlation back to the attempt that preceded it must survive.
    assert rep.payload()["attempt_id"] == "a3f19c"


def test_resolve_boot_is_a_noop_outside_pending_confirm(tmp_path: Path) -> None:
    rep = _reporter(tmp_path)
    rep.set("confirmed", "ok", "1.6.0")
    rep.resolve_boot("9.9.9")
    assert rep.payload()["phase"] == "confirmed"


def test_unknown_phase_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown OTA phase"):
        _reporter(tmp_path).set("exploding")


def test_unknown_reason_is_rejected(tmp_path: Path) -> None:
    """One vocabulary spans verify and apply; a typo would be silently
    dropped server-side."""
    with pytest.raises(ValueError, match="unknown OTA reason"):
        _reporter(tmp_path).set("failed", "it_broke")


def test_corrupt_state_file_falls_back_to_idle(tmp_path: Path) -> None:
    path = tmp_path / "ota-state.json"
    path.write_text("{not json")
    assert OtaReporter(path).payload() == {"schema": OTA_SCHEMA}


def test_clear_returns_to_idle(tmp_path: Path) -> None:
    rep = _reporter(tmp_path)
    rep.set("failed", "flash_error", "1.6.0")
    rep.clear()
    assert rep.payload() == {"schema": OTA_SCHEMA}
