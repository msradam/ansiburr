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
        "notify",
    }
)

# Keys handled by the flattening pass before the main converter runs over
# the (now-flat) task list. ``include_tasks``/``import_tasks``/``block`` are
# expanded; ``rescue``/``always`` still raise (deferred to a later release).
_FLATTEN_TASK_KEYS: frozenset[str] = frozenset({"include_tasks", "import_tasks", "block"})

_UNSUPPORTED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "rescue",
        "always",
        "loop",
        "loop_control",
        "with_items",
        "with_dict",
        "with_fileglob",
        "with_subelements",
        "import_role",
        "include",
        "include_role",
    }
)

_UNSUPPORTED_PLAY_KEYS: frozenset[str] = frozenset(
    {
        "roles",
        "pre_tasks",
        "post_tasks",
        "serial",
        "strategy",
        "max_fail_percentage",
        "any_errors_fatal",
    }
)

_MAX_INCLUDE_DEPTH: int = 5


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
    non-reserved, non-unsupported, non-flatten key whose value is the module's
    args. The flatten keys (``include_tasks``, ``import_tasks``, ``block``)
    are expanded before this function runs, so seeing one here is a converter
    bug."""
    for key in task:
        if key in _UNSUPPORTED_TASK_KEYS:
            raise UnsupportedPlaybookConstruct(
                f"task uses unsupported construct {key!r}: {task.get('name', task)}"
            )
    excluded = _RESERVED_TASK_KEYS | _FLATTEN_TASK_KEYS
    module_keys = [k for k in task if k not in excluded]
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


def _coerce_when_to_list(value: Any) -> list[str]:
    """Normalize ``when:`` (string, list, or None) to a list of clause strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(c) for c in value]
    return [str(value)]


def _coerce_notify_to_list(value: Any) -> list[str]:
    """Normalize ``notify:`` to a flat list of handler names."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(n) for n in value]
    return [str(value)]


def _flatten_tasks(
    tasks: list[Any],
    *,
    base_dir: Path,
    inherited_when: list[str] | None = None,
    inherited_notify: list[str] | None = None,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Recursively expand ``include_tasks``, ``import_tasks``, and ``block:``
    into a flat list of leaf tasks.

    ``inherited_when`` and ``inherited_notify`` accumulate down the recursion
    so block-wide ``when:`` / ``notify:`` propagate to every nested leaf
    task. The output preserves task ordering. Leaf tasks have their own
    ``when:`` AND-merged with the inherited list and their ``notify:``
    union-merged with the inherited list.

    Static and dynamic includes (``import_tasks`` and ``include_tasks``) are
    treated identically at conversion time: the referenced file is loaded
    once and inlined. Jinja-templated paths raise
    :class:`UnsupportedPlaybookConstruct` because resolving them would
    require a per-call runtime path resolution we do not implement.
    """
    if depth > _MAX_INCLUDE_DEPTH:
        raise UnsupportedPlaybookConstruct(
            f"include/block nesting depth exceeded {_MAX_INCLUDE_DEPTH}"
        )

    inherited_when = list(inherited_when or [])
    inherited_notify = list(inherited_notify or [])
    flat: list[dict[str, Any]] = []

    for raw_task in tasks:
        if not isinstance(raw_task, Mapping):
            raise ValueError(f"task is not a mapping: {raw_task!r}")
        task: dict[str, Any] = dict(raw_task)

        task_when = _coerce_when_to_list(task.get("when"))
        combined_when = inherited_when + task_when

        task_notify = _coerce_notify_to_list(task.get("notify"))
        combined_notify = inherited_notify + task_notify

        if "block" in task:
            if "rescue" in task or "always" in task:
                raise UnsupportedPlaybookConstruct(
                    f"rescue:/always: are not supported; task: {task.get('name', '<unnamed>')!r}"
                )
            block_tasks = task["block"]
            if not isinstance(block_tasks, list):
                raise ValueError(f"block: must be a list, got {type(block_tasks).__name__}")
            flat.extend(
                _flatten_tasks(
                    block_tasks,
                    base_dir=base_dir,
                    inherited_when=combined_when,
                    inherited_notify=combined_notify,
                    depth=depth + 1,
                )
            )
            continue

        include_key = next((k for k in ("include_tasks", "import_tasks") if k in task), None)
        if include_key is not None:
            include_value = task[include_key]
            if isinstance(include_value, str):
                include_rel_path = include_value
            elif isinstance(include_value, Mapping):
                file_value = include_value.get("file") or include_value.get("name")
                if not file_value:
                    raise ValueError(f"{include_key}: missing file/name")
                include_rel_path = str(file_value)
            else:
                raise ValueError(f"{include_key}: unsupported shape {type(include_value).__name__}")

            if "{{" in include_rel_path or "{%" in include_rel_path:
                raise UnsupportedPlaybookConstruct(
                    f"{include_key}: Jinja-templated paths are not supported "
                    f"(got {include_rel_path!r})"
                )

            included_path = (base_dir / include_rel_path).resolve()
            if not included_path.exists():
                raise FileNotFoundError(f"{include_key}: file not found: {included_path}")

            with included_path.open() as f:
                included_tasks = yaml.safe_load(f) or []
            if not isinstance(included_tasks, list):
                raise ValueError(f"{include_key}: {included_path} top-level must be a list")

            flat.extend(
                _flatten_tasks(
                    included_tasks,
                    base_dir=included_path.parent,
                    inherited_when=combined_when,
                    inherited_notify=combined_notify,
                    depth=depth + 1,
                )
            )
            continue

        # Leaf task. Inject the combined when/notify so the rest of the
        # converter sees them as if the author had written them inline.
        if combined_when:
            task["when"] = combined_when
        if combined_notify:
            task["notify"] = combined_notify
        flat.append(task)

    return flat


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


