"""Unit tests for the shared notify-policy predicate (notify_policy.should_notify).

This is the single source of truth consulted by both the server's desktop-
notification poll and the bin/dispatch-wait model-wake long-poll, so it is
tested directly here (no server import needed — the function is pure).
"""

from __future__ import annotations

from notify_policy import should_notify

DM = {"to": "alpha", "from": "bob", "priority": "normal"}
BROADCAST = {"to": "all", "from": "bob", "priority": "normal"}
CHANNEL = {"to": "#eng", "from": "bob", "priority": "normal"}


def test_none_never_notifies_even_must_read():
    assert not should_notify(DM, "none", "alpha")
    assert not should_notify({"to": "alpha", "must_read": True}, "none", "alpha")
    assert not should_notify({"priority": "urgent"}, "none", "alpha")


def test_all_always_notifies():
    assert should_notify(DM, "all", "alpha")
    assert should_notify(BROADCAST, "all", "alpha")
    assert should_notify(CHANNEL, "all", "alpha")


def test_direct_wakes_on_dm_and_not_broadcast():
    assert should_notify(DM, "direct", "alpha")
    assert not should_notify(BROADCAST, "direct", "alpha")
    # A DM addressed to someone else is not mine.
    assert not should_notify({"to": "beta"}, "direct", "alpha")


def test_direct_wakes_on_a_subscribed_channel():
    # Subscribing IS the opt-in, so a post to a room I joined is "addressed to me".
    assert should_notify(CHANNEL, "direct", "alpha", ["eng"])
    assert should_notify(CHANNEL, "direct", "alpha", ["ops", "eng"])


def test_direct_ignores_channels_i_did_not_join():
    assert not should_notify(CHANNEL, "direct", "alpha", ["ops"])
    # No channels supplied at all (e.g. an unreadable presence file) fails closed.
    assert not should_notify(CHANNEL, "direct", "alpha")
    assert not should_notify(CHANNEL, "direct", "alpha", [])


def test_subscribed_channel_does_not_leak_into_broadcast():
    # "all" is not a room anyone joined — a subscription must not make it wake.
    assert not should_notify(BROADCAST, "direct", "alpha", ["eng", "all"])


def test_direct_does_not_filter_dms_by_priority():
    # Directedness is the signal: a normal-priority DM still wakes.
    assert should_notify({"to": "alpha", "priority": "normal"}, "direct", "alpha")


def test_must_read_pierces_direct_even_on_broadcast():
    assert should_notify({"to": "all", "must_read": True}, "direct", "alpha")
    assert should_notify({"to": "#eng", "must_read": True}, "direct", "alpha")


def test_urgent_broadcast_does_not_wake_under_direct():
    # Decision: urgent is too cheap to honor at fan-out scale; only must_read pierces.
    assert not should_notify({"to": "all", "priority": "urgent"}, "direct", "alpha")


def test_important_is_urgent_plus_must_read():
    assert should_notify({"priority": "urgent"}, "important", "alpha")
    assert should_notify({"must_read": True}, "important", "alpha")
    assert not should_notify({"priority": "normal"}, "important", "alpha")


def test_unresolved_identity_fails_closed_under_direct():
    assert not should_notify(DM, "direct", None)
    assert not should_notify(DM, "direct", "")
    # ...but must_read still pierces regardless of identity.
    assert should_notify({"to": "all", "must_read": True}, "direct", None)


NATIVE_MUST_READ = {"to": "all", "must_read": True, "_via": "native-bridge"}


def test_untrusted_native_bridge_must_read_does_not_pierce():
    # A message that only crossed the untrusted native-protocol bridge cannot
    # force a wake purely by claiming must_read — that would let any local
    # process able to open a socket escalate itself into attention.
    assert not should_notify(NATIVE_MUST_READ, "direct", "alpha")
    assert not should_notify(NATIVE_MUST_READ, "important", "alpha")
    assert not should_notify(NATIVE_MUST_READ, "none", "alpha")


def test_untrusted_native_bridge_still_notifies_via_normal_policy():
    # The carve-out only removes the must_read override — ordinary policy
    # matching is untouched.
    assert should_notify(NATIVE_MUST_READ, "all", "alpha")
    dm = {"to": "alpha", "must_read": True, "_via": "native-bridge"}
    assert should_notify(dm, "direct", "alpha")  # addressed to me either way


def test_trust_wake_opt_in_restores_the_pierce():
    assert should_notify(NATIVE_MUST_READ, "direct", "alpha", native_bridge_trusted=True)
    assert should_notify(NATIVE_MUST_READ, "important", "alpha", native_bridge_trusted=True)


def test_git_transport_must_read_is_unaffected_by_the_native_carveout():
    # The carve-out is scoped to `_via == "native-bridge"` specifically — a
    # cross-host git message's must_read still pierces as before.
    git_msg = {"to": "all", "must_read": True, "_via": "git"}
    assert should_notify(git_msg, "direct", "alpha")
    assert should_notify(git_msg, "important", "alpha")
