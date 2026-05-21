"""Convert Ansible YAML playbooks into ansiburr/Burr ``Application`` objects.

Supports the subset of Ansible's playbook syntax that maps cleanly to a
single-host FSM:

  - One play per file (multi-play raises :class:`UnsupportedPlaybookConstruct`).
  - Tasks with a name, exactly one module reference, and a module-args dict.
  - ``when:`` predicates (string expressions; lists are AND-joined). Jinja-
    style attribute access on registered names (``result.rc == 0``) is
    translated to Python bracket access (``result['rc'] == 0``) so Burr's
    ``expr()`` can evaluate the predicate.
  - ``register:`` capturing the full module result into a state key.
  - ``become:`` per-task or per-play.
  - ``failed_when:`` and ``ignore_errors:`` as guard transitions. Failed
    tasks route to an auto-generated ``escalate`` terminal unless
    ``ignore_errors: yes`` is set.
  - ``gather_facts: yes`` lowers to a leading ``ansible.builtin.setup`` action.
  - Play-level ``vars:`` populate ``with_state(...)``.
  - ``block:`` (group-only) inlines its tasks with the block's ``when:``
    AND-propagated to each inner task. ``rescue:`` / ``always:`` still raise.
  - ``include_tasks:`` and ``import_tasks:`` with a literal filesystem path
    read the referenced file and inline its tasks at conversion time, with
    outer ``when:`` and ``notify:`` propagating to every leaf.
  - ``notify:`` + ``handlers:`` round-trip: each notifying task gets a
    synthesized notify-marker that flips a ``_notified_<slug>`` flag when
    ``_last_changed`` is true; handlers are appended after the main tasks
    and gated on their flag.
  - ``loop:`` and ``with_items:`` with a literal list lower into a
    three-action sub-FSM (init -> task -> advance with a back-edge until
    the items are exhausted). The task body sees ``{{ item }}`` in its
    Jinja context.
  - Jinja templates inside task arguments referencing registered values
    are rendered using Burr state per task, so a converted ``msg: "{{ git_check.stdout }}"``
    resolves across plays.

The following constructs raise :class:`UnsupportedPlaybookConstruct` with
the offending node:

  - ``rescue:`` / ``always:`` (deferred to a later release as
    STRATUS-style undo/transactional edges)
  - ``loop_control:``, ``with_dict``, ``with_fileglob``, ``with_subelements``
  - Jinja-templated ``loop:`` / ``with_items:`` values (only literal lists
    are lowered)
  - Jinja-templated ``include_tasks:`` / ``import_tasks:`` paths
  - ``import_role:``, ``include_role:``, ``include:``
  - ``roles:`` blocks
  - ``pre_tasks:`` / ``post_tasks:``
  - ``serial:`` / ``strategy:`` / ``max_fail_percentage:`` / ``any_errors_fatal:``
  - Multi-play files

The output is a runnable :class:`burr.core.Application`. Module tasks are
``@module_action`` decorations under the hood; loop init, loop advance,
notify markers, and the ``escalate`` / ``done`` terminals are pure
Python ``@action`` nodes.

Example::

    from ansiburr import from_playbook

    app = from_playbook("playbook.yml")
    last, _, final = app.run(halt_after=["done", "escalate"])
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

# ``loop:`` and ``with_items:`` are now first-class. ``with_dict``/
# ``with_fileglob``/``with_subelements`` would each need their own item-
# producing semantics; they still raise.
_LOOP_KEYS: frozenset[str] = frozenset({"loop", "with_items"})

_UNSUPPORTED_TASK_KEYS: frozenset[str] = frozenset(
    {
        "rescue",
        "always",
        "loop_control",
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
    bug. Loop keys (``loop``, ``with_items``) are handled by the caller and
    excluded from the module search."""
    for key in task:
        if key in _UNSUPPORTED_TASK_KEYS:
            raise UnsupportedPlaybookConstruct(
                f"task uses unsupported construct {key!r}: {task.get('name', task)}"
            )
    excluded = _RESERVED_TASK_KEYS | _FLATTEN_TASK_KEYS | _LOOP_KEYS
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
            if "always" in task:
                raise UnsupportedPlaybookConstruct(
                    "always: clauses on block: are not yet supported "
                    f"(task: {task.get('name', '<unnamed>')!r}); "
                    "rescue: alone is supported as of v0.0.10"
                )
            block_tasks = task["block"]
            if not isinstance(block_tasks, list):
                raise ValueError(f"block: must be a list, got {type(block_tasks).__name__}")
            if "rescue" in task:
                # Don't inline; the main converter loop handles block+rescue
                # so it can wire the failure -> rescue routing. The inherited
                # when:/notify: have to be pushed into the block & rescue
                # subtrees here so the surrounding logic doesn't lose them.
                rescue_tasks = task["rescue"]
                if not isinstance(rescue_tasks, list):
                    raise ValueError(
                        f"rescue: must be a list, got {type(rescue_tasks).__name__}"
                    )
                # Inline outer when/notify into each block & rescue task so
                # the surrounding pipeline doesn't need to track inheritance.
                # A new task dict is constructed with the inherited fields
                # AND-joined / unioned with whatever each inner task already has.
                preserved = {
                    "block": _flatten_tasks(
                        block_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                    "rescue": _flatten_tasks(
                        rescue_tasks,
                        base_dir=base_dir,
                        inherited_when=combined_when,
                        inherited_notify=combined_notify,
                        depth=depth + 1,
                    ),
                }
                if task.get("name"):
                    preserved["name"] = task["name"]
                flat.append(preserved)
                continue
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


def _when_to_expr_string(when: Any) -> str:
    """Lower Ansible ``when:`` / ``changed_when:`` / ``failed_when:``
    (string, list of strings, or YAML-typed bool) into a single Python
    expression. A list is AND-joined with parentheses around each clause.
    YAML booleans (``changed_when: false``) become the matching Python
    literal."""
    if isinstance(when, bool):
        return "True" if when else "False"
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
    loop_item_state_key: str | None = None,
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
        # Build the Jinja context from play vars, all registered values
        # currently in state, plus any other non-internal state field (so
        # set_fact-written variables resolve in subsequent tasks).
        # Internal sentinels (``_last_*``, ``_loop_*``, ``_notified_*``)
        # are skipped to avoid leaking ansiburr's bookkeeping into user
        # template namespaces.
        context: dict[str, Any] = {**pinned_vars}
        state_dict = state.get_all()
        for name in register_names:
            if name in state_dict:
                context[name] = state_dict[name]
        for key, value in state_dict.items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        if loop_item_state_key is not None and loop_item_state_key in state_dict:
            # Match Ansible's variable name so playbook authors write
            # ``{{ item }}`` and ``{{ item.name }}`` as they would in any
            # loop-bearing task.
            context["item"] = state_dict[loop_item_state_key]
        return _render_jinja(dict(args), context)

    _impl.__name__ = py_name
    return module_action(module, register=register, become=become)(_impl)


def _loop_items_from_task(task: Mapping[str, Any]) -> list[Any]:
    """Extract a literal-list ``loop:`` / ``with_items:`` value from a task,
    or raise :class:`UnsupportedPlaybookConstruct` if the value is
    Jinja-templated, a non-list scalar, or otherwise dynamic."""
    raw = task.get("loop") if "loop" in task else task.get("with_items")
    if isinstance(raw, str):
        raise UnsupportedPlaybookConstruct(
            "loop:/with_items: with a Jinja-templated or string value is not "
            f"supported (got {raw!r}); only literal lists are lowered"
        )
    if not isinstance(raw, list):
        raise ValueError(f"loop:/with_items: must be a list, got {type(raw).__name__}")
    return list(raw)


def _build_loop_init(
    *,
    py_name: str,
    items_state_key: str,
    item_state_key: str,
    idx_state_key: str,
    done_state_key: str,
    items: list[Any],
) -> Any:
    """Build the pure-Python action that seeds the loop's iteration state.

    Writes the literal item list, the current-item field (set to ``items[0]``),
    the index counter (0), and a done flag (False unless items is empty)."""
    writes = [items_state_key, item_state_key, idx_state_key, done_state_key]
    empty = not items

    def _impl(state: State) -> State:
        return state.update(
            **{
                items_state_key: list(items),
                item_state_key: items[0] if not empty else None,
                idx_state_key: 0,
                done_state_key: empty,
            }
        )

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_loop_advance(
    *,
    py_name: str,
    items_state_key: str,
    item_state_key: str,
    idx_state_key: str,
    done_state_key: str,
) -> Any:
    """Build the pure-Python action that advances the loop counter and
    populates the next item, or flags ``done`` when the list is exhausted."""
    reads = [items_state_key, idx_state_key]
    writes = [item_state_key, idx_state_key, done_state_key]

    def _impl(state: State) -> State:
        items = state[items_state_key]
        next_idx = state[idx_state_key] + 1
        if next_idx < len(items):
            return state.update(
                **{
                    item_state_key: items[next_idx],
                    idx_state_key: next_idx,
                    done_state_key: False,
                }
            )
        return state.update(**{idx_state_key: next_idx, done_state_key: True})

    _impl.__name__ = py_name
    return action(reads=reads, writes=writes)(_impl)


_SET_FACT_MODULES: frozenset[str] = frozenset({"set_fact", "ansible.builtin.set_fact"})


def _build_clear_failure_action(*, py_name: str) -> Any:
    """Build a pure-Python action that resets the failure sentinels.

    Inserted between a successfully-completed ``rescue:`` chain and the
    downstream tasks following the block. Ansible treats a successful
    rescue as "the failure was handled," so ``_last_failed`` and the
    associated diagnostic sentinels reset to their healthy values before
    later transitions can read them. Without this, a downstream
    ``failed_when:`` or default-escalate routing would re-trigger on a
    stale failure.
    """
    writes = ["_last_failed", "_last_msg", "_last_unreachable", "_last_failure_kind"]

    def _impl(state: State) -> State:
        return state.update(
            _last_failed=False,
            _last_msg="",
            _last_unreachable=False,
            _last_failure_kind="ok",
        )

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_set_fact_action(
    *,
    py_name: str,
    args: Mapping[str, Any],
    known_registers: Iterable[str],
    play_vars: Mapping[str, Any],
) -> Any:
    """Build a pure-Python action that mirrors Ansible's ``set_fact:``.

    Each key in ``args`` becomes a state field. Values containing Jinja
    templates are rendered against play vars plus any registered values
    visible in current state, so ``set_fact: total: "{{ a + b }}"`` works
    the way it would inside one Ansible play (even though our converter
    runs each task in its own play and would otherwise drop the new fact).
    """
    register_names = list(known_registers)
    pinned_vars = dict(play_vars)
    writes = list(args.keys())

    def _impl(state: State) -> State:
        context: dict[str, Any] = {**pinned_vars}
        state_dict = state.get_all()
        for name in register_names:
            if name in state_dict:
                context[name] = state_dict[name]
        for key, value in state_dict.items():
            if key.startswith("_"):
                continue
            context.setdefault(key, value)
        rendered = _render_jinja(dict(args), context)
        return state.update(**rendered)

    _impl.__name__ = py_name
    return action(reads=[], writes=writes)(_impl)


def _build_changed_when_post(
    *,
    py_name: str,
    expression: str,
    known_registers: Iterable[str],
) -> Any:
    """Build a pure-Python action that re-evaluates ``_last_changed`` per
    the task's ``changed_when:`` expression.

    Ansible's ``changed_when: false`` (and predicates like
    ``changed_when: result.rc != 0``) override the module's idea of
    whether anything changed. Inserted immediately after a task whose YAML
    sets ``changed_when:``. Reads the same Burr state Burr's expr() reads
    so the predicate has access to registered values.
    """
    # Translate Jinja-style attribute access on registered names to bracket
    # access so Python's ``eval`` can evaluate it. Same transformation the
    # ``when:`` lowering uses elsewhere in the converter.
    translated = _translate_register_dot_access(expression, known_registers)

    def _impl(state: State) -> State:
        state_dict = state.get_all()
        try:
            value = bool(eval(translated, {"__builtins__": {}}, dict(state_dict)))
        except Exception:
            # If the expression can't evaluate (e.g. references a register
            # the upstream task didn't populate), be conservative: don't
            # flip changed.
            value = bool(state_dict.get("_last_changed"))
        return state.update(_last_changed=value)

    _impl.__name__ = py_name
    return action(reads=["_last_changed"], writes=["_last_changed"])(_impl)


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
    # Index tracking for emitting loop back-edges during the transition pass.
    # Maps the loop-task's py_name -> (init_py_name, advance_py_name, done_state_key).
    loop_back_edges: dict[str, tuple[str, str, str]] = {}
    # Per-task block routing context, populated when a leaf is part of a
    # block + rescue group. Maps py_name -> (rescue_first, clear_action, is_last)
    # for block tasks. Rescue tasks and the clear action do not need
    # overrides because their standard escalate / next-seq behavior is
    # exactly what Ansible's semantics ask for. The transition builder
    # consults this dict to emit the block_task -> rescue_first transition
    # on failure and the block_last -> clear_action transition on success.
    block_ctxs: dict[str, tuple[str, str, bool]] = {}

    for idx, task in enumerate(leaf_tasks):
        raw_name = task.get("name") or f"task_{idx + 1}"

        # block + rescue (no always for v0.0.10) is lowered into a flat
        # sequence: [block_task_1, ..., block_last, rescue_first, ...,
        # rescue_last, clear_action]. Failure in any block task routes to
        # rescue_first; rescue_last falls through to clear_action by default;
        # clear_action wipes the failure sentinels so downstream tasks don't
        # see a stale failure. The override logic for block-on-success
        # (skipping past the rescue chain) lives in the transition builder.
        if "block" in task:
            block_inner = task["block"]
            rescue_inner = task["rescue"]
            block_leaves = _flatten_tasks(block_inner, base_dir=base_dir)
            rescue_leaves = _flatten_tasks(rescue_inner, base_dir=base_dir)
            if not block_leaves:
                raise ValueError("block: must contain at least one task")
            if not rescue_leaves:
                raise ValueError("rescue: must contain at least one task")
            if any("block" in leaf for leaf in block_leaves + rescue_leaves):
                raise UnsupportedPlaybookConstruct(
                    "nested block + rescue is not yet supported; "
                    "v0.0.10 only handles a single non-nested block + rescue"
                )

            # Pre-allocate py_names for everything so block tasks can carry
            # the rescue_first reference forward.
            block_py_names: list[str] = []
            for b_idx, btask in enumerate(block_leaves):
                b_raw = btask.get("name") or f"block_task_{b_idx + 1}"
                block_py_names.append(_unique(_slugify(b_raw)))
            rescue_py_names: list[str] = []
            for r_idx, rtask in enumerate(rescue_leaves):
                r_raw = rtask.get("name") or f"rescue_task_{r_idx + 1}"
                rescue_py_names.append(_unique(_slugify(r_raw)))
            clear_py_name = _unique("_block_clear_failure")
            rescue_first = rescue_py_names[0]

            def _record_inner(py: str, t: dict[str, Any]) -> None:
                """Record a single block/rescue inner task. v0.0.10 supports
                module tasks and ``set_fact:``. ``notify:``, ``loop:``, and
                ``changed_when:`` inside a block/rescue are not yet lowered
                (those features still work outside block/rescue)."""
                if any(k in t for k in ("loop", "with_items", "notify", "changed_when")):
                    raise UnsupportedPlaybookConstruct(
                        "loop:/with_items:/notify:/changed_when: inside block: "
                        "or rescue: are not yet supported (planned for v0.0.11); "
                        f"task: {t.get('name', '<unnamed>')!r}"
                    )
                module, args = _module_from_task(t)
                register = t.get("register")
                become = bool(t.get("become", play_become))
                when = t.get("when")
                fw = t.get("failed_when")
                if module in _SET_FACT_MODULES:
                    _record(
                        py_name=py,
                        when_clause=_when_to_expr_string(when) if when is not None else None,
                        failed_when_clause=None,
                        ignore_errors=True,
                        register=None,
                        meta=("set_fact", args),
                    )
                    return
                _record(
                    py_name=py,
                    when_clause=_when_to_expr_string(when) if when is not None else None,
                    failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
                    ignore_errors=bool(t.get("ignore_errors", False)),
                    register=register,
                    meta=("module", module, args, register, become),
                )

            for b_idx, (py, btask) in enumerate(zip(block_py_names, block_leaves, strict=True)):
                is_last_block = b_idx == len(block_py_names) - 1
                block_ctxs[py] = (rescue_first, clear_py_name, is_last_block)
                _record_inner(py, btask)
            for py, rtask in zip(rescue_py_names, rescue_leaves, strict=True):
                _record_inner(py, rtask)
            _record(
                py_name=clear_py_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("clear_failure",),
            )
            continue

        # ``loop:`` / ``with_items:`` lower to a three-action sub-FSM:
        # init -> task -> advance, with a back-edge from advance to task
        # until the items are exhausted. The task action reads the current
        # item from a dedicated state key and exposes it as ``{{ item }}``
        # in the rendered Jinja context, matching Ansible's variable naming.
        if "loop" in task or "with_items" in task:
            base_py = _slugify(raw_name)
            init_py = _unique(f"{base_py}_loop_init")
            task_py = _unique(base_py)
            advance_py = _unique(f"{base_py}_loop_advance")
            items_key = f"_loop_{task_py}_items"
            item_key = f"_loop_{task_py}_item"
            idx_key = f"_loop_{task_py}_idx"
            done_key = f"_loop_{task_py}_done"
            items = _loop_items_from_task(task)
            module, args = _module_from_task(task)
            register = task.get("register")
            become = bool(task.get("become", play_become))
            when = task.get("when")
            fw = task.get("failed_when")

            _record(
                py_name=init_py,
                when_clause=_when_to_expr_string(when) if when is not None else None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("loop_init", items_key, item_key, idx_key, done_key, items),
            )
            _record(
                py_name=task_py,
                when_clause=None,
                failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
                ignore_errors=bool(task.get("ignore_errors", False)),
                register=register,
                meta=("module", module, args, register, become, item_key),
            )
            _record(
                py_name=advance_py,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("loop_advance", items_key, item_key, idx_key, done_key),
            )
            loop_back_edges[advance_py] = (init_py, task_py, done_key)

            notify_list = _coerce_notify_to_list(task.get("notify"))
            if notify_list:
                seen: dict[str, None] = {}
                for handler in notify_list:
                    slug = handler_name_to_slug[handler]
                    seen.setdefault(slug, None)
                marker_handlers = list(seen)
                notified_handlers_seen.update(marker_handlers)
                marker_name = _unique(f"_notify_{task_py}")
                _record(
                    py_name=marker_name,
                    when_clause=None,
                    failed_when_clause=None,
                    ignore_errors=True,
                    register=None,
                    meta=("notify_marker", marker_handlers),
                )
            continue

        py = _unique(_slugify(raw_name))
        module, args = _module_from_task(task)
        register = task.get("register")
        become = bool(task.get("become", play_become))
        when = task.get("when")
        fw = task.get("failed_when")
        cw = task.get("changed_when")

        # ``set_fact:`` is lowered to a pure-Python state-update action
        # rather than handed to ansible-runner, because each converter task
        # runs in its own play and Ansible's fact propagation across plays
        # is unreliable. The arg dict's values are Jinja-rendered against
        # current state, so ``set_fact: total: "{{ a + b }}"`` works as
        # authored.
        if module in _SET_FACT_MODULES:
            _record(
                py_name=py,
                when_clause=_when_to_expr_string(when) if when is not None else None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("set_fact", args),
            )
            continue

        _record(
            py_name=py,
            when_clause=_when_to_expr_string(when) if when is not None else None,
            failed_when_clause=_when_to_expr_string(fw) if fw is not None else None,
            ignore_errors=bool(task.get("ignore_errors", False)),
            register=register,
            meta=("module", module, args, register, become),
        )

        # ``changed_when:`` overrides Ansible's idea of whether the task
        # changed anything (``changed_when: false`` for a read-only command,
        # ``changed_when: result.rc != 0`` for a custom predicate). Insert
        # a tiny post-action that re-evaluates _last_changed against state.
        if cw is not None:
            cw_expr = _when_to_expr_string(cw)
            post_name = _unique(f"_changed_when_{py}")
            _record(
                py_name=post_name,
                when_clause=None,
                failed_when_clause=None,
                ignore_errors=True,
                register=None,
                meta=("changed_when_post", cw_expr),
            )

        notify_list = _coerce_notify_to_list(task.get("notify"))
        if notify_list:
            # Dedupe handlers but preserve order so the marker writes flags
            # in the order the playbook listed. The marker tracks the
            # *slugified* handler names so the resulting state keys are
            # valid Python identifiers (``_notified_say_hello`` rather than
            # ``_notified_say hello``).
            slugs_seen: dict[str, None] = {}
            for handler in notify_list:
                slug = handler_name_to_slug[handler]
                slugs_seen.setdefault(slug, None)
            marker_handlers = list(slugs_seen)
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
            # Plain tasks have 5-tuples (kind, module, args, register, become);
            # loop-body tasks have 6-tuples with an extra loop-item state key.
            if len(meta) == 5:
                _, module, args, register, become = meta
                loop_item_key = None
            else:
                _, module, args, register, become, loop_item_key = meta
            actions[candidate] = _build_task_action(
                py_name=candidate,
                module=module,
                args=args,
                register=register,
                become=become,
                known_registers=known_registers,
                play_vars=play_vars,
                loop_item_state_key=loop_item_key,
            )
        elif kind == "notify_marker":
            _, handlers = meta
            actions[candidate] = _build_notify_marker(py_name=candidate, handlers=handlers)
        elif kind == "loop_init":
            _, items_key, item_key, idx_key, done_key, items = meta
            actions[candidate] = _build_loop_init(
                py_name=candidate,
                items_state_key=items_key,
                item_state_key=item_key,
                idx_state_key=idx_key,
                done_state_key=done_key,
                items=items,
            )
        elif kind == "loop_advance":
            _, items_key, item_key, idx_key, done_key = meta
            actions[candidate] = _build_loop_advance(
                py_name=candidate,
                items_state_key=items_key,
                item_state_key=item_key,
                idx_state_key=idx_key,
                done_state_key=done_key,
            )
        elif kind == "set_fact":
            _, set_fact_args = meta
            actions[candidate] = _build_set_fact_action(
                py_name=candidate,
                args=set_fact_args,
                known_registers=known_registers,
                play_vars=play_vars,
            )
        elif kind == "changed_when_post":
            _, cw_expr = meta
            actions[candidate] = _build_changed_when_post(
                py_name=candidate,
                expression=cw_expr,
                known_registers=known_registers,
            )
        elif kind == "clear_failure":
            actions[candidate] = _build_clear_failure_action(py_name=candidate)
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

        # block + rescue: a failing block task must route to the rescue
        # chain, not to escalate. The block_ctx tag identifies these
        # positions; the rescue chain itself uses default escalate routing
        # (a failing rescue task IS a real escalation).
        block_ctx = block_ctxs.get(current)
        if block_ctx is not None:
            rescue_first_target, clear_action_target, is_block_last_flag = block_ctx
            is_block_task = True
            is_block_last = is_block_last_flag
        else:
            rescue_first_target = None
            clear_action_target = None
            is_block_task = False
            is_block_last = False

        # Failure routing for the current task: if the failed_when expression
        # is satisfied (or _last_failed if none was given) and ignore_errors
        # was not set, the FSM routes to the escalate terminal, except for
        # block tasks under a rescue clause, where it routes to the rescue
        # chain's entry instead.
        if not ignore_errors_flags[i]:
            failure_predicate = failed_when_clauses[i] or "_last_failed"
            failure_target = rescue_first_target if is_block_task else "escalate"
            transitions.append((current, failure_target, expr(failure_predicate)))

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

        # Loop back-edge: when this position is a ``loop_advance``, route
        # back to the loop body until the iteration is exhausted. The
        # default ``(current, nxt)`` then handles the exhausted case by
        # falling through to whatever follows the loop block.
        if current in loop_back_edges:
            _init_py, task_py, done_key = loop_back_edges[current]
            transitions.append((current, task_py, expr(f"not {done_key}")))

        # block_last success: skip past the rescue chain to the clear action.
        # The standard next-seq for block_last would route into rescue_first,
        # which is correct only on failure.
        if is_block_last:
            transitions.append((current, clear_action_target))
        else:
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
    # Pre-seed loop state so loop-init isn't strictly required for the
    # state to be readable. Each loop's init action overwrites these.
    for meta in task_meta:
        if meta[0] == "loop_init":
            _, items_key, item_key, idx_key, done_key, _items = meta
            state_init.setdefault(items_key, [])
            state_init.setdefault(item_key, None)
            state_init.setdefault(idx_key, 0)
            state_init.setdefault(done_key, False)
    if gather_facts:
        state_init.setdefault("gathered_facts", {})
    builder = builder.with_state(**state_init).with_entrypoint(entry)

    if project is not None:
        from burr.tracking import LocalTrackingClient

        builder = builder.with_tracker(LocalTrackingClient(project=project))  # type: ignore[arg-type]

    return builder.build()
