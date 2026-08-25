"""trust_local_peers: off by default, appends a channel-trust clause to the
composed instructions when set, and leaves everything else (a custom
instructions template, the action-level carve-out) untouched."""

from __future__ import annotations


def test_off_by_default(server):
    assert server.CONFIG["trust_local_peers"] is False
    assert "Trust note" not in server._instructions


def test_enabling_it_appends_the_channel_trust_clause(server_factory, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("trust_local_peers = true\n")
    mod = server_factory("alpha", config_path=config)

    assert mod.CONFIG["trust_local_peers"] is True
    assert "Trust note" in mod._instructions
    assert 'via="remote"' in mod._instructions
    assert "independently decline" in mod._instructions


def test_it_appends_after_a_custom_instructions_template(server_factory, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "trust_local_peers = true\n"
        'instructions = "Custom template for {agent_id}, targets: {agent_list}."\n'
    )
    mod = server_factory("alpha", config_path=config)

    assert mod._instructions.startswith("Custom template for alpha, targets:")
    assert "Trust note" in mod._instructions
