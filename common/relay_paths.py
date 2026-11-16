"""Resolve the local directory used for CLIProxy-compatible auth files.

Production installs may mount the relay service directory at the historical
``/opt/codex-services/data/cliproxy/auths`` path.  Standalone panel installs do
not always have that companion service, so keep a project-local directory as a
working fallback.  The choice is made when the web process imports the module;
restarting after adding a mount automatically switches back to the mounted
directory.
"""

from __future__ import annotations

import os
from pathlib import Path


EXTERNAL_RELAY_AUTHS_DIR = Path(
    os.environ.get(
        "CLIPROXY_AUTHS_DIR",
        "/opt/codex-services/data/cliproxy/auths",
    )
)
LOCAL_RELAY_AUTHS_DIR = Path(__file__).resolve().parents[1] / "data" / "cliproxy" / "auths"


def default_relay_auths_dir() -> Path:
    """Use the mounted relay directory when present, otherwise local storage."""

    return EXTERNAL_RELAY_AUTHS_DIR if EXTERNAL_RELAY_AUTHS_DIR.is_dir() else LOCAL_RELAY_AUTHS_DIR

