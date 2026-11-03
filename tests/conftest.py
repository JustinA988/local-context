from __future__ import annotations

from pathlib import Path

import pytest

from local_context.config import load_settings
from local_context.server import build_app


@pytest.fixture
def settings(tmp_path: Path):
    env = {
        "LOCAL_CONTEXT_ROOT": str(tmp_path),
        "LOCAL_CONTEXT_DATA": str(tmp_path / "data"),
        "LOCAL_CONTEXT_MAX_CHARS": "4000",
        "LOCAL_CONTEXT_ALLOW_SHELL": "true",
    }
    loaded = load_settings(env, cwd=tmp_path)
    loaded.ensure_dirs()
    return loaded


@pytest.fixture
def app(settings):
    return build_app(settings)
