"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import dataclasses
import json
from dataclasses import dataclass

CONFIG_DEFAULT_PATH = "theke.json"


# --------------------------------------------------------------------- config

class ConfigError(Exception):
    """Invalid or unreadable configuration."""


@dataclass
class Config:
    """Effective configuration; defaults < config file < CLI parameters."""
    db_path: str = "theke.db"


def load_config(path: str | None, overrides: dict | None = None) -> Config:
    """Load the JSON config file and apply CLI overrides (None = not set).

    An explicitly given path must exist; the default path may be absent.
    Unknown keys and wrong value types are errors (typo protection).
    """
    explicit = path is not None
    path = path or CONFIG_DEFAULT_PATH
    fields = {f.name: f.type for f in dataclasses.fields(Config)}
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config root in {path} must be a JSON object")

    for key, value in data.items():
        if key not in fields:
            raise ConfigError(f"unknown config key in {path}: {key}")
        if not isinstance(value, fields[key]):
            raise ConfigError(
                f"config key {key} must be of type {fields[key].__name__}")
    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value
    return Config(**data)


# ---------------------------------------------------------------- placeholder

def greeting() -> str:
    """Return the placeholder greeting (real stages replace this later)."""
    return "Hallo Welt"


def main() -> None:
    """CLI entry point."""
    print(greeting())


if __name__ == "__main__":
    main()
