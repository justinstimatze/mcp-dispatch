"""group_mode tests — the opt-in for sharing one relay across trusting accounts.

Owner-only is the default and is covered in test_security. Here we verify that
turning group_mode on relaxes perms to group-rw with a setgid relay dir, and
that a server tolerates a pre-existing relay it does not own (the second
account to start must not crash chmod'ing a dir owned by the first).
"""

from __future__ import annotations

import stat

import pytest


def _group_config(tmp_path):
    cfg = tmp_path / "group.toml"
    cfg.write_text("group_mode = true\n")
    return cfg


def test_relay_dir_is_setgid_and_group_rwx(server_factory, tmp_path):
    srv = server_factory("alpha", config_path=_group_config(tmp_path))
    mode = stat.S_IMODE(srv.DISPATCH_DIR.stat().st_mode)
    assert mode & stat.S_ISGID, "relay dir must be setgid so children inherit the group"
    assert mode & 0o070 == 0o070, "group needs rwx"
    assert mode & 0o007 == 0, "no access for users outside the group"


def test_message_file_is_group_readable(server_factory, tmp_path):
    srv = server_factory("alpha", config_path=_group_config(tmp_path))
    srv._send("alpha", "beta", "shared")
    f = next((srv.DISPATCH_DIR / "beta").glob("*.json"))
    mode = stat.S_IMODE(f.stat().st_mode)
    assert mode & 0o060 == 0o060, "group must be able to read/write the message"
    assert mode & 0o007 == 0, "still no access outside the group"


def test_runtime_inbox_inherits_setgid(server_factory, tmp_path):
    srv = server_factory("alpha", config_path=_group_config(tmp_path))
    srv._send("alpha", "beta", "hi")  # creates beta/ under the setgid relay
    mode = stat.S_IMODE((srv.DISPATCH_DIR / "beta").stat().st_mode)
    assert mode & stat.S_ISGID, "inbox created at runtime must inherit setgid"
    assert mode & 0o070 == 0o070


def test_group_mode_tolerates_unowned_relay(server_factory, tmp_path, monkeypatch):
    srv = server_factory("alpha", config_path=_group_config(tmp_path))

    def boom(*_a, **_k):
        raise PermissionError("not the owner")

    monkeypatch.setattr(srv.os, "chmod", boom)
    srv._setup_dirs()  # must not raise — the relay was set up by another account


def test_owner_mode_reraises_chmod_failure(server_factory, tmp_path, monkeypatch):
    srv = server_factory("alpha")  # default owner-only mode

    def boom(*_a, **_k):
        raise PermissionError("not the owner")

    monkeypatch.setattr(srv.os, "chmod", boom)
    with pytest.raises(PermissionError):
        srv._setup_dirs()  # owner-only mode must NOT silently tolerate this