def _build_notify_marker(*, py_name: str, handlers: list[str]) -> Any:
    """Build a pure-Python ``@action`` that records notify triggers.

    Inserted after each task that has a ``notify:`` clause. If the preceding
    task reported ``_last_changed=True``, the marker writes
    ``_notified_<handler>=True`` for each handler name in the notify list.
    Handler actions, appended at the end of the play, gate their execution
    on these flags.
    """
    write_keys = [f"_notified_{h}" for h in handlers]

    def _marker(state: State) -> State:
        if state["_last_changed"]:
            return state.update(**{f"_notified_{h}": True for h in handlers})
        return state

    # Name the underlying function before the decorator wraps it, so the
    # resulting Burr Action reports this name in ``_last_action`` and in
    # the tracker rather than the literal ``_marker``.
    _marker.__name__ = py_name
    return action(reads=["_last_changed"], writes=write_keys)(_marker)


def from_playbook(path: str | Path, *, project: str | None = None) -> Application:
    """Parse a YAML playbook and return a runnable :class:`Application`.

    ``path`` is the playbook file. ``project`` is an optional tracker
    project name; when given, a ``LocalTrackingClient`` is attached.

    Raises :class:`UnsupportedPlaybookConstruct` for playbook constructs
    that don't map cleanly to the ansiburr/Burr model. See module
    docstring for the full list.
    """
    playbook_path = Path(path)
    base_dir = playbook_path.resolve().parent
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

    raw_tasks = play.get("tasks") or []
    if not raw_tasks:
        raise ValueError(f"{playbook_path}: play has no tasks")

    raw_handlers = play.get("handlers") or []

    play_vars: dict[str, Any] = dict(play.get("vars") or {})
    play_become = bool(play.get("become", False))
    gather_facts = play.get("gather_facts")
    if gather_facts is None:
        gather_facts = True  # Ansible default

    # ------------------------------------------------------------------
    # Flatten ``include_tasks``/``import_tasks`` and ``block:`` constructs
    # into a flat list of leaf tasks. Inherited ``when:`` and ``notify:``
    # propagate down to every nested leaf.
    # ------------------------------------------------------------------
    leaf_tasks = _flatten_tasks(raw_tasks, base_dir=base_dir)
    leaf_handlers = _flatten_tasks(raw_handlers, base_dir=base_dir)

    # Map each handler's real (display) name to a slug used as the suffix
    # for the ``_notified_<slug>`` state flag. Real names contain spaces in
    # idiomatic Ansible ("notify: restart nginx"); the slug strips those
    # for use in Python attribute-style state keys.
    handler_name_to_slug: dict[str, str] = {}
    for h in leaf_handlers:
        name = h.get("name")
        if not name:
            raise ValueError("handler tasks must have a 'name'")
        handler_name_to_slug[str(name)] = _slugify(str(name))

    # Validate every notify target refers to a real handler before building
    # anything. A typo in a playbook should fail loudly at convert time.
    for t in leaf_tasks:
        for n in _coerce_notify_to_list(t.get("notify")):
            if n not in handler_name_to_slug:
                raise UnsupportedPlaybookConstruct(
                    f"task {t.get('name', '<unnamed>')!r} notifies unknown handler {n!r}; "
                    f"declared handlers: {sorted(handler_name_to_slug)}"
                )

    # ------------------------------------------------------------------
    # Build the "logical sequence" of FSM nodes: each leaf task, plus a
    # notify-marker after any task that notifies, plus every handler at
    # the end gated on its ``_notified_<name>`` flag. The downstream loop
    # walks this sequence uniformly.
    # ------------------------------------------------------------------
    py_names: list[str] = []
    when_clauses: list[str | None] = []
    failed_when_clauses: list[str | None] = []
    ignore_errors_flags: list[bool] = []
    register_targets: list[str | None] = []
    # Each entry tags whether the node is a real module task or a
    # synthesized notify-marker, and carries the per-type metadata.
    task_meta: list[tuple] = []
    used: set[str] = set()

    def _unique(base: str) -> str:
        candidate = base
        n = 1
        while candidate in used:
            n += 1
            candidate = f"{base}_{n}"
        used.add(candidate)
        return candidate

    def _record(
        *,
        py_name: str,
        when_clause: str | None,
        failed_when_clause: str | None,
        ignore_errors: bool,
        register: str | None,
        meta: tuple,
    ) -> None:
        py_names.append(py_name)
        when_clauses.append(when_clause)
        failed_when_clauses.append(failed_when_clause)
        ignore_errors_flags.append(ignore_errors)
        register_targets.append(register)
        task_meta.append(meta)

    notified_handlers_seen: set[str] = set()
    for idx, task in enumerate(leaf_tasks):
        raw_name = task.get("name") or f"task_{idx + 1}"
        py = _unique(_slugify(raw_name))
        module, args = _module_from_task(task)
        register = task.get("register")
        become = bool(task.get("become", play_become))
        when = task.get("when")
        fw = task.get("failed_when")
        _record(
            py_name=py,
            when_clause=_when_to_expr_string(when) if when is not None else None,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            ignore_errors=bool(task.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become),
        )

        notify_list = _coerce_notify_to_list(task.get("notify"))
        if notify_list:
            # Dedupe handlers but preserve order so the marker writes flags
            # in the order the playbook listed. The marker tracks the
            # *slugified* handler names so the resulting state keys are
            # valid Python identifiers (``_notified_say_hello`` rather than
            # ``_notified_say hello``).
            seen: dict[str, None] = {}
            for handler in notify_list:
                slug = handler_name_to_slug[handler]
                seen.setdefault(slug, None)
            marker_handlers = list(seen)
            notified_handlers_seen.update(marker_handlers)
            marker_name = _unique(f"_notify_{py}")
            _record(
                py_name=marker_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("notify_marker", marker_handlers),
            )

    # Append handlers in declaration order, gated on _notified_<slug>.
    for idx, handler_task in enumerate(leaf_handlers):
        handler_real_name = str(handler_task.get("name") or f"handler_{idx + 1}")
        handler_slug = handler_name_to_slug[handler_real_name]
        # Skip handlers nobody notifies; keeps the graph tight.
        if handler_slug not in notified_handlers_seen:
            continue
        py = _unique(_slugify(f"handler_{handler_real_name}"))
        module, args = _module_from_task(handler_task)
        register = handler_task.get("register")
        become = bool(handler_task.get("become", play_become))
        when_user = handler_task.get("when")
        gate = f"_notified_{handler_slug}"
        if when_user is not None:
            when_combined = f"({_when_to_expr_string(when_user)}) and {gate}"
        else:
            when_combined = gate
        fw = handler_task.get("failed_when")
        _record(
            py_name=py,
            when_clause=when_combined,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            # Handler failures by default propagate; Ansible's --force-handlers
            # behavior is out of scope here.
            ignore_errors=bool(handler_task.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become),
        )

    known_registers: set[str] = {r for r in register_targets if r}

    actions: dict[str, Any] = {}
    for candidate, meta in zip(py_names, task_meta, strict=True):
        kind = meta[0]
        if kind == "module":
            _, module, args, register, become = meta
            actions[candidate] = _build_task_action(
                py_name=candidate,
                module=module,
                args=args,
                register=register,
                become=become,
                known_registers=known_registers,
                play_vars=play_vars,
            )
        elif kind == "notify_marker":
            _, handlers = meta
            actions[candidate] = _build_notify_marker(py_name=candidate, handlers=handlers)
        else:
            raise AssertionError(f"unknown task meta kind: {kind}")

    # Translate Jinja-style attribute access on registered names into the
    # bracket access Python's eval expects. ``known_registers`` was assembled
    # while walking the logical sequence.
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

        # ``when:`` skip behavior with chained skip. For each k>=1, if the
        # next k tasks ALL carry a ``when:`` that evaluates false, jump
        # past all of them to task[i+k+1] (or to ``done``). Longest skip
        # is emitted first so Burr's in-declaration-order condition match
        # picks the deepest skip when multiple are satisfied. This matters
        # for handler chains, where consecutive unnotified handlers all
        # need to be skipped together.
        skip_transitions: list[tuple] = []
        accumulated: list[str] = []
        j = i + 1
        while j < len(py_names) and when_clauses[j] is not None:
            accumulated.append(when_clauses[j])  # type: ignore[arg-type]
            skip_target = py_names[j + 1] if j + 1 < len(py_names) else "done"
            condition = " and ".join(f"not ({c})" for c in accumulated)
            skip_transitions.append((current, skip_target, expr(condition)))
            j += 1
        # Longest skip first so Burr's in-declaration-order matching picks
        # the deepest applicable skip when several conditions are satisfied.
        transitions.extend(reversed(skip_transitions))

        transitions.append((current, nxt))

    # ------------------------------------------------------------------
    # Build the Application.
    # ------------------------------------------------------------------
    builder: ApplicationBuilder = ApplicationBuilder().with_actions(**actions)
    for transition_tuple in transitions:
        builder = builder.with_transitions(transition_tuple)

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
    # Pre-seed notify flags so handler when:-gates can be evaluated before
    # any task has had a chance to flip them.
    for handler_name in notified_handlers_seen:
        state_init.setdefault(f"_notified_{handler_name}", False)
    if gather_facts:
        state_init.setdefault("gathered_facts", {})
    builder = builder.with_state(**state_init).with_entrypoint(entry)

    if project is not None:
        from burr.tracking import LocalTrackingClient

        builder = builder.with_tracker(LocalTrackingClient(project=project))  # type: ignore[arg-type]

    return builder.build()
