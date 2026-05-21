"""The ``ansiburr`` command-line entry point.

Subcommands::

    ansiburr run    <path> [--halt-after ACTION ...]
    ansiburr graph  <path> [--format {mermaid,dot,text}]
    ansiburr lint   <path>

``<path>`` is either an Ansible YAML playbook (converted via
:func:`ansiburr.from_playbook`) or a Python module that builds an
``Application`` and exposes it as a module-level ``app`` attribute or via
a ``build_application()`` callable.

``lint`` is the dry-conversion reporter: it tries to lift the playbook
into an Application and prints a structural summary (actions,
transitions, what got lowered) without running anything. On failure it
names the blocking construct so you can decide whether to rewrite or
file an issue.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from burr.core import Application

from ansiburr import __version__, from_playbook

_PLAYBOOK_SUFFIXES = (".yml", ".yaml")
_PYTHON_SUFFIXES = (".py",)
_DEFAULT_HALT_AFTER: tuple[str, ...] = ("done", "escalate")


# ---------------------------------------------------------------------------
# Application loading
# ---------------------------------------------------------------------------


def _load_python_application(path: Path) -> Application:
    """Import a Python file and return the Application it defines.

    Looks for a module-level ``app`` attribute first, then a
    ``build_application()`` callable. Raises ``SystemExit`` with a useful
    message if neither is present.
    """
    spec = importlib.util.spec_from_file_location(f"ansiburr_cli_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ansiburr: cannot load Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Make the example's own directory importable, so sibling modules work
    # (e.g. examples/hero.py imports examples/localhost_disk_check.py).
    sys.path.insert(0, str(path.resolve().parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    candidate = getattr(module, "app", None)
    if isinstance(candidate, Application):
        return candidate

    builder: Callable[..., Application] | None = getattr(module, "build_application", None)
    if builder is not None and callable(builder):
        result = builder()
        if isinstance(result, Application):
            return result
        raise SystemExit(
            f"ansiburr: {path} build_application() returned "
            f"{type(result).__name__}, expected Application"
        )

    raise SystemExit(
        f"ansiburr: {path} has neither an ``app`` Application "
        "nor a ``build_application()`` callable"
    )


def _load_application(path: Path) -> Application:
    if not path.exists():
        raise SystemExit(f"ansiburr: no such file: {path}")
    suffix = path.suffix.lower()
    if suffix in _PLAYBOOK_SUFFIXES:
        return from_playbook(path)
    if suffix in _PYTHON_SUFFIXES:
        return _load_python_application(path)
    raise SystemExit(
        f"ansiburr: don't know how to load {path}; expected one of "
        f"{_PLAYBOOK_SUFFIXES + _PYTHON_SUFFIXES}"
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    app = _load_application(Path(args.path))
    available = {a.name for a in app.graph.actions}
    if args.halt_after:
        # Caller specified halt actions explicitly; surface a clear error
        # rather than passing unknown names to Burr, which would raise.
        requested = list(args.halt_after)
        missing = [n for n in requested if n not in available]
        if missing:
            raise SystemExit(
                f"ansiburr: halt-after action(s) {missing} not in graph; "
                f"known actions: {sorted(available)}"
            )
        halt_after = requested
    else:
        # Default to the conventional terminal names, filtered to those that
        # actually exist in the graph. Hand-authored FSMs commonly have at
        # least one of them; if neither, the run halts only at a natural dead
        # end.
        halt_after = [n for n in _DEFAULT_HALT_AFTER if n in available]
    last_action, _result, final_state = app.run(halt_after=halt_after)
    print(f"final action: {last_action.name}")
    outcome = final_state.get_all().get("outcome")
    if outcome:
        print(f"outcome:      {outcome}")
    return 0


def _format_condition(condition: Any) -> str:
    """Best-effort render of a Burr ``Condition``. Falls back to ``str()``
    so future Condition subclasses still produce something readable."""
    if condition is None:
        return ""
    # Burr's expr-built conditions expose ``.expr`` in their string form;
    # the default repr is something like "Condition(expr='x == 1')".
    text = str(condition)
    return text.strip()


def _graph_text(app: Application) -> str:
    lines = ["actions:"]
    lines.extend(f"  - {action_obj.name}" for action_obj in app.graph.actions)
    lines.append("transitions:")
    for t in app.graph.transitions:
        cond = _format_condition(t.condition)
        arrow = f"  {t.from_.name} -> {t.to.name}"
        if cond:
            arrow += f"  [{cond}]"
        lines.append(arrow)
    return "\n".join(lines)


def _safe_id(name: str) -> str:
    """Identifier safe for Mermaid/DOT node IDs (alphanumeric + underscore)."""
    return "".join(c if c.isalnum() else "_" for c in name)


def _graph_mermaid(app: Application) -> str:
    lines = ["graph TD"]
    name_to_id: dict[str, str] = {}
    for action_obj in app.graph.actions:
        node_id = _safe_id(action_obj.name)
        name_to_id[action_obj.name] = node_id
        lines.append(f"    {node_id}[{action_obj.name}]")
    for t in app.graph.transitions:
        src = name_to_id[t.from_.name]
        dst = name_to_id[t.to.name]
        cond = _format_condition(t.condition)
        if cond:
            lines.append(f"    {src} -- {cond} --> {dst}")
        else:
            lines.append(f"    {src} --> {dst}")
    return "\n".join(lines)


def _graph_dot(app: Application) -> str:
    lines = ["digraph ansiburr {", '    rankdir="LR";']
    name_to_id: dict[str, str] = {}
    for action_obj in app.graph.actions:
        node_id = _safe_id(action_obj.name)
        name_to_id[action_obj.name] = node_id
        lines.append(f'    {node_id} [label="{action_obj.name}"];')
    for t in app.graph.transitions:
        src = name_to_id[t.from_.name]
        dst = name_to_id[t.to.name]
        cond = _format_condition(t.condition).replace('"', '\\"')
        if cond:
            lines.append(f'    {src} -> {dst} [label="{cond}"];')
        else:
            lines.append(f"    {src} -> {dst};")
    lines.append("}")
    return "\n".join(lines)


_GRAPH_FORMATTERS: dict[str, Callable[[Application], str]] = {
    "text": _graph_text,
    "mermaid": _graph_mermaid,
    "dot": _graph_dot,
}


def _cmd_graph(args: argparse.Namespace) -> int:
    app = _load_application(Path(args.path))
    formatter = _GRAPH_FORMATTERS[args.format]
    print(formatter(app))
    return 0


def _cmd_emit(args: argparse.Namespace) -> int:
    """Round-trip a playbook through ``from_playbook`` then ``to_playbook``
    and write the canonical YAML to stdout. Useful for normalizing a
    playbook's formatting or for diffing against a hand-edited version.
    """
    from ansiburr import from_playbook, to_playbook

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"ansiburr: no such file: {path}")
    if path.suffix.lower() not in _PLAYBOOK_SUFFIXES:
        raise SystemExit(
            f"ansiburr: emit expects a YAML playbook (one of "
            f"{_PLAYBOOK_SUFFIXES}); got {path}"
        )
    app = from_playbook(path)
    print(to_playbook(app), end="")
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    """Dry-convert a playbook and report a structural summary.

    Returns 0 on a clean conversion, 1 on UnsupportedPlaybookConstruct,
    2 on any other parse / lowering error.
    """
    from ansiburr import UnsupportedPlaybookConstruct, from_playbook

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"ansiburr: no such file: {path}")
    suffix = path.suffix.lower()
    if suffix not in _PLAYBOOK_SUFFIXES:
        raise SystemExit(
            f"ansiburr: lint expects a YAML playbook (one of "
            f"{_PLAYBOOK_SUFFIXES}); got {path}"
        )

    try:
        app = from_playbook(path)
    except UnsupportedPlaybookConstruct as e:
        print(f"ansiburr lint  {path.name}: REJECTED")
        print(f"  reason: {e}")
        print()
        print("  the converter refused this playbook because of an unsupported")
        print("  construct. options:")
        print("    1. rewrite the construct in a form ansiburr accepts")
        print("       (see REFERENCE.md for the supported list)")
        print("    2. open an issue at github.com/msradam/ansiburr/issues")
        return 1
    except Exception as e:
        print(f"ansiburr lint  {path.name}: ERROR")
        print(f"  {type(e).__name__}: {e}")
        return 2

    actions = list(app.graph.actions)
    transitions = list(app.graph.transitions)

    # Bucket the action names so the report shows what got lowered into
    # what. Each prefix corresponds to a synthesized action class added
    # by ansiburr.
    buckets: dict[str, list[str]] = {
        "module + python tasks": [],
        "loop init/advance (auto)": [],
        "notify markers (auto)": [],
        "changed_when posts (auto)": [],
        "block save/restore/clear (auto)": [],
        "terminals (auto)": [],
        "handlers": [],
    }
    for a in actions:
        n = a.name
        if n in ("done", "escalate"):
            buckets["terminals (auto)"].append(n)
        elif n.startswith("handler_"):
            buckets["handlers"].append(n)
        elif n.startswith("_notify_"):
            buckets["notify markers (auto)"].append(n)
        elif n.startswith("_changed_when_"):
            buckets["changed_when posts (auto)"].append(n)
        elif n.startswith("_block_"):
            buckets["block save/restore/clear (auto)"].append(n)
        elif "_loop_init" in n or "_loop_advance" in n:
            buckets["loop init/advance (auto)"].append(n)
        else:
            buckets["module + python tasks"].append(n)

    print(f"ansiburr lint  {path.name}: OK")
    print(f"  actions:     {len(actions)}")
    print(f"  transitions: {len(transitions)}")
    print()
    print("  breakdown:")
    for label, names in buckets.items():
        if names:
            print(f"    {label:32}  {len(names):3d}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ansiburr",
        description="Run and inspect ansiburr/Burr applications from playbooks or Python files.",
    )
    parser.add_argument("--version", action="version", version=f"ansiburr {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_p = subparsers.add_parser(
        "run",
        help="Execute an FSM from a playbook (.yml) or a Python module exporting an Application.",
    )
    run_p.add_argument("path", help="Path to a .yml/.yaml playbook or a .py module.")
    run_p.add_argument(
        "--halt-after",
        action="append",
        default=None,
        metavar="ACTION",
        help=(
            "Terminal action name to halt on. May be passed multiple times. "
            f"Default: {' '.join(_DEFAULT_HALT_AFTER)}."
        ),
    )
    run_p.set_defaults(func=_cmd_run)

    graph_p = subparsers.add_parser(
        "graph",
        help="Print the FSM structure (actions and transitions) for an application.",
    )
    graph_p.add_argument("path", help="Path to a .yml/.yaml playbook or a .py module.")
    graph_p.add_argument(
        "--format",
        choices=sorted(_GRAPH_FORMATTERS.keys()),
        default="mermaid",
        help="Output format. Default: mermaid.",
    )
    graph_p.set_defaults(func=_cmd_graph)

    lint_p = subparsers.add_parser(
        "lint",
        help="Dry-convert a playbook and report a structural summary.",
    )
    lint_p.add_argument("path", help="Path to a .yml / .yaml playbook.")
    lint_p.set_defaults(func=_cmd_lint)

    emit_p = subparsers.add_parser(
        "emit",
        help="Round-trip a playbook YAML -> Application -> YAML and print the result.",
    )
    emit_p.add_argument("path", help="Path to a .yml / .yaml playbook.")
    emit_p.set_defaults(func=_cmd_emit)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
