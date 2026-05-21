"""Convert Ansible YAML playbooks into ansiburr/Burr ``Application`` objects.

Supports the subset of Ansible's playbook syntax that maps cleanly to a
single-host, flat-task FSM:

  - One play (multi-play raises :class:`UnsupportedPlaybookConstruct`).
  - Tasks with a name, exactly one module reference, and a module-args dict.
  - ``when:`` predicates (string expressions; lists are AND-joined).
  - ``register:`` capturing the full module result (mapped to ansiburr's
    ``register=`` projection).
  - ``become:`` per-task or per-play.
  - ``failed_when:`` and ``ignore_errors:`` as guard transitions; failed
    tasks route to an auto-generated ``escalate`` terminal unless the play
    sets ``ignore_errors: yes`` for the task.
  - ``gather_facts: yes`` lowers to a leading :meth:`Host.gather_facts` action.
  - Play-level ``vars:`` populate ``with_state(...)``.

The following constructs raise :class:`UnsupportedPlaybookConstruct` with
the offending node:

  - ``block`` / ``rescue`` / ``always``
  - ``loop`` / ``with_items`` / ``with_*``
  - ``notify`` + handlers
  - ``import_playbook`` / ``import_tasks`` / ``include_*``
  - ``roles:`` blocks
  - ``serial:`` / ``max_fail_percentage:`` strategy keywords

The output is a runnable :class:`burr.core.Application` whose actions are
ansiburr ``@module_action`` decorations of each task. Pure-Python actions
in the FSM (the ``escalate`` terminal, the optional ``gather_facts``
step) are added automatically.

Example::

    from ansiburr import from_playbook

    app = from_playbook("playbook.yml")
    last, _, final = app.run(halt_after=["task_3", "escalate"])
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import jinja2
import yaml
from burr.core import Application, ApplicationBuilder, State, action, expr

from ansiburr._action import initial_sentinels, module_action

# Permissive Jinja: undefined references become empty strings rather than
# raising. Matches Ansible's default behavior closer than StrictUndefined
# would, and keeps the FSM advancing even when an upstream task hasn't
# populated the referenced register yet (e.g. when ``when:`` skipped it).
_JINJA_ENV = jinja2.Environment(
    undefined=jinja2.ChainableUndefined,
    autoescape=False,
)


def _render_jinja(value: Any, context: Mapping[str, Any]) -> Any:
    """Walk a Python value, rendering Jinja2 templates inside any strings
    using ``context``. Non-string leaves are returned as-is. Dicts and lists
    recurse."""
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        return _JINJA_ENV.from_string(value).render(**context)
    if isinstance(value, Mapping):
        return {k: _render_jinja(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_jinja(v, context) for v in value]
    return value


_RESERVED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "when",
        "register",
        "ignore_errors",
        "failed_when",
        "changed_when",
        "become",
        "become_user",
        "become_method",
        "tags",
        "vars",
        "delegate_to",
        "check_mode",
        "diff",
        "no_log",
        "args",
    }
)

_UNSUPPORTED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "block",
        "rescue",
        "always",
        "loop",
        "loop_control",
        "with_items",
        "with_dict",
        "with_fileglob",
        "with_subelements",
        "notify",
        "import_tasks",
        "import_role",
        "include",
        "include_tasks",
        "include_role",
    }
)

_UNSUPPORTED_PLAY_KEYS: frozenset[str] = frozenset(
    {
        "roles",
        "handlers",
        "pre_tasks",
        "post_tasks",
        "serial",
        "strategy",
        "max_fail_percentage",
        "any_errors_fatal",
    }
)


class UnsupportedPlaybookConstruct(NotImplementedError):
    """Raised when a playbook uses constructs ansiburr's converter doesn't support."""


_NAME_NON_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def _slugify(name: str) -> str:
    """Turn an arbitrary Ansible task name into a Python identifier."""
    slug = _NAME_NON_IDENT_RE.sub("_", name.strip()).strip("_").lower()
    if not slug:
        slug = "task"
    if slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def _module_from_task(task: Mapping[str, Any]) -> tuple[str, Any]:
    """Find the module name + args for a task. Tasks have exactly one
    non-reserved, non-unsupported key whose value is the module's args."""
    for key in task:
        if key in _UNSUPPORTED_TASK_KEYS:
            raise UnsupportedPlaybookConstruct(
                f"task uses unsupported construct {key!r}: {task.get('name', task)}"
            )
    module_keys = [k for k in task if k not in _RESERVED_TASK_KEYS]
    if not module_keys:
        raise ValueError(f"task has no module reference: {task}")
    if len(module_keys) > 1:
        raise ValueError(
            f"task has multiple module-shaped keys {module_keys}: {task.get('name', task)}"
        )
    module = module_keys[0]
    args = task[module]
    if args is None:
        args = {}
    elif isinstance(args, str):
        # Short form (e.g. ``shell: echo hello``) gets preserved via the
        # ``_raw_params`` key, which ansible.builtin.shell, .command and a few
        # others honor.
        args = {"_raw_params": args}
    elif not isinstance(args, Mapping):
        raise ValueError(f"task {module!r} args must be a mapping or string; got {type(args)}")
    return module, dict(args)


