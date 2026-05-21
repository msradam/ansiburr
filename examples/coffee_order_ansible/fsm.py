"""The reversal: burr-mcp's coffee_order FSM, every action body swapped for an Ansible module.

Original (burr-mcp/examples/coffee_order.py) is a small non-linear FSM with
five actions (``take_order``, ``add_modifier``, ``pay``, ``fulfill``,
``cancel``), a self-loop on ``add_modifier``, and two terminal stages.
Each action body is a pure-Python ``state.update(...)``.

This file preserves the graph topology EXACTLY and replaces each action
body with a real Ansible module call against a filesystem-as-queue::

    take_order   -> ansible.builtin.copy        writes /tmp/coffee/active.json
    add_modifier -> ansible.builtin.copy        rewrites file with new modifier
    pay          -> ansible.builtin.copy        rewrites with status=paid + paid_amount
    fulfill      -> ansible.builtin.command     mv active.json -> done/<ts>.json
    cancel       -> ansible.builtin.file        state=absent

The Burr state still tracks the order shape (``item``, ``qty``, ``modifiers``,
``total``, ``stage``) so transitions and the agent-facing schema are
identical; the difference is that **the source of truth is the file on
disk**, written by Ansible modules. The same Burr graph can be backed by
mutating Python objects (the original) or persistent infrastructure
operations (this file). That's the substrate flexibility ansiburr aims for.

Uses ansiburr features:
- Burr per-step inputs (``item``, ``qty``, ``modifier``, ``amount``)
- Tuple-return form of ``@module_action`` (args + state overrides) so
  app-level fields land in state even though Ansible's copy module
  returns only file metadata.

Run-of-show::

    uv run python examples/coffee_order_ansible/fsm.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from burr.core import Application, ApplicationBuilder, State, action
from burr.core.action import Condition
from burr.tracking import LocalTrackingClient

from ansiburr import initial_sentinels, module_action

_BASE_PRICE = 5.0
_MODIFIER_PRICE = {"extra_shot": 1.0, "oat_milk": 1.0, "syrup": 1.0}

_QUEUE_DIR = Path("/tmp/coffee")
_ACTIVE_PATH = _QUEUE_DIR / "active.json"
_DONE_DIR = _QUEUE_DIR / "done"


def _file_content(item: str, qty: int, modifiers: list[str], total: float, stage: str) -> str:
    return json.dumps(
        {
            "item": item,
            "qty": qty,
            "modifiers": modifiers,
            "total": total,
            "stage": stage,
        },
        indent=2,
    )


@module_action(
    "ansible.builtin.copy",
    writes=["stage", "item", "qty", "modifiers", "total"],
)
def take_order(
    state: State,
    item: str,
    qty: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Place a new coffee order by writing it to the queue."""
    if qty < 1:
        raise ValueError(f"qty must be >= 1; got {qty}")
    modifiers: list[str] = []
    total = _BASE_PRICE * qty
    _QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    _DONE_DIR.mkdir(parents=True, exist_ok=True)
    module_args = {
        "content": _file_content(item, qty, modifiers, total, "ordered"),
        "dest": str(_ACTIVE_PATH),
        "mode": "0644",
    }
    state_overrides = {
        "stage": "ordered",
        "item": item,
        "qty": qty,
        "modifiers": modifiers,
        "total": total,
    }
    return module_args, state_overrides


