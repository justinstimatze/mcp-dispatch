# Wire fixtures — cross-language interop corpus

The mcp-dispatch/leat git-transport wire format has been reimplemented in Go
(`leat`) and asserted compatible with this repo's Python reference
(`git_transport.py`) since leat's first commit — but never actually checked
against it. This directory is that check.

## Contract

- `manifest.json` lists every case file and the `wire_version`
  (`git_transport.WIRE_VERSION`) they were generated against. A consumer
  vendoring this directory should pin to a specific mcp-dispatch tag/commit,
  not a live checkout, and re-sync when `wire_version` bumps.
- Each `<case>.json` carries:
  - `envelope` — the canonical field values, keyed exactly as the wire field
    names (`from`, not `from_`).
  - `jsonl` — this repo's own serialization of those fields, for exactly one
    purpose: proving THIS repo's serializer is stable, not as something
    another language's serializer should reproduce byte-for-byte.

## What "compatible" means here — read before writing a loader

**Struct-level equivalence, not byte-identical serialization.** This
repo's `Envelope.to_json()` always writes all 11 header keys, nulls included.
leat's Go struct uses `omitempty` on the nullable fields (`to`, `chan`, `key`,
`ttl`, `sig`) — an omitted key and an explicit `null` are wire-equivalent by
design (leat's own `envelope.go` docstring says so), so a byte-for-byte
comparison across languages would fail on exactly the cases most worth
covering (see `lww-atom-seq-zero` and `ttl-zero-equals-never-expire-null-body`
below) for no real interop reason.

The check that actually matters, in both directions:

1. **Read:** parse `jsonl` with your language's decoder → the resulting
   struct's fields must equal `envelope`.
2. **Write:** construct your language's envelope type from `envelope`,
   serialize it → decode your OWN output and confirm it equals `envelope`
   again (intra-language round-trip stability — this is where
   "byte-identical" is a fair thing to assert: against your own previous
   output, not against the other language's).

`jsonl` is provided so a reader doesn't have to trust their own encoder to
produce a valid test input — it's this repo's independently-generated,
known-good wire bytes for case 1.

## Cases

| file | exercises |
|---|---|
| `basic-dm.json` | plain DM, all optional fields unset |
| `channel-post.json` | channel post, finite TTL |
| `lww-atom-seq-zero.json` | LWW state record; `seq=0` is a real value, not absence |
| `unicode-and-symbol-body.json` | non-ASCII sender id; CJK, emoji, and `<`/`&` in body |
| `signed-message.json` | `sig` populated (v1: carried, unenforced) |
| `ttl-zero-equals-never-expire-null-body.json` | `ttl=0` ≡ `ttl=null`; `body=null` |

## Regenerating

Never hand-edit a case file. Add or change an entry in `generate.py`'s
`CASES` list and re-run:

```bash
python3 tests/interop/wire-fixtures/generate.py
```

This regenerates every `jsonl` from the real `Envelope` encoder (with an
in-generator round-trip self-check before anything is written to disk), so a
fixture can never silently drift from what this repo's serializer actually
produces.
