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


def test_block_rescue_plus_always_still_raises() -> None:
    """``block: + rescue:`` (v0.0.10) and ``block: + always:`` (v0.0.11) are
    each supported alone. Combining all three (block + rescue + always) is
    not yet."""
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match=r"rescue.*always"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_block.yml")


def test_block_plus_always_runs_always_after_block_success() -> None:
    """A block + always with no rescue, where the block succeeds, runs
    every always task after the block completes."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_block_always.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["block_succeeded"] is True
    assert final["always_ran"] is True


def test_block_plus_always_runs_always_after_block_failure_then_escalates() -> None:
    """A block + always where the block fails:
      - always still runs (always must run regardless of block outcome)
      - after always, the latched block failure is restored
      - the FSM reaches escalate, not done
    """
    app = ansiburr.from_playbook(FIXTURES / "playbook_block_always_failure.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "escalate"
    # Always ran even though the block failed.
    assert final["always_ran"] is True
    # The latched failure was restored before escalation.
    assert final["_last_failed"] is True


def test_block_plus_rescue_runs_rescue_on_block_failure() -> None:
    """A deliberate ``ansible.builtin.fail`` inside ``block:`` must:
      - stop block execution at the failure point,
      - run the rescue chain,
      - clear the failure sentinels so post-block tasks run cleanly,
      - reach ``done`` (not ``escalate``).
    """
    app = ansiburr.from_playbook(FIXTURES / "playbook_block_rescue.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The rescue chain ran: ``record_rescue`` set ``rescued=True``.
    assert final["rescued"] is True
    # The clear-failure post-action wiped ``_last_failed`` before the
    # post-block task executed; ``_last_failed`` is False at the end.
    assert final["_last_failed"] is False
    # The post-block task executed and registered a result.
    assert final["post_probe"]["ping"] == "pong"


def test_jinja_templated_loop_still_raises() -> None:
    """Literal-list ``loop:`` is supported. A Jinja-templated value
    (``loop: \"{{ users }}\"``) is not yet, because resolving it needs
    runtime variable evaluation we don't implement."""
    with pytest.raises(ansiburr.UnsupportedPlaybookConstruct, match="loop"):
        ansiburr.from_playbook(FIXTURES / "playbook_unsupported_loop.yml")


def test_set_fact_writes_into_state_and_jinja_resolves_downstream() -> None:
    """``set_fact:`` lowers to a pure-Python state update, and a downstream
    ``set_fact:`` that templates the freshly-written value resolves it from
    Burr state. Validates that fact-set values propagate across tasks."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_set_fact.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    assert final["greeting"] == "hello"
    assert final["target"] == "world"
    assert final["composed"] == "hello, world!"


def test_changed_when_false_suppresses_change_signal() -> None:
    """``changed_when: false`` must force ``_last_changed=False`` even when
    the underlying module reports changed. This is the standard idiom for
    read-only command/shell tasks."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_changed_when.yml")
    _, _, final = app.run(halt_after=["done", "escalate"])
    assert final["_last_changed"] is False
    # The register still landed; only the changed signal was overridden.
    assert final["probe"]["rc"] == 0


def test_literal_loop_visits_each_item_in_order() -> None:
    """A ``loop:`` with a literal list lowers to a three-action sub-FSM:
    init -> task -> advance, with a back-edge from advance to task until
    the items are exhausted. The task action exposes ``{{ item }}`` in
    its Jinja context."""
    app = ansiburr.from_playbook(FIXTURES / "playbook_with_loop.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
    assert last.name == "done"
    # The literal list landed in state via the loop_init action.
    assert final["_loop_greet_each_items"] == ["alice", "bob", "charlie"]
    # The advance walked all three; the final advance flipped done to True.
    assert final["_loop_greet_each_done"] is True
    # The final idx counter is one past the last item.
    assert final["_loop_greet_each_idx"] == 3
    # The graph has the three loop nodes.
    action_names = {a.name for a in app.graph.actions}
    assert "greet_each_loop_init" in action_names
    assert "greet_each" in action_names
    assert "greet_each_loop_advance" in action_names


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
