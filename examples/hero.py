"""Walk an ansiburr ``Application`` step by step and print each transition,
state delta, and final outcome with colored terminal output.

Drives ``examples/localhost_disk_check.py``: a six-action FSM that runs
``ansible.builtin.ping``, captures uptime via ``shell``, reads disk usage,
parses the result in pure Python, and branches to ``ok`` or ``warn``.
The whole thing runs on the local machine (no container, no ssh).

Run::

    uv run python examples/hero.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# The example package isn't installable; import directly by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from localhost_disk_check import build_application

# ---- ANSI ---------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

# Internal sentinel and bookkeeping keys we don't want to surface in the
# per-step state delta. Their job is to drive transitions, not to be read
# by humans watching the demo.
_HIDDEN_STATE_KEYS = frozenset(
    {
        "_last_action",
        "_last_failed",
        "_last_changed",
        "_last_unreachable",
        "_last_msg",
        "_last_diff",
        "__SEQUENCE_ID",
        "__PRIOR_STEP",
    }
)

# Terminal action names this demo halts on. The same FSM has both ``ok``
# (healthy disk) and ``warn`` (over threshold); either is the end.
_TERMINALS = frozenset({"ok", "warn"})


def _truncate(value: object, width: int = 56) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _state_delta(prev: dict[str, object], curr: dict[str, object]) -> list[tuple[str, object]]:
    """Return (key, value) pairs that are new or changed since ``prev``,
    skipping ansiburr's internal sentinel and tracking keys."""
    out: list[tuple[str, object]] = []
    for key, value in curr.items():
        if key in _HIDDEN_STATE_KEYS:
            continue
        if key not in prev or prev[key] != value:
            out.append((key, value))
    return out


def _print_header() -> None:
    print()
    print(f"  {BOLD}{CYAN}ansiburr{RESET}{DIM} · {RESET}{BOLD}localhost disk check{RESET}")
    print(f"  {DIM}Six-action FSM. Ansible modules + pure-Python branching, run locally.{RESET}")
    print()


def _print_step(
    step: int,
    name: str,
    duration_ms: float,
    delta: list[tuple[str, object]],
    failed: bool,
) -> None:
    mark = f"{RED}✗{RESET}" if failed else f"{GREEN}✓{RESET}"
    name_color = RED if failed else CYAN
    print(
        f"  {DIM}[{step:2d}]{RESET} {BOLD}{name_color}{name:<24}{RESET}"
        f"  {DIM}{duration_ms:>5.0f}ms{RESET}  {mark}"
    )
    for key, value in delta:
        print(f"       {DIM}{key}={RESET}{_truncate(value)}")
    print()


def _print_terminal(name: str, outcome: object) -> None:
    color = GREEN if name == "ok" else YELLOW
    label = "OK" if name == "ok" else "WARN"
    print(f"  {color}{BOLD}→ {label}{RESET}  {_truncate(outcome)}")
    print()


def main() -> None:
    _print_header()

    app = build_application()
    prev_state = dict(app.state.get_all())
    step_no = 0

    while True:
        step_no += 1
        t0 = time.perf_counter()
        outcome = app.step()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if outcome is None:
            print(f"  {DIM}no further reachable actions; halting.{RESET}")
            break

        action_obj, _result, state = outcome
        state_now = dict(state.get_all())
        delta = _state_delta(prev_state, state_now)
        prev_state = state_now

        failed = bool(state_now.get("_last_failed", False))
        _print_step(step_no, action_obj.name, elapsed_ms, delta, failed)

        if action_obj.name in _TERMINALS:
            _print_terminal(action_obj.name, state_now.get("status", ""))
            break


if __name__ == "__main__":
    main()
