from __future__ import annotations

import json

import pytest

from builder.windows_service import command_and_env, load_config


def _config(tmp_path, **overrides):
    data = {
        "python_exe": "C:\\Python\\python.exe",
        "workdir": str(tmp_path),
        "public_url": "https://mai.example.test",
        "admin_password": "a-long-admin-password",
        "auth_db": str(tmp_path / "auth.db"),
        "port": 8780,
        "build": True,
        "no_backtest": True,
        "date": "20260801",
    }
    data.update(overrides)
    path = tmp_path / "shared.config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_service_config_builds_localhost_only_shared_command(tmp_path):
    cfg = load_config(_config(tmp_path))
    cmd, env, workdir = command_and_env(cfg)
    assert cmd[:4] == ["C:\\Python\\python.exe", "-m", "builder.api", "--shared"]
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "--build" in cmd and "--no-backtest" in cmd
    assert env["BUILDER_ADMIN_PASSWORD"] == "a-long-admin-password"
    assert env["BUILDER_AUTH_DB"] == str(tmp_path / "auth.db")
    assert env["PYTHONUTF8"] == "1"
    assert workdir == tmp_path.resolve()


@pytest.mark.parametrize("field,value", [
    ("public_url", "http://mai.example.test"),
    ("admin_password", "short"),
])
def test_service_config_rejects_unsafe_public_settings(tmp_path, field, value):
    with pytest.raises(ValueError):
        load_config(_config(tmp_path, **{field: value}))
