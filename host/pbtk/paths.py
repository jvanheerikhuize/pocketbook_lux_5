"""Where pbtk keeps its state, and where the toolkit's device files live."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def state_dir() -> Path:
    """Activity log, screenshots, token, cached agent address, probe facts."""
    base = os.environ.get("PBTK_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "pbtk"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def shots_dir() -> Path:
    p = state_dir() / "shots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def toolkit_root() -> Path:
    """The pocketbook_lux_5 checkout (device/, sdk/, templates/)."""
    env = os.environ.get("PBTK_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def device_dir() -> Path:
    return toolkit_root() / "device"


def token() -> str:
    """The shared secret apps' agents require. Created on first use."""
    env = os.environ.get("PBAGENT_TOKEN")
    if env:
        return env
    path = state_dir() / "token"
    if not path.exists():
        path.write_text(secrets.token_hex(16) + "\n")
        path.chmod(0o600)
    return path.read_text().strip()
