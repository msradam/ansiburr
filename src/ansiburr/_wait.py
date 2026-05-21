"""``wait_until()`` — polling sub-graph builder.

Ansible's ``wait_for`` (and ``wait_for_connection``, ``pause``, friends) are
structurally different from one-shot modules like ``copy`` or ``service``:
they block until a condition holds, with a timeout. Wrapping them as a single
``@module_action`` collapses an unbounded polling loop into one opaque Burr
step, which throws away the granular trace observability that's most of
Burr's value.

``wait_until()`` materializes the polling loop as native FSM structure: two
internal actions (a ``check`` and an ``increment_wait``) with transitions
that branch on the condition each iteration. Every poll attempt is a discrete
step in the Burr tracker; timeouts route to a caller-supplied terminal; the
LLM (or whatever else) never decides when to stop — the FSM does, per the
MAST FM-1.5 recommendation.

Usage::

    @target.shell(register="port_check")
    def check_port(state):
        return {"cmd": "nc -z 127.0.0.1 80; echo $?"}

    poll = ansiburr.wait_until(
        name="wait_for_listener",
        check=check_port,
        condition_expr="port_check['rc'] == 0",
        max_attempts=10,
        interval_s=1.0,
        on_success="verify",
        on_timeout="escalate",
    )

    app = (
        ApplicationBuilder()
        .with_actions(reload_nginx=reload_nginx, verify=verify, escalate=escalate,
                      **poll.actions)
        .with_transitions(
            ("reload_nginx", poll.entry),
            *poll.transitions,
            ...
        )
        .with_state(..., **poll.initial_state)
        ...
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from burr.core import State, action, expr


@dataclass
class WaitGraph:
    """Built by :func:`wait_until`. The fields are the components a caller
    merges into their ApplicationBuilder.

    - ``actions``: action_name -> action object. Merge via ``with_actions(**poll.actions)``.
    - ``transitions``: list of tuples for ``with_transitions(*poll.transitions)``.
    - ``entry``: the name of the first action in the sub-graph; the caller
      transitions INTO this from the action preceding the wait.
    - ``initial_state``: seeds needed in ``with_state(...)`` so the attempt
      counter starts at 0.
    """

    actions: dict[str, Any]
    transitions: list[tuple]
    entry: str
    initial_state: dict[str, Any]


def wait_until(
    *,
    name: str,
    check: Any,
    condition_expr: str,
    max_attempts: int = 10,
    interval_s: float = 1.0,
    on_success: str,
    on_timeout: str,
) -> WaitGraph:
    """Build a polling-until-condition sub-graph.

    Topology::

        <name>_check
          ├── (condition_expr)               → on_success
          ├── (<name>_attempts >= max-1)     → on_timeout
          └── <name>_wait (sleep + ++count)  → <name>_check

    The ``check`` argument is a normal ``@module_action`` (or any Burr
    ``@action``) the caller has already built — typically a shell/uri/wait_for
    call that writes a result the ``condition_expr`` references.
    ``condition_expr`` is a Python expression evaluated against state via
    Burr's standard ``expr`` builder.

    On timeout the FSM routes to ``on_timeout``; on success to ``on_success``.
    These are action names the caller registers separately. The wait sub-graph
    contains no escalation logic of its own — escalation is a graph-level
    concern.
    """
    check_name = f"{name}_check"
    wait_name = f"{name}_wait"
    attempts_key = f"{name}_attempts"

    @action(reads=[attempts_key], writes=[attempts_key])
    def _increment_wait(state: State) -> State:
        """Sleep ``interval_s`` and bump the attempt counter, then re-enter check."""
        time.sleep(interval_s)
        return state.update(**{attempts_key: state[attempts_key] + 1})

    actions: dict[str, Any] = {
        check_name: check,
        wait_name: _increment_wait,
    }

    # Transitions are tried in declaration order; first matching predicate wins.
    # That means: success path first (so we exit as soon as the condition holds),
    # then timeout (so we bail if attempts is at the cap), then the default
    # retry edge into wait. The check action itself is run on entry; downstream
    # of it we branch on the condition + attempt count.
    transitions: list[tuple] = [
        (check_name, on_success, expr(condition_expr)),
        (check_name, on_timeout, expr(f"{attempts_key} >= {max_attempts - 1}")),
        (check_name, wait_name),
        (wait_name, check_name),
    ]

    initial_state = {attempts_key: 0}

    return WaitGraph(
        actions=actions,
        transitions=transitions,
        entry=check_name,
        initial_state=initial_state,
    )
