# ansiburr reference

## CLI

```
ansiburr run    <path> [--halt-after ACTION ...]
ansiburr graph  <path> [--format {mermaid,dot,text}]
```

`<path>` is either a `.yml`/`.yaml` playbook (lifted via `from_playbook`) or a `.py` file that exposes either a module-level `app` Application or a `build_application()` callable. `run` halts on `done` or `escalate` by default. `graph` defaults to mermaid output.

## Library

- `module_action(module, reads, writes, register, host, connection, become, check_mode, diff)`. The core decorator. Wraps a function returning module args into a Burr `@action` that invokes the module via ansible-runner.
- `host(name, **hostvars)`. Connection profile. Captures Ansible hostvars plus `become` once; exposes `.module(fqcn, ...)` and shorthands (`.service`, `.copy`, `.shell`, `.command`, `.template`, `.file`, `.find`, `.slurp`, `.uri`, `.systemd`).
- `host.gather_facts()`. Runs `ansible.builtin.setup`. Flattens facts into top-level State keys; the full dict lands at `state['facts']`.
- `host.initial_facts()`. Placeholder seeds for `with_state(...)`, so transitions can read fact keys before the gather has executed.
- `initial_sentinels()`. Placeholder seeds for the ambient `_last_*` keys (`_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`).
- `snapshot_sentinels(write="failure_reason")`. Pure-Python `@action` that persists the current sentinels into a durable state key. Used when a recovery action would otherwise overwrite the original failure diagnostic.
- `wait_until(name, check, condition_expr, max_attempts, interval_s, on_success, on_timeout)`. Polling sub-graph builder. Returns a `WaitGraph(actions, transitions, entry, initial_state)` to merge into an `ApplicationBuilder`. Each attempt is one Burr step.
- `from_playbook(path, *, project=None)`. Parses a YAML playbook and returns a runnable `Application`. Supports single play with `tasks:` (and `handlers:`), `when:`, `register:`, `failed_when:`, `ignore_errors:`, `become:`, `gather_facts:`, play-level `vars:`, `block:` (group-only), `include_tasks:` / `import_tasks:` with a literal path, and `notify:` references to declared handlers. Jinja `{{ ... }}` references to registered values are rendered using Burr state per task. Raises `UnsupportedPlaybookConstruct` on `rescue:`/`always:`, `loop:`/`with_*:`, `roles:`, `import_role:`, Jinja-templated include paths, multi-play files, and `serial:`/`strategy:`.
- `run_module(module, args, host, connection, become, check_mode, diff)`. The underlying runner, exposed for callers that want the raw ansible-runner call without the decorator.

Every `@module_action` writes the ambient sentinels: `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`. Burr's tracker captures the full Ansible module result dict per step, so the trace shows `stdout`, `stderr`, `rc`, `diff`, `changed`, and any module-specific fields alongside the state snapshot.

## Ansible playbook idioms in ansiburr

| Ansible playbook idiom | ansiburr / Burr equivalent |
|---|---|
| `gather_facts: yes` | `host.gather_facts()` flattens `ansible_facts` into top-level State keys |
| `vars:` block on a play | `.with_state(**kwargs)` initializer |
| `set_fact: foo: bar` | pure-Python `@action` doing `state.update(foo="bar")` |
| `register: result` | `@module_action(register="result")` or `target.shell(register="result")` |
| `when: cond` | transition predicate `expr("cond")` |
| `failed_when:` | guard transition on `_last_failed` plus state expressions |
| `changed_when:` | guard on `_last_changed` plus computed result fields |
| `block:` (group-only) | inlined into the surrounding task list with outer `when:` AND-propagated |
| `block:` + `rescue:` / `always:` | not yet supported by `from_playbook` (deferred to a later release) |
| `include_tasks:` / `import_tasks:` (literal path) | inlined at conversion time; outer `when:`/`notify:` propagate to leaves |
| `notify:` plus handlers | synthesized marker action sets `_notified_<slug>` when `_last_changed`; handler action appended after main tasks, gated on that flag |
| `loop: items` | not yet supported by `from_playbook`; hand-write FSM iteration via state counter and back-edge |
| `wait_for: ...` | `ansiburr.wait_until(...)` polling sub-graph (each attempt is one Burr step) |

Two of these required new ansiburr primitives: `gather_facts()` and `register=`. The rest are supported by Burr directly.