@module_action(
    "ansible.builtin.copy",
    reads=["item", "qty", "modifiers", "total"],
    writes=["modifiers", "total"],
)
def add_modifier(
    state: State,
    modifier: Literal["extra_shot", "oat_milk", "syrup"],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append a modifier to the active order, rewriting the queue file."""
    new_modifiers = [*state["modifiers"], modifier]
    new_total = state["total"] + _MODIFIER_PRICE[modifier]
    module_args = {
        "content": _file_content(state["item"], state["qty"], new_modifiers, new_total, "ordered"),
        "dest": str(_ACTIVE_PATH),
        "mode": "0644",
    }
    return module_args, {"modifiers": new_modifiers, "total": new_total}


@module_action(
    "ansible.builtin.copy",
    reads=["item", "qty", "modifiers", "total"],
    writes=["stage", "paid_amount"],
)
def pay(
    state: State,
    amount: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist payment to the queue file."""
    module_args = {
        "content": _file_content(
            state["item"], state["qty"], state["modifiers"], state["total"], "paid"
        ),
        "dest": str(_ACTIVE_PATH),
        "mode": "0644",
    }
    return module_args, {"stage": "paid", "paid_amount": amount}


@module_action(
    "ansible.builtin.command",
    reads=["item"],
    writes=["stage"],
)
def fulfill(state: State) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move the order to the done archive with a timestamped filename."""
    ts = int(time.time())
    item_slug = state["item"].replace(" ", "_")
    target = _DONE_DIR / f"{ts}-{item_slug}.json"
    module_args = {"cmd": f"mv {_ACTIVE_PATH} {target}"}
    return module_args, {"stage": "fulfilled"}


@module_action(
    "ansible.builtin.file",
    writes=["stage"],
)
def cancel(state: State) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove the active order from the queue."""
    module_args = {"path": str(_ACTIVE_PATH), "state": "absent"}
    return module_args, {"stage": "cancelled"}


@action(
    reads=["stage", "item", "qty", "modifiers", "total", "paid_amount"],
    writes=["outcome"],
)
def report(state: State) -> State:
    return state.update(
        outcome=(
            f"stage={state['stage']} item={state['item']} qty={state['qty']} "
            f"modifiers={state['modifiers']} total={state['total']} "
            f"paid_amount={state['paid_amount']}"
        )
    )


def build_application() -> Application:
    """Same transition graph as burr-mcp/examples/coffee_order.py — verbatim."""
    ordered = Condition.expr("stage == 'ordered'")
    paid = Condition.expr("stage == 'paid'")
    return (
        ApplicationBuilder()
        .with_actions(
            take_order=take_order,
            add_modifier=add_modifier,
            pay=pay,
            fulfill=fulfill,
            cancel=cancel,
            report=report,
        )
        .with_transitions(
            ("take_order", "pay", ordered),
            ("take_order", "add_modifier", ordered),
            ("take_order", "cancel", ordered),
            ("add_modifier", "pay", ordered),
            ("add_modifier", "add_modifier", ordered),
            ("add_modifier", "cancel", ordered),
            ("pay", "fulfill", paid),
            ("fulfill", "report"),
            ("cancel", "report"),
        )
        .with_tracker(LocalTrackingClient(project="ansiburr-coffee-order-ansible"))
        .with_state(
            **initial_sentinels(),
            stage="new",
            item="",
            qty=0,
            modifiers=[],
            total=0.0,
            paid_amount=0.0,
        )
        .with_entrypoint("take_order")
        .build()
    )


def _step_as(app: Application, action_name: str, inputs: dict[str, Any]) -> Any:
    """Force ``app.step`` to advance to ``action_name`` regardless of which
    transition Burr's auto-router would pick.

    Burr's ``step()`` consults transitions in declaration order and picks the
    first one whose predicate holds. For the coffee_order FSM (which is meant
    to be agent-driven), that means programmatic walks always take the first
    transition and never visit ``add_modifier`` or ``cancel``. Pattern borrowed
    from burr-mcp's adapter: monkey-patch ``get_next_action`` to return the
    action we asked for, then call step.
    """
    actions_by_name = {a.name: a for a in app.graph.actions}
    target = actions_by_name[action_name]
    original = app.get_next_action
    app.get_next_action = lambda: target  # type: ignore[method-assign]
    try:
        return app.step(inputs=inputs)
    finally:
        app.get_next_action = original  # type: ignore[method-assign]


def _run_walk(app: Application, walk: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Drive the FSM with an explicit walk; each step forces a specific action."""
    print(f"\nwalking: {' -> '.join(name for name, _ in walk)}")
    state = None
    for action_name, inputs in walk:
        action_obj, _result, state = _step_as(app, action_name, inputs)
        print(f"  {action_obj.name:<14} stage={state['stage']:<10} total={state['total']}")
    return dict(state.get_all()) if state is not None else {}


def main() -> None:
    # Three walks against the same graph: happy-path, with-modifiers, cancel.
    for label, walk in [
        (
            "happy-path",
            [
                ("take_order", {"item": "latte", "qty": 1}),
                ("pay", {"amount": 5.00}),
                ("fulfill", {}),
                ("report", {}),
            ],
        ),
        (
            "with-modifiers",
            [
                ("take_order", {"item": "americano", "qty": 2}),
                ("add_modifier", {"modifier": "extra_shot"}),
                ("add_modifier", {"modifier": "oat_milk"}),
                ("pay", {"amount": 12.00}),
                ("fulfill", {}),
                ("report", {}),
            ],
        ),
        (
            "cancelled",
            [
                ("take_order", {"item": "cortado", "qty": 1}),
                ("add_modifier", {"modifier": "syrup"}),
                ("cancel", {}),
                ("report", {}),
            ],
        ),
    ]:
        print(f"\n=== walk: {label} ===")
        app = build_application()
        final = _run_walk(app, walk)
        print(f"  outcome: {final['outcome']}")


if __name__ == "__main__":
    main()
