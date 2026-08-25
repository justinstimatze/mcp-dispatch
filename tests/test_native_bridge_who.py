"""Server-side native-bridge surface: peek provenance + who()'s native roster.

Mirrors test_git_hybrid.py's shape exactly — the bits server.py shows to
agents (a `via: "native-bridge"` tag, and a `native` list in who()) — using the
reloadable `server` fixture from conftest. who() stays bridge-agnostic (no
import of bridge_native.py), so these tests only need to write the on-disk
files a NativeInboundListener/NativeBridge would have written; bridge_native.py
itself is covered directly in test_bridge_native.py.
"""

from __future__ import annotations

import json


def _native_msg(to: str, content: str) -> dict:
    return {
        "id": "msg-native1",
        "from": "native-bridge",
        "to": to,
        "timestamp": "2026-06-24T00:00:00Z",
        "priority": "normal",
        "content": content,
        "payload": {"claimed_from": "uds:/tmp/cc-socks/someone.sock"},
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
        "_via": "native-bridge",  # as materialized by NativeInboundListener
    }


def test_peek_surfaces_native_bridge_via(server):
    inbox = server.DISPATCH_DIR / "alpha"
    inbox.mkdir(parents=True, exist_ok=True)
    server._atomic_write(
        inbox / "1-native-bridge-aaaaaaaa.json", _native_msg("alpha", "from a native peer")
    )

    out = server.peek_tool()
    assert out["count"] == 1
    msg = out["messages"][0]
    assert msg["via"] == "native-bridge"
    assert msg["content"] == "from a native peer"
    # Internal underscore fields never leak to the wire.
    assert not any(k.startswith("_") for k in msg)


def test_who_includes_native_roster(server):
    native = server.DISPATCH_DIR / ".native"
    native.mkdir(parents=True, exist_ok=True)
    (native / "carol.json").write_text(json.dumps({"name": "carol", "via": "native"}))

    out = server.who_tool()
    assert out["native_count"] == 1
    assert out["native"][0]["name"] == "carol"
    # carol is not double-counted as a live local agent.
    assert "carol" not in {a.get("agent_id") for a in out["agents"]}


def test_live_local_shadows_native_entry(server):
    # alpha is live-local (the fixture holds its presence flock); a native
    # roster entry that happens to share its name must be hidden so who()
    # shows one truth per id.
    native = server.DISPATCH_DIR / ".native"
    native.mkdir(parents=True, exist_ok=True)
    (native / "alpha.json").write_text(json.dumps({"name": "alpha", "via": "native"}))

    out = server.who_tool()
    assert "native" not in out  # only entry was shadowed -> list empty -> omitted
    assert "alpha" in {a.get("agent_id") for a in out["agents"]}


def test_who_omits_native_key_when_roster_dir_absent(server):
    # No .native/ directory at all (dispatch-ucbridge never ran) must not error
    # or add an empty key — matches .remote/'s absent-directory behavior.
    out = server.who_tool()
    assert "native" not in out