def _when_to_expr_string(when: str | Iterable[str]) -> str:
    """Lower Ansible ``when:`` (string or list of strings) to a single Python
    expression. A list is AND-joined with parentheses around each clause."""
    if isinstance(when, str):
        return when
    clauses = [str(c) for c in when]
    if not clauses:
        return "True"
    if len(clauses) == 1:
        return clauses[0]
    return " and ".join(f"({c})" for c in clauses)


def _translate_register_dot_access(expr_text: str, registers: Iterable[str]) -> str:
    """Translate ``register.attr.attr2`` to ``register["attr"]["attr2"]`` for
    every registered name.

    Ansible's ``when:`` and ``failed_when:`` expressions use Jinja-style
    attribute access on registered results (``result.rc == 0``). Burr's
    ``expr()`` uses Python ``eval``, and Python dicts don't support attribute
    access. This rewriter converts the simple case (chained ``.attr`` on a
    bare register name) to bracket access. More complex Jinja syntax
    (filters, ``is defined`` tests) is left as-is and will fail at expression
    evaluation time with a Python error pointing at the unsupported feature.
    """
    register_set = set(registers)
    if not register_set:
        return expr_text
    # Match: word-boundary register name + one or more ``.attr`` accesses.
    name_alt = "|".join(re.escape(r) for r in register_set)
    pattern = re.compile(rf"\b(?P<name>{name_alt})(?P<chain>(?:\.[A-Za-z_]\w*)+)\b")

    def _rewrite(match: re.Match[str]) -> str:
        name = match.group("name")
        attrs = match.group("chain").lstrip(".").split(".")
        return name + "".join(f"[{attr!r}]" for attr in attrs)

    return pattern.sub(_rewrite, expr_text)


def _build_task_action(
    *,
    py_name: str,
    module: str,
    args: Mapping[str, Any],
    register: str | None,
    become: bool,
    known_registers: Iterable[str],
    play_vars: Mapping[str, Any],
) -> Any:
    """Construct a ``@module_action`` that renders Jinja2 templates in the
    task's args using Burr state plus play-level vars as the context.

    Per-task plays in the converter don't share registered facts the way a
    single multi-task play would, so we render templates ourselves before
    ansible-runner sees them. ``known_registers`` lists the names of every
    ``register:`` target declared across the playbook; their values are
    pulled from State on each invocation and added to the rendering context
    alongside the play-level ``vars:`` block.

    The inner function's ``__name__`` is set BEFORE applying ``module_action``
    so that ``functools.wraps`` inside the decorator carries the task's real
    name through to ``_last_action`` and to the tracker.
    """
    register_names = list(known_registers)
    pinned_vars = dict(play_vars)

    def _impl(state: State) -> dict[str, Any]:
        context: dict[str, Any] = {**pinned_vars}
        state_dict = state.get_all()
        for name in register_names:
            if name in state_dict:
                context[name] = state_dict[name]
        return _render_jinja(dict(args), context)

    _impl.__name__ = py_name
    return module_action(module, register=register, become=become)(_impl)


