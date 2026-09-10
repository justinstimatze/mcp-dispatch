#!/usr/bin/env python3
"""Regenerate this directory's fixture JSON files from CASES below.

Run from the repo root: python3 tests/interop/wire-fixtures/generate.py

Each fixture is generated FROM the real git_transport.Envelope encoder rather
than hand-typed, so `jsonl` can never silently drift from what this repo's own
serializer actually produces — the one thing a hand-maintained fixture corpus
cannot guarantee. To add a case: add an entry to CASES and re-run.

manifest.json lists every case file, in order, pinned to this repo's
git_transport.WIRE_VERSION — a Go (or any other language) consumer reads the
manifest to know which files exist and which wire version they were generated
against, without listing the directory itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from git_transport import WIRE_VERSION, Envelope  # noqa: E402

HERE = Path(__file__).resolve().parent

# (name, description, kwargs for Envelope — `from_` maps to the wire's `from`)
CASES: list[tuple[str, str, dict]] = [
    (
        "basic-dm",
        "Plain DM with all optional fields unset.",
        {
            "type": "message",
            "from_": "alice",
            "to": "bob",
            "chan": None,
            "key": None,
            "id": "msg-abc12345",
            "ts": "2026-01-01T00:00:00Z",
            "seq": 1,
            "ttl": None,
            "version": 1,
            "sig": None,
            "body": {"content": "hello"},
        },
    ),
    (
        "channel-post",
        "Channel post with a finite TTL.",
        {
            "type": "message",
            "from_": "alice",
            "to": None,
            "chan": "eng",
            "key": None,
            "id": "msg-def67890",
            "ts": "2026-01-01T00:05:00Z",
            "seq": 2,
            "ttl": 3600,
            "version": 1,
            "sig": None,
            "body": {"content": "deploy started"},
        },
    ),
    (
        "lww-atom-seq-zero",
        "LWW state record (key set) at seq=0 — a real first record, not absence.",
        {
            "type": "atom",
            "from_": "carol",
            "to": None,
            "chan": None,
            "key": "slot-1",
            "id": "rec-000001",
            "ts": "2026-01-01T00:10:00Z",
            "seq": 0,
            "ttl": None,
            "version": 1,
            "sig": None,
            "body": {"s": 1},
        },
    ),
    (
        "unicode-and-symbol-body",
        "Non-ASCII sender id and a body mixing CJK, emoji, and HTML-special chars.",
        {
            "type": "message",
            "from_": "日本語",
            "to": "bob",
            "chan": None,
            "key": None,
            "id": "msg-unicode1",
            "ts": "2026-01-01T00:15:00Z",
            "seq": 3,
            "ttl": None,
            "version": 1,
            "sig": None,
            "body": {"content": "héllo wörld \U0001f389 日本語テスト <b>&</b>"},
        },
    ),
    (
        "signed-message",
        "sig populated (v1: carried but unenforced on both sides).",
        {
            "type": "message",
            "from_": "alice",
            "to": "bob",
            "chan": None,
            "key": None,
            "id": "msg-signed01",
            "ts": "2026-01-01T00:20:00Z",
            "seq": 4,
            "ttl": None,
            "version": 1,
            "sig": "ed25519:deadbeefcafefeed",
            "body": {"content": "signed message"},
        },
    ),
    (
        "ttl-zero-equals-never-expire-null-body",
        "ttl=0 and ttl=null are wire-equivalent (both mean never-expire); body is null.",
        {
            "type": "ack",
            "from_": "bob",
            "to": "alice",
            "chan": None,
            "key": None,
            "id": "msg-ack00001",
            "ts": "2026-01-01T00:25:00Z",
            "seq": 5,
            "ttl": 0,
            "version": 1,
            "sig": None,
            "body": None,
        },
    ),
]


def main() -> None:
    names = []
    for name, description, kwargs in CASES:
        env = Envelope(**kwargs)
        jsonl = env.to_json()
        # Self-check: this repo's own round-trip must reproduce the same fields
        # before the fixture is trusted enough to write to disk.
        back = Envelope.from_json(jsonl)
        assert back.type == env.type
        assert back.from_ == env.from_
        assert back.to == env.to
        assert back.chan == env.chan
        assert back.key == env.key
        assert back.id == env.id
        assert back.ts == env.ts
        assert back.seq == env.seq
        assert back.ttl == env.ttl
        assert back.version == env.version
        assert back.sig == env.sig
        assert back.body == env.body

        envelope_fields = {
            "type": kwargs["type"],
            "from": kwargs["from_"],
            "to": kwargs["to"],
            "chan": kwargs["chan"],
            "key": kwargs["key"],
            "id": kwargs["id"],
            "ts": kwargs["ts"],
            "seq": kwargs["seq"],
            "ttl": kwargs["ttl"],
            "version": kwargs["version"],
            "sig": kwargs["sig"],
            "body": kwargs["body"],
        }
        out = {
            "name": name,
            "description": description,
            "envelope": envelope_fields,
            "jsonl": jsonl,
        }
        path = HERE / f"{name}.json"
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        names.append(f"{name}.json")
        print(f"wrote {path.relative_to(REPO_ROOT)}")

    manifest = {"wire_version": WIRE_VERSION, "cases": names}
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {(HERE / 'manifest.json').relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
