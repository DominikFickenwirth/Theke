"""Tests for the Theke CLI (config / DB / CLI skeleton)."""

import json

import pytest

from theke import Config, ConfigError, greeting, load_config, main


# ---------------------------------------------------------------- placeholder

def test_greeting_returns_hallo_welt():
    assert greeting() == "Hallo Welt"


def test_main_prints_greeting(capsys):
    main()
    assert capsys.readouterr().out.strip() == "Hallo Welt"


# --------------------------------------------------------------------- config

def write_config(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no theke.json here
    cfg = load_config(None)
    assert cfg == Config()
    assert cfg.db_path == "theke.db"


def test_config_default_path_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path / "theke.json", {"db_path": "other.db"})
    assert load_config(None).db_path == "other.db"


def test_config_explicit_file_overrides_defaults(tmp_path):
    path = tmp_path / "custom.json"
    write_config(path, {"db_path": "x/y.db"})
    assert load_config(str(path)).db_path == "x/y.db"


def test_config_explicit_missing_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.json"))


def test_config_empty_object_keeps_defaults(tmp_path):
    path = tmp_path / "empty.json"
    write_config(path, {})
    assert load_config(str(path)) == Config()


def test_config_unknown_key_is_error(tmp_path):
    path = tmp_path / "typo.json"
    write_config(path, {"db_pathh": "x.db"})
    with pytest.raises(ConfigError, match="db_pathh"):
        load_config(str(path))


def test_config_wrong_type_is_error(tmp_path):
    path = tmp_path / "typed.json"
    write_config(path, {"db_path": 42})
    with pytest.raises(ConfigError, match="db_path"):
        load_config(str(path))


def test_config_broken_json_is_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="broken.json"):
        load_config(str(path))


def test_config_empty_file_is_error(tmp_path):
    path = tmp_path / "void.json"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_config_non_object_json_is_error(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="object"):
        load_config(str(path))


def test_config_cli_overrides_file(tmp_path):
    path = tmp_path / "prec.json"
    write_config(path, {"db_path": "file.db"})
    cfg = load_config(str(path), overrides={"db_path": "cli.db"})
    assert cfg.db_path == "cli.db"


def test_config_none_override_keeps_file_value(tmp_path):
    path = tmp_path / "prec.json"
    write_config(path, {"db_path": "file.db"})
    cfg = load_config(str(path), overrides={"db_path": None})
    assert cfg.db_path == "file.db"
