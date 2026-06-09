"""Shared notify-policy predicate.

Pure and side-effect-free (stdlib only, no imports beyond typing). Imported by
both the MCP server (server.py, for the OS-notification poll) and the
bin/dispatch-wait waiter (for the model-wake long-poll) so the two delivery
paths apply *identical* rules — a message that wakes the model is exactly a
message that fires a desktop notification, and vice versa.

Deliberately does NOT import server.py: that module claims an agent id and
starts threads at import time, so importing it from a short-lived CLI would
collide with the live session's server. Keep this module dependency-free.
"""

from __future__ import annotations


def should_notify(msg: dict, notify_on: str, agent_id: str | None) -> bool:
    """Return True if ``msg`` warrants notifying ``agent_id`` under ``notify_on``.

    Policies (``notify_on``):
      "none"      — never notify.
      "direct"    — notify on messages addressed to this agent (``to == agent_id``).
      "important" — notify on urgent-priority messages.
      "all"       — notify on everything.

    ``must_read`` always pierces, regardless of policy, *except* under "none"
    (an explicit opt-out stays silent). This mirrors must_read's override
    semantics elsewhere in the relay (e.g. it ignores TTL expiry).

    A None/empty ``agent_id`` simply never matches the "direct" rule, so an
    unresolved identity fails closed rather than waking on every message.
    """
    if notify_on == "none":
        return False
    if notify_on == "all":
        return True
    if msg.get("must_read"):
        return True
    if notify_on == "direct":
        return bool(agent_id) and msg.get("to") == agent_id
    # "important" (default, back-compat): urgent priority. must_read handled above.
    return msg.get("priority") == "urgent"