def from_playbook(path: str | Path, *, project: str | None = None) -> Application:
    """Parse a YAML playbook and return a runnable :class:`Application`.

    ``path`` is the playbook file. ``project`` is an optional tracker
    project name; when given, a ``LocalTrackingClient`` is attached.

    Raises :class:`UnsupportedPlaybookConstruct` for playbook constructs
    that don't map cleanly to the ansiburr/Burr model. See module
    docstring for the full list.
    """
    playbook_path = Path(path)
    with playbook_path.open() as f:
        plays = yaml.safe_load(f)

    if not isinstance(plays, list):
        raise ValueError(f"{playbook_path}: top level must be a list of plays")
    if len(plays) == 0:
        raise ValueError(f"{playbook_path}: no plays found")
    if len(plays) > 1:
        raise UnsupportedPlaybookConstruct(
            f"{playbook_path}: multi-play playbooks are not supported"
        )

    play = plays[0]
    if not isinstance(play, Mapping):
        raise ValueError(f"{playbook_path}: play must be a mapping; got {type(play)}")

    unsupported_play = [k for k in play if k in _UNSUPPORTED_PLAY_KEYS]
    if unsupported_play:
        raise UnsupportedPlaybookConstruct(
            f"{playbook_path}: play uses unsupported keys {unsupported_play}"
        )

    tasks = play.get("tasks") or []
    if not tasks:
        raise ValueError(f"{playbook_path}: play has no tasks")

    play_vars: dict[str, Any] = dict(play.get("vars") or {})
    play_become = bool(play.get("become", False))
    gather_facts = play.get("gather_facts")
    if gather_facts is None:
        gather_facts = True  # Ansible default

    # ------------------------------------------------------------------
    # Walk the tasks: build one action per task, with a unique name. Detect
    # name collisions and append _2, _3, ... rather than silently merging.
    # Done in two passes: first collect names, registers, when clauses; then
    # build actions with the full set of register names + play vars in scope
    # so Jinja templates inside task args can resolve at execution time.
    # ------------------------------------------------------------------
    py_names: list[str] = []  # parallel to tasks
    when_clauses: list[str | None] = []
    failed_when_clauses: list[str | None] = []
    ignore_errors_flags: list[bool] = []
    register_targets: list[str | None] = []
    # Parallel list of (module, args, register, become) for the second pass.
    task_meta: list[tuple[str, dict[str, Any], str | None, bool]] = []

    used: set[str] = set()
    for idx, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            raise ValueError(f"task #{idx} is not a mapping: {task!r}")
        raw_name = task.get("name") or f"task_{idx + 1}"
        py = _slugify(raw_name)
        # de-collide
        candidate = py
        n = 1
        while candidate in used:
            n += 1
            candidate = f"{py}_{n}"
        used.add(candidate)
        py_names.append(candidate)

        module, args = _module_from_task(task)
        register = task.get("register")
        register_targets.append(register)
        become = bool(task.get("become", play_become))
        task_meta.append((module, args, register, become))

        when = task.get("when")
        when_clauses.append(_when_to_expr_string(when) if when is not None else None)
        fw = task.get("failed_when")
        failed_when_clauses.append(_when_to_expr_string(fw) if fw is not None else None)
        ignore_errors_flags.append(bool(task.get("ignore_errors", False)))

    known_registers: set[str] = {r for r in register_targets if r}

    actions: dict[str, Any] = {}
    for candidate, (module, args, register, become) in zip(py_names, task_meta, strict=True):
        actions[candidate] = _build_task_action(
            py_name=candidate,
            module=module,
            args=args,
            register=register,
            become=become,
            known_registers=known_registers,
            play_vars=play_vars,
        )

    # Translate Jinja-style attribute access on registered names into the
    # bracket access Python's eval expects. ``known_registers`` was assembled
    # in the first pass over the tasks.
    when_clauses = [
        _translate_register_dot_access(c, known_registers) if c is not None else None
        for c in when_clauses
    ]
    failed_when_clauses = [
        _translate_register_dot_access(c, known_registers) if c is not None else None
        for c in failed_when_clauses
    ]

    # ------------------------------------------------------------------
    # Terminals: ``done`` (reached after the last task or when when-skipped)
    # and ``escalate`` (any task fails and isn't ignore_errors). Pure Python.
    # ------------------------------------------------------------------
    @action(reads=[], writes=["outcome"])
    def done(state: State) -> State:
        return state.update(outcome="OK: playbook completed")

    @action(reads=["_last_action", "_last_msg"], writes=["outcome"])
    def escalate(state: State) -> State:
        return state.update(
            outcome=f"ESCALATE at {state['_last_action']}: {state['_last_msg'][:300]}"
        )

    actions["done"] = done
    actions["escalate"] = escalate

    # Optional gather_facts as the entrypoint.
    if gather_facts:
        # ``ansible.builtin.setup`` invoked controller-side without an explicit
        # host is fine for localhost playbooks (the common case for converted
        # tutorials). Users targeting a remote host should wire a ``host()``
        # profile and use ``host.gather_facts()`` directly.
        @module_action("ansible.builtin.setup", register="gathered_facts")
        def _setup(state: State) -> dict[str, Any]:
            return {}

        _setup.__name__ = "gather_facts"
        actions["gather_facts"] = _setup
        entry = "gather_facts"
    else:
        entry = py_names[0]

    # ------------------------------------------------------------------
    # Transitions:
    #   - ``when:`` on task[i]: if predicate fails, jump straight to task[i+1]
    #     (or to ``done`` if i is the last task).
    #   - ``failed_when:`` or _last_failed by default: if predicate holds, jump
    #     to escalate (unless ``ignore_errors: yes``).
    #   - Otherwise: linear flow to task[i+1].
    # ------------------------------------------------------------------
    transitions: list[tuple] = []
    if gather_facts:
        transitions.append(("gather_facts", "escalate", expr("_last_failed")))
        transitions.append(("gather_facts", py_names[0]))

    for i, current in enumerate(py_names):
        nxt = py_names[i + 1] if i + 1 < len(py_names) else "done"

        # Failure routing for the current task: if the failed_when expression
        # is satisfied (or _last_failed if none was given) and ignore_errors
        # was not set, the FSM routes to the escalate terminal.
        if not ignore_errors_flags[i]:
            failure_predicate = failed_when_clauses[i] or "_last_failed"
            transitions.append((current, "escalate", expr(failure_predicate)))

        # ``when:`` skip behavior: if the next task carries a ``when:`` whose
        # predicate evaluates to False at this point in the run, advance past
        # it directly to the task after. v1 implements single-level skip;
        # consecutive skipped tasks aren't chained.
        if i + 1 < len(py_names) and when_clauses[i + 1] is not None:
            next_when = when_clauses[i + 1]
            skip_target = py_names[i + 2] if i + 2 < len(py_names) else "done"
            transitions.append((current, skip_target, expr(f"not ({next_when})")))
        transitions.append((current, nxt))

    # ------------------------------------------------------------------
    # Build the Application.
    # ------------------------------------------------------------------
    builder: ApplicationBuilder = ApplicationBuilder().with_actions(**actions)
    for t in transitions:
        builder = builder.with_transitions(t)

    state_init: dict[str, Any] = {
        **initial_sentinels(),
        **play_vars,
        "outcome": "",
    }
    # Pre-seed register targets so transitions can read them before the producing
    # task runs.
    for reg in register_targets:
        if reg and reg not in state_init:
            state_init[reg] = {}
    if gather_facts:
        state_init.setdefault("gathered_facts", {})
    builder = builder.with_state(**state_init).with_entrypoint(entry)

    if project is not None:
        from burr.tracking import LocalTrackingClient

        builder = builder.with_tracker(LocalTrackingClient(project=project))  # type: ignore[arg-type]

    return builder.build()
