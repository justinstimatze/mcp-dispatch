"""The nick a directory produces, and the two implementations that must agree.

bin/dispatch-launcher computes it in shell, before it can import anything;
dispatch_fs.nick_for_dir is the Python half the supervisor uses to answer "what
would a session started here actually be called?" without launching one. A drift
between them is invisible until a supervised nick silently never goes live, so
the agreement is pinned end-to-end against the real launcher rather than against
a reimplementation of its pipeline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dispatch_fs  # noqa: E402

LAUNCHER = REPO_ROOT / "bin" / "dispatch-launcher"

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the launcher execs `uv run`")


def _env(tmp_path: Path, relay: Path) -> dict[str, str]:
    """The launcher's environment, minus anything that would pin its identity.

    PATH is inherited rather than pinned: the launcher execs `uv`, which lives in
    ~/.local/bin here and is not on a minimal PATH. HOME is redirected because
    make test-ci already runs under a throwaway HOME, so the test has to work
    without a warm uv cache either way.
    """
    env = dict(os.environ)
    env.pop("MCP_DISPATCH_AGENT_ID", None)  # would win over the directory rule
    env.pop("MCP_DISPATCH_CWD", None)
    env.update(
        HOME=str(tmp_path),
        MCP_DISPATCH_DIR=str(relay),
        MCP_DISPATCH_CONFIG=str(tmp_path / "no-such-config.toml"),
    )
    return env


# Each is a directory name the rule has to bend: case, punctuation that is
# dropped outright, a leading hyphen the id grammar forbids, and a dotted name
# whose dots vanish rather than becoming separators.
TRICKY = ["Stope", "aipotluck.org", "-leading", "my_project", "UPPER-Case"]


def test_nick_for_dir_matches_the_documented_rule():
    assert dispatch_fs.nick_for_dir("/home/x/code/webapp") == "webapp"
    assert dispatch_fs.nick_for_dir("/home/x/Documents") == "documents"
    assert dispatch_fs.nick_for_dir("/home/x/Documents/") == "documents", "trailing slash"
    assert dispatch_fs.nick_for_dir("/home/x/aipotluck.org") == "aipotluckorg"
    assert dispatch_fs.nick_for_dir("/home/x/-leading") == "leading"


def test_a_directory_with_no_usable_characters_yields_nothing():
    """The launcher falls back to `agent` here. nick_for_dir returns "" instead,
    so a caller reasoning about identity is never handed a name that belongs to
    every unusable directory at once."""
    assert dispatch_fs.nick_for_dir("/home/x/...") == ""
    assert dispatch_fs.nick_for_dir("/") == ""


def test_the_nick_is_capped_below_the_id_limit():
    # 50 chars, leaving room for "-<pid>" under the 64-char id grammar.
    assert len(dispatch_fs.nick_for_dir("/tmp/" + "a" * 80)) == 50  # nosec B108


@needs_uv
@pytest.mark.parametrize("dirname", TRICKY)
def test_the_shell_launcher_agrees_with_the_python_half(dirname, tmp_path):
    """Runs the real launcher, not a copy of its pipeline.

    It execs a server that blocks on stdin, so feed it EOF and let it claim
    presence against a throwaway relay; the id it registers is the answer.
    """
    project = tmp_path / dirname
    project.mkdir()
    relay = tmp_path / "relay"

    subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(LAUNCHER)],
        cwd=project,
        env=_env(tmp_path, relay),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        check=False,
    )

    claimed = [json.loads(p.read_text())["agent_id"] for p in (relay / ".presence").glob("*.json")]
    assert len(claimed) == 1, f"expected one session, got {claimed}"
    nick, _, pid = claimed[0].rpartition("-")
    assert pid.isdigit(), f"launcher id should end in a pid: {claimed[0]}"
    assert nick == dispatch_fs.nick_for_dir(str(project))


@needs_uv
@pytest.mark.parametrize("dirname", TRICKY)
def test_the_launcher_records_the_launch_directory(dirname, tmp_path):
    project = tmp_path / dirname
    project.mkdir()
    relay = tmp_path / "relay"

    subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(LAUNCHER)],
        cwd=project,
        env=_env(tmp_path, relay),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        check=False,
    )

    presence = list((relay / ".presence").glob("*.json"))
    assert len(presence) == 1
    rec = json.loads(presence[0].read_text())
    assert Path(rec["cwd"]).resolve() == project.resolve()
