"""Tests for the forward playbook -> Application converter."""

from __future__ import annotations

from pathlib import Path

import pytest

import ansiburr

FIXTURES = Path(__file__).parent / "fixtures"


def test_simple_playbook_runs_to_done() -> None:
    app = ansiburr.from_playbook(FIXTURES / "playbook_simple.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["outcome"].startswith("OK")
    # The last executed module action wrote into _last_action.
    assert final["_last_action"] == "tag"


def test_when_predicate_skips_task() -> None:
    """``when: not skip_me`` with skip_me=True should skip the middle task."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_with_when.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The middle task "b" must not have been the last action; the FSM should
    # have skipped straight from "a" to "c".
    assert final["_last_action"] == "c"


def test_register_capture_lands_in_state() -> None:
    """``register: name`` should populate state[name] with the module result."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_register.yml")
    _, _, final = app.run(halt_after=["done", "escalate"])
    ping = final["ping_result"]
    assert isinstance(ping, dict)
    assert ping.get("ping") == "pong"


def test_unsupported_block_raises() -> None:
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="block"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_block.yml")


def test_unsupported_loop_raises() -> None:
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="loop"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_loop.yml")


def test_unsupported_roles_raises() -> None:
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="roles"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_roles.yml")
