"""This repo's own half of the cross-language interop contract in
tests/interop/wire-fixtures/ — see that directory's README for the full
contract leat (and any other reimplementation) builds its loader against.

Asserts what this repo can assert about itself: `jsonl` parses back to
`envelope`, and re-serializing `envelope` reproduces `jsonl` exactly (this
repo's own round-trip stability — see the README for why cross-language
byte-identity is deliberately NOT the bar).
"""

from __future__ import annotations

import json
from pathlib import Path

from git_transport import WIRE_VERSION, Envelope

FIXTURES_DIR = Path(__file__).resolve().parent / "interop" / "wire-fixtures"


def _load_manifest() -> dict:
    return json.loads((FIXTURES_DIR / "manifest.json").read_text())


def _load_case(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text())


def test_manifest_lists_every_fixture_file_and_none_extra():
    manifest = _load_manifest()
    on_disk = {p.name for p in FIXTURES_DIR.glob("*.json") if p.name != "manifest.json"}
    assert set(manifest["cases"]) == on_disk


def test_manifest_wire_version_matches_this_repos_encoder():
    # The whole point of pinning wire_version in the manifest: a consumer
    # vendoring this directory needs to know which encoder generated it. If
    # this drifts from the real constant, every vendored copy is silently
    # stale from the moment it's committed.
    assert _load_manifest()["wire_version"] == WIRE_VERSION


def _envelope_from_fields(fields: dict) -> Envelope:
    return Envelope(
        type=fields["type"],
        from_=fields["from"],
        body=fields["body"],
        to=fields["to"],
        chan=fields["chan"],
        key=fields["key"],
        id=fields["id"],
        ts=fields["ts"],
        seq=fields["seq"],
        ttl=fields["ttl"],
        version=fields["version"],
        sig=fields["sig"],
    )


def _fields_of(env: Envelope) -> dict:
    return {
        "type": env.type,
        "from": env.from_,
        "to": env.to,
        "chan": env.chan,
        "key": env.key,
        "id": env.id,
        "ts": env.ts,
        "seq": env.seq,
        "ttl": env.ttl,
        "version": env.version,
        "sig": env.sig,
        "body": env.body,
    }


def _case_filenames() -> list[str]:
    return _load_manifest()["cases"]


def test_every_fixtures_jsonl_parses_to_its_declared_envelope():
    for filename in _case_filenames():
        case = _load_case(filename)
        parsed = Envelope.from_json(case["jsonl"])
        assert _fields_of(parsed) == case["envelope"], filename


def test_every_fixtures_envelope_reserializes_to_its_declared_jsonl():
    # Intra-language round-trip stability — the one place "byte-identical" is
    # a fair bar (against this repo's own prior output), per the README.
    for filename in _case_filenames():
        case = _load_case(filename)
        env = _envelope_from_fields(case["envelope"])
        assert env.to_json() == case["jsonl"], filename


def test_seq_zero_is_not_treated_as_absent():
    """The one field where omitempty-vs-null wire-equivalence would be wrong
    if it applied: seq=0 is a real first record. Confirm this repo's own
    round-trip never collapses it to something else."""
    case = _load_case("lww-atom-seq-zero.json")
    assert case["envelope"]["seq"] == 0
    parsed = Envelope.from_json(case["jsonl"])
    assert parsed.seq == 0


def test_ttl_zero_and_ttl_null_are_wire_equivalent_on_this_side():
    """leat's Go struct omits ttl=0 the same way it omits ttl=null (both mean
    never-expire). Confirm this repo's own reader treats an explicit 0 the
    same way `_send`'s callers already rely on None being treated."""
    case = _load_case("ttl-zero-equals-never-expire-null-body.json")
    assert case["envelope"]["ttl"] == 0
    parsed = Envelope.from_json(case["jsonl"])
    assert parsed.ttl == 0  # decodes literally; callers treat 0 and None the same
