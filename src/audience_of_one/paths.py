"""Public filesystem layout for Audience of One."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "audience-of-one"


def config_dir() -> Path:
    override = os.environ.get("STATION_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / APP_NAME


def state_dir() -> Path:
    override = os.environ.get("STATION_STATE_DIR")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / APP_NAME


def config_file() -> Path:
    override = os.environ.get("STATION_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.toml"
