"""First-time config file generator driven by scripts/install.sh.

Reads values from environment variables, writes a config.toml at the
configured path, and validates the result by round-tripping through the
real parser. Exits non-zero (with a readable error) if the values don't
pass validation — install.sh surfaces that and the user can re-run.

Env vars (all optional — unset means "use the default"):

    T_CONFIG_PATH        target path (default: DEFAULT_CONFIG_PATH)
    T_TRANSPORT_MODE     "rest" (default) | "mqtt"
    T_REST_SERVER_URL    e.g. http://tesserae.local:8765 (rest mode)
    T_REST_PAIRING_CODE  optional 6-digit code; blank to use the discover loop
    T_MQTT_HOST
    T_MQTT_PORT
    T_MQTT_USERNAME
    T_MQTT_PASSWORD
    T_MQTT_CLIENT_ID
    T_DEVICE_ID          device id (default "pi_bin"); see config.DEVICE_ID_RE
    T_PANEL_MODEL
    T_OVERWRITE          "1" to overwrite an existing file; otherwise abort
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, parse_toml, render_config_toml


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value != "" else None


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"environment {name}={value!r} is not an integer") from exc


def main() -> int:
    target = Path(os.environ.get("T_CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    overwrite = os.environ.get("T_OVERWRITE") == "1"

    if target.exists() and not overwrite:
        print(f"config already exists at {target}; leaving it alone", file=sys.stderr)
        return 0

    overrides: dict[str, str | int | bool] = {}
    for env_name, kw in [
        ("T_TRANSPORT_MODE", "transport_mode"),
        ("T_REST_SERVER_URL", "rest_server_url"),
        ("T_REST_PAIRING_CODE", "rest_pairing_code"),
        ("T_MQTT_HOST", "mqtt_host"),
        ("T_MQTT_USERNAME", "mqtt_username"),
        ("T_MQTT_PASSWORD", "mqtt_password"),
        ("T_MQTT_CLIENT_ID", "mqtt_client_id"),
        ("T_DEVICE_ID", "device_id"),
        ("T_PANEL_MODEL", "panel_model"),
    ]:
        val = _env_str(env_name)
        if val is not None:
            overrides[kw] = val
    port = _env_int("T_MQTT_PORT")
    if port is not None:
        overrides["mqtt_port"] = port

    if os.environ.get("T_BUTTONS_ENABLED") == "1":
        overrides["buttons_enabled"] = True

    # Button C is the one pin that moves between Impression sizes: GPIO 16 on
    # the 4"/5.7"/7.3", GPIO 25 on the 13.3" (Pimoroni's own examples carry
    # the same caveat). Derive it from the chosen panel so a fresh install is
    # correct without the user knowing that, while an explicit override still
    # wins for a board we haven't seen.
    pin_c = _env_int("T_BUTTONS_PIN_C")
    if pin_c is not None:
        overrides["buttons_pin_c"] = pin_c
    elif overrides.get("panel_model", "inky_13_3") != "inky_13_3":
        overrides["buttons_pin_c"] = 16

    body = render_config_toml(**overrides)  # type: ignore[arg-type]

    # Round-trip through the real parser before writing — catches bad ports,
    # unknown panel models, etc. up front so install.sh can fail loudly.
    parse_toml(body)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
