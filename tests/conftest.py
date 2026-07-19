"""Shared test fixtures for mcp-dispatch.

server.py runs work at import time (_setup_dirs + _claim_id), and reads its
configuration from environment variables. To test it in isolation we reload
the module fresh per test with a temp dispatch dir and a controlled agent id.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_ENV_KEYS = [
    "MCP_DISPATCH_DIR",
    "DISPATCH_DIR",
    "MCP_DISPATCH_AGENT_ID",
    "MCP_DISPATCH_CONFIG",
]


def load_server(dispatch_dir, agent_id="alpha", *, config_path=None, extra_env=None):
    """(Re)import server.py with a controlled environment.

    Returns the freshly imported module. Raising during import (e.g. an
    invalid agent id) propagates to the caller, which is what several
    security tests assert on.
    """
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["MCP_DISPATCH_DIR"] = str(dispatch_dir)
    if agent_id is not None:
        os.environ["MCP_DISPATCH_AGENT_ID"] = agent_id
    # Always pin the config path so tests never pick up the user's real
    # ~/.config/mcp-dispatch/config.toml. A test that wants config supplies one;
    # otherwise point at a guaranteed-absent file → built-in defaults.
    os.environ["MCP_DISPATCH_CONFIG"] = str(
        config_path if config_path is not None else dispatch_dir.parent / "no-such-config.toml"
    )
    if extra_env:
        os.environ.update(extra_env)

    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.fixture
def server_factory(tmp_path):
    """Factory that loads a server instance against an isolated temp dir."""
    dispatch_dir = tmp_path / "messages"

    def _make(agent_id="alpha", *, config_path=None, extra_env=None):
        return load_server(dispatch_dir, agent_id, config_path=config_path, extra_env=extra_env)

    _make.dispatch_dir = dispatch_dir
    return _make


@pytest.fixture
def server(server_factory):
    """A loaded server instance as agent 'alpha'."""
    return server_factory("alpha")
