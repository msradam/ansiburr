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


def test_block_with_rescue_still_raises() -> None:
    """``block:`` group-only is supported; ``block: + rescue:/always:`` is not."""
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="rescue"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_block.yml")


def test_unsupported_loop_raises() -> None:
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="loop"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_loop.yml")


def test_unsupported_roles_raises() -> None:
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="roles"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_roles.yml")


# ---------------------------------------------------------------------------
# v0.0.4: block (group-only), include_tasks/import_tasks, notify + handlers
# ---------------------------------------------------------------------------


def test_block_group_only_flattens_to_inline_tasks() -> None:
    """``block:`` without rescue/always inlines its inner tasks into the
    main sequence with the block's ``when:`` propagated to each inner task."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_block_group.yml")
    action_names = {a.name for a in app.graph.actions}
    # The block's inner tasks should appear as top-level actions.
    assert "first_inner" in action_names
    assert "second_inner" in action_names
    # And the wrapping block: name should NOT appear.
    assert "wrap" not in action_names
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["_last_action"] == "second_inner"


def test_include_tasks_inlines_referenced_file() -> None:
    """``include_tasks: <file>`` reads the referenced YAML and inlines its
    tasks into the main sequence at conversion time."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_with_include.yml")
    action_names = {a.name for a in app.graph.actions}
    # Tasks from the included file land in the main graph.
    assert "included_alpha" in action_names
    assert "included_beta" in action_names
    last, _, _ = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"


def test_import_tasks_inlines_referenced_file() -> None:
    """``import_tasks:`` behaves identically to ``include_tasks:`` at
    conversion time (we resolve the path once and inline)."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_with_import.yml")
    action_names = {a.name for a in app.graph.actions}
    assert "included_alpha" in action_names
    assert "included_beta" in action_names


def test_handler_runs_on_change() -> None:
    """A task with ``notify:`` triggers the named handler at the end of the
    play when the task reports ``changed=True``."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_with_handlers.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The notified handler was triggered (the marker after the changing
    # task flipped the slugified flag to True).
    assert final["_notified_say_hello"] is True
    # The handler action was the last module-flavored step before done.
    action_names = [a.name for a in app.graph.actions]
    assert any(n.startswith("handler_") for n in action_names)


def test_notify_unknown_handler_raises() -> None:
    """A ``notify:`` reference to a handler name that isn't declared must
    fail at convert time rather than producing a dead reference."""
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="unknown handler"):
        ansiburr.from_playbook(FIXTURES / "playbook_notify_unknown.yml")
