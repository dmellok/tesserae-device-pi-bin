from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from tesserae_pi_bin_client.config import (
    DEFAULT_TOML,
    Config,
    RestConfig,
    load_config,
    parse_toml,
    render_config_toml,
    render_from_config,
    save_config,
    with_rest_updates,
)


def test_default_toml_parses() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    assert isinstance(cfg, Config)
    assert cfg.mqtt.host == "192.168.1.10"
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.device_id == "pi_bin"
    assert cfg.panel.model == "inky_13_3"
    assert cfg.http.download_timeout_s == 30
    assert cfg.http.max_frame_bytes == 16_000_000
    assert cfg.logging.level == "INFO"


def test_load_config_creates_default_on_first_run(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    assert not path.exists()
    cfg = load_config(path)
    assert path.exists()
    assert cfg.panel.model == "inky_13_3"


def test_unknown_panel_rejected() -> None:
    bad = DEFAULT_TOML.replace('model = "inky_13_3"', 'model = "inky_nope"')
    with pytest.raises(ValueError, match="unknown"):
        parse_toml(bad)


def test_bad_port_rejected() -> None:
    bad = DEFAULT_TOML.replace("port = 1883", "port = 0")
    with pytest.raises(ValueError, match="out of range"):
        parse_toml(bad)


def test_negative_keepalive_rejected() -> None:
    bad = DEFAULT_TOML.replace("keepalive = 60", "keepalive = -1")
    with pytest.raises(ValueError, match="keepalive"):
        parse_toml(bad)


def test_missing_required_field_rejected() -> None:
    bad = DEFAULT_TOML.replace('client_id = "pi-impression-1"\n', "")
    with pytest.raises(ValueError, match="client_id"):
        parse_toml(bad)


def test_unknown_log_level_rejected() -> None:
    bad = DEFAULT_TOML.replace('level = "INFO"', 'level = "VERBOSE"')
    with pytest.raises(ValueError, match="level"):
        parse_toml(bad)


def test_overrides_take_effect() -> None:
    bad = DEFAULT_TOML.replace(
        'host = "192.168.1.10"', 'host = "broker.local"'
    ).replace("port = 1883", "port = 8883")
    cfg = parse_toml(bad)
    assert cfg.mqtt.host == "broker.local"
    assert cfg.mqtt.port == 8883


# --- render_config_toml -------------------------------------------------------


def test_render_no_args_matches_default() -> None:
    # DEFAULT_TOML is just render_config_toml(); they must agree.
    assert render_config_toml() == DEFAULT_TOML


def test_render_overrides_round_trip() -> None:
    body = render_config_toml(
        mqtt_host="broker.lan",
        mqtt_port=8883,
        mqtt_username="alice",
        mqtt_password="hunter2",
        mqtt_client_id="kitchen-display",
        panel_model="inky_7_3",
    )
    cfg = parse_toml(body)
    assert cfg.mqtt.host == "broker.lan"
    assert cfg.mqtt.port == 8883
    assert cfg.mqtt.username == "alice"
    assert cfg.mqtt.password == "hunter2"
    assert cfg.mqtt.client_id == "kitchen-display"
    assert cfg.panel.model == "inky_7_3"


def test_render_escapes_quote_in_string_value() -> None:
    # A password containing a double quote must still produce valid TOML.
    body = render_config_toml(mqtt_password='abc"def')
    cfg = parse_toml(body)
    assert cfg.mqtt.password == 'abc"def'


def test_render_escapes_backslash_in_string_value() -> None:
    body = render_config_toml(mqtt_password=r"a\b")
    cfg = parse_toml(body)
    assert cfg.mqtt.password == r"a\b"


# --- device_id ----------------------------------------------------------------


def test_render_custom_device_id_round_trips() -> None:
    body = render_config_toml(device_id="pi_kitchen")
    cfg = parse_toml(body)
    assert cfg.mqtt.device_id == "pi_kitchen"


def test_missing_device_id_defaults_to_pi_bin() -> None:
    # A config that omits device_id (e.g. one written before the field existed)
    # must parse and fall back to "pi_bin" — the post-split server prefix, not
    # the legacy "pi".
    body = "\n".join(
        line for line in DEFAULT_TOML.splitlines() if "device_id" not in line
    ) + "\n"
    cfg = parse_toml(body)
    assert cfg.mqtt.device_id == "pi_bin"


def test_invalid_device_id_rejected() -> None:
    bad = DEFAULT_TOML.replace('device_id = "pi_bin"', 'device_id = "Pi-Kitchen"')
    with pytest.raises(ValueError, match="device_id"):
        parse_toml(bad)


def test_too_short_device_id_rejected() -> None:
    bad = DEFAULT_TOML.replace('device_id = "pi_bin"', 'device_id = "a"')
    with pytest.raises(ValueError, match="device_id"):
        parse_toml(bad)


def test_device_id_starting_with_digit_rejected() -> None:
    bad = DEFAULT_TOML.replace('device_id = "pi_bin"', 'device_id = "1pi"')
    with pytest.raises(ValueError, match="device_id"):
        parse_toml(bad)


# --- transport_mode + [rest] section ------------------------------------------


def test_default_transport_mode_is_rest() -> None:
    # REST is the new default for fresh installs — the install prompt also
    # picks it. Existing configs that predate the key still fall back to
    # MQTT via the parser fallback; see test_missing_transport_mode_*.
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.transport_mode == "rest"
    assert cfg.rest.server_url == "http://tesserae.local:8765"
    assert cfg.rest.device_token == ""
    assert cfg.rest.pairing_code == ""
    assert cfg.rest.last_frame_etag == ""
    assert cfg.rest.poll_interval_s == 60


def test_unknown_transport_mode_rejected() -> None:
    bad = DEFAULT_TOML.replace(
        'transport_mode = "rest"', 'transport_mode = "carrier-pigeon"'
    )
    with pytest.raises(ValueError, match="transport_mode"):
        parse_toml(bad)


def test_rest_mode_requires_server_url() -> None:
    # DEFAULT_TOML already has rest mode with a placeholder server_url —
    # strip the value to trigger the missing-url validation.
    bad = DEFAULT_TOML.replace(
        'server_url = "http://tesserae.local:8765"', 'server_url = ""'
    )
    with pytest.raises(ValueError, match="server_url"):
        parse_toml(bad)


def test_rest_mode_with_custom_server_url_parses() -> None:
    body = render_config_toml(
        transport_mode="rest",
        rest_server_url="http://192.168.1.20:8765",
        rest_pairing_code="ABC123",
    )
    cfg = parse_toml(body)
    assert cfg.transport_mode == "rest"
    assert cfg.rest.server_url == "http://192.168.1.20:8765"
    assert cfg.rest.pairing_code == "ABC123"


def test_missing_transport_mode_defaults_to_mqtt() -> None:
    # A config predating the REST split (no transport_mode key) keeps the
    # MQTT behaviour without prompting the user to re-pair.
    body = "\n".join(
        line for line in DEFAULT_TOML.splitlines() if "transport_mode" not in line
    ) + "\n"
    cfg = parse_toml(body)
    assert cfg.transport_mode == "mqtt"


def test_mqtt_mode_explicit_still_works() -> None:
    body = render_config_toml(transport_mode="mqtt")
    cfg = parse_toml(body)
    assert cfg.transport_mode == "mqtt"


def test_negative_poll_interval_rejected() -> None:
    body = render_config_toml(rest_poll_interval_s=60)
    bad = body.replace("poll_interval_s = 60", "poll_interval_s = 0")
    with pytest.raises(ValueError, match="poll_interval_s"):
        parse_toml(bad)


# --- save_config round-trip ---------------------------------------------------


def test_save_config_round_trips_rest_state(tmp_path: Path) -> None:
    """The daemon persists device_token + last_frame_etag through save_config;
    re-loading must give back the exact same values (no quote escaping bugs)."""
    path = tmp_path / "config.toml"
    body = render_config_toml(
        transport_mode="rest",
        rest_server_url="http://srv:8765",
    )
    path.write_text(body, encoding="utf-8")
    cfg = parse_toml(path.read_text())

    updated = with_rest_updates(
        cfg,
        device_token="TOK_xyz123",
        last_frame_etag='"sha-deadbeef"',  # ETag quotes must survive
    )
    save_config(updated, path)

    reloaded = parse_toml(path.read_text())
    assert reloaded.rest.device_token == "TOK_xyz123"
    assert reloaded.rest.last_frame_etag == '"sha-deadbeef"'
    # All other fields preserved.
    assert reloaded.mqtt.device_id == cfg.mqtt.device_id
    assert reloaded.panel.model == cfg.panel.model


def test_save_config_atomic_writes_via_temp_file(tmp_path: Path) -> None:
    """A save with a non-writable parent dir doesn't leave a stray .tmp."""
    path = tmp_path / "config.toml"
    body = render_config_toml()
    path.write_text(body, encoding="utf-8")
    cfg = parse_toml(path.read_text())
    save_config(cfg, path)
    # Only the canonical file remains; no .tmp leftovers.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_render_from_config_round_trip() -> None:
    cfg = Config(
        mqtt=parse_toml(DEFAULT_TOML).mqtt,
        panel=parse_toml(DEFAULT_TOML).panel,
        http=parse_toml(DEFAULT_TOML).http,
        logging=parse_toml(DEFAULT_TOML).logging,
        transport_mode="rest",
        rest=RestConfig(
            server_url="http://h:1",
            device_token="t",
            pairing_code="",
            last_frame_etag="e",
            poll_interval_s=120,
        ),
    )
    body = render_from_config(cfg)
    rt = parse_toml(body)
    assert rt.transport_mode == "rest"
    assert rt.rest.server_url == "http://h:1"
    assert rt.rest.device_token == "t"
    assert rt.rest.poll_interval_s == 120


# --- buttons -------------------------------------------------------------------


def test_buttons_default_off_with_13_3_pin_c() -> None:
    """C is GPIO 25 on the 13.3" (16 on the smaller Impressions), and the
    default panel here is the 13.3"."""
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.buttons.enabled is False
    assert cfg.buttons.pin_c == 25
    assert cfg.buttons.pin_actions() == {5: "left", 6: "refresh", 25: "right"}


def test_buttons_unmapped_action_is_dropped() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    # action_d defaults to "" -> button D is not mapped.
    assert cfg.buttons.pin_d not in cfg.buttons.pin_actions()


def test_buttons_reject_unknown_action() -> None:
    body = DEFAULT_TOML.replace('action_a = "left"', 'action_a = "rotate_prev"')
    with pytest.raises(ValueError, match="action_a unknown"):
        parse_toml(body)


def test_buttons_reject_out_of_range_pin() -> None:
    body = DEFAULT_TOML.replace("pin_a = 5", "pin_a = 999")
    with pytest.raises(ValueError, match="pin_a must be a BCM pin"):
        parse_toml(body)


def test_buttons_enabled_with_no_mapping_is_rejected() -> None:
    body = DEFAULT_TOML.replace("enabled = false", "enabled = true")
    for action in ("action_a", "action_b", "action_c"):
        body = re.sub(rf'{action} = "[a-z]*"', f'{action} = ""', body)
    with pytest.raises(ValueError, match="map at least one button"):
        parse_toml(body)


def test_buttons_duplicate_pin_collapses() -> None:
    """Two buttons on one pin must not produce two watchers on one line."""
    body = DEFAULT_TOML.replace("pin_b = 6", "pin_b = 5")
    cfg = parse_toml(body)
    assert len(cfg.buttons.pin_actions()) == 2


# --- rest tz / sse -------------------------------------------------------------


def test_rest_tz_and_sse_round_trip() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.rest.tz == ""
    assert cfg.rest.sse_enabled is True
    updated = replace(cfg, rest=replace(cfg.rest, tz="Australia/Hobart", sse_enabled=False))
    reparsed = parse_toml(render_from_config(updated))
    assert reparsed.rest.tz == "Australia/Hobart"
    assert reparsed.rest.sse_enabled is False


def test_sse_enabled_must_be_bool() -> None:
    body = DEFAULT_TOML.replace("sse_enabled = true", 'sse_enabled = "yes"')
    with pytest.raises(ValueError, match="sse_enabled must be bool"):
        parse_toml(body)


# --- relay / cache / ota / environment sections --------------------------------


def test_relay_defaults_round_trip() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.relay.relay_url == "https://relay.tesserae.ink"
    assert cfg.relay.frame_key == ""
    assert cfg.relay.private_key == ""


def test_relay_mode_requires_a_url() -> None:
    body = DEFAULT_TOML.replace('transport_mode = "rest"', 'transport_mode = "relay"')
    body = body.replace('relay_url = "https://relay.tesserae.ink"', 'relay_url = ""')
    with pytest.raises(ValueError, match="relay_url is required"):
        parse_toml(body)


def test_relay_transport_mode_is_accepted() -> None:
    body = DEFAULT_TOML.replace('transport_mode = "rest"', 'transport_mode = "relay"')
    assert parse_toml(body).transport_mode == "relay"


def test_dropped_pairing_still_round_trips() -> None:
    """A revoked pairing legitimately leaves neither key nor code. The daemon
    writes that state, so the parser must be able to read it back — otherwise
    a restart after revocation cannot start."""
    cfg = parse_toml(DEFAULT_TOML.replace('transport_mode = "rest"',
                                          'transport_mode = "relay"'))
    dropped = replace(
        cfg,
        relay=replace(cfg.relay, frame_key="", pairing_code="", device_token=""),
    )
    reparsed = parse_toml(render_from_config(dropped))
    assert reparsed.relay.frame_key == ""


def test_cache_defaults_on_with_xdg_root() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.cache.enabled is True
    assert cfg.cache.root == ""
    assert cfg.cache.root_path().name == "tesserae-pi-bin-client"


def test_cache_root_expands_user() -> None:
    body = DEFAULT_TOML.replace('root = ""', 'root = "~/frames"')
    assert "~" not in str(parse_toml(body).cache.root_path())


def test_ota_reporting_defaults_on() -> None:
    assert parse_toml(DEFAULT_TOML).ota.report_enabled is True


def test_environment_sensor_defaults_off() -> None:
    cfg = parse_toml(DEFAULT_TOML)
    assert cfg.environment.sensor == ""
    assert cfg.environment.i2c_bus == 1


def test_environment_rejects_an_unknown_sensor() -> None:
    body = DEFAULT_TOML.replace('sensor = ""', 'sensor = "bme280"')
    with pytest.raises(ValueError, match="sensor unknown"):
        parse_toml(body)


def test_environment_accepts_known_sensors() -> None:
    for name in ("sht4x", "shtc3"):
        body = DEFAULT_TOML.replace('sensor = ""', f'sensor = "{name}"')
        assert parse_toml(body).environment.sensor == name
