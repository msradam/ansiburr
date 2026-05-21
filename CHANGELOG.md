# Changelog

All notable changes to ansiburr are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.5] - 2026-05-21

### Added

- ``_last_failure_kind`` sentinel populated on every ``@module_action`` call.
  Classifies the result into one of ``ok``, ``unreachable``, ``auth_failed``,
  ``timeout``, or ``module_error``. Lets transitions take distinct recovery
  paths per failure mode without grepping ``_last_msg``: e.g. retry on
  ``unreachable``, escalate immediately on ``auth_failed``, fall back to an
  alternative module on ``module_error``, raise the timeout next try on
  ``timeout``. Loosely aligned with the MAST taxonomy (Multi-Agent System
  failure Taxonomy, IBM Research + UC Berkeley, arXiv:2503.13657) restricted
  to modes a deterministic FSM can detect from an Ansible result.
- Exported as module-level constants: ``ansiburr.FAILURE_KIND_OK``,
  ``FAILURE_KIND_UNREACHABLE``, ``FAILURE_KIND_AUTH_FAILED``,
  ``FAILURE_KIND_TIMEOUT``, ``FAILURE_KIND_MODULE_ERROR``, plus the
  ``FAILURE_KINDS`` tuple. Pattern: use the constants in transition
  predicates rather than string literals for typo-safety.
- ``initial_sentinels()`` seeds ``_last_failure_kind="ok"``.
- Four new tests in ``test_failures.py`` covering: ok on success,
  module_error on intentional fail, timeout on runner cap overrun, and
  branching a transition on the kind.

### Documented

- The classification is conservative and pattern-based: ``unreachable``
  trusts ansible's flag; ``auth_failed`` matches "permission denied" /
  "authentication failed" / publickey-denied phrases in ``msg``;
  ``timeout`` matches "exceeded timeout" / "timed out"; anything else that
  failed lands in ``module_error``. False negatives on auth_failed against
  exotic SSH error wording fall through to ``module_error``, which still
  routes correctly through any failure-handling branch.

## [0.0.4] - 2026-05-21

The converter now ingests the constructs every popular community Ansible
role opens with (OS-conditional ``include_tasks``, ``block:`` grouping,
``notify:`` + handlers). Without this, ``from_playbook`` could not convert
realistic third-party playbooks.

### Changed

- Burr dependency migrated from the pre-incubator ``burr[tracking]`` to
  ``apache-burr[tracking]>=0.42,<0.43``. The upstream project graduated to
  Apache incubation and renamed its PyPI distribution; the internal
  ``burr.core`` and ``burr.tracking`` import paths are unchanged. Fresh
  ``pip install ansiburr`` was previously stranded on the pre-rename
  package.

### Added

- Converter: ``block:`` (group-only) lowers into inline tasks with the
  block's ``when:`` AND-propagated to every inner task. ``rescue:`` /
  ``always:`` still raise ``UnsupportedPlaybookConstruct`` (deferred to a
  later release as STRATUS-style undo/transactional edges).
- Converter: ``include_tasks:`` and ``import_tasks:`` with a literal
  filesystem path inline the referenced file's tasks at conversion time.
  Outer ``when:`` and ``notify:`` propagate down to every included leaf.
  Jinja-templated paths are not yet supported.
- Converter: ``notify:`` + ``handlers:`` round-trip end-to-end. Each
  notifying task gets a synthesized notify-marker action that flips a
  ``_notified_<slug>`` state flag when ``_last_changed`` is true. Handlers
  are appended after the main tasks and gated on their flag. Notify
  references to unknown handlers raise at convert time.
- Converter: chained ``when:`` skip transitions. Consecutive tasks whose
  ``when:`` evaluates false are skipped together rather than only the
  immediate next one. Matters for handler chains with multiple unnotified
  handlers.

### Tests

- Fixtures and tests added for the four new converter features. Updated
  ``test_unsupported_block_raises`` to ``test_block_with_rescue_still_raises``
  (block alone is now supported; ``rescue:``/``always:`` still rejected).
- Total suite: 40 tests, all green.

## [0.0.3] - 2026-05-21

### Added

- `ansiburr` command-line entry point. `ansiburr run <path>` executes an FSM
  from either a YAML playbook or a Python module that exposes an `app`
  attribute (or a `build_application()` callable). `ansiburr graph <path>`
  prints the FSM structure as mermaid (default), graphviz dot, or plain
  text. `--halt-after ACTION` (repeatable) overrides the default halt set;
  unknown halt names produce a clear error instead of an opaque library
  trace.

### Documented

- README has a CLI section between "From an existing playbook" and "Demo
  corpus". REFERENCE.md gets a CLI usage block at the top.

## [0.0.2] - 2026-05-21

Runner hardening and a real playbook-conversion example.

### Added

- `run_module` and `@module_action` accept a `timeout` parameter (default
  300 seconds). On overrun the ansible-playbook subprocess is killed and
  the action's result carries `failed=True` with a diagnostic `msg`.
  Threaded through `Host.module(...)` and the shorthand methods.
- `ANSIBURR_DEBUG=1` environment variable disables ansible-runner's quiet
  mode. The ansible-playbook stdout and stderr stream to the controller,
  useful when a module crashes in a way the event stream does not capture.
- SIGINT handling. Ctrl-C during a long-running module call asks
  ansible-runner to cancel the subprocess via its `cancel_callback`,
  then re-raises `KeyboardInterrupt`. Without this, ansible-playbook
  would orphan and the SSH ControlMaster socket would linger.
- The `from_playbook(...)` converter now renders Jinja2 templates inside
  task arguments using Burr state as the context. Lets a converted
  playbook with `msg: "{{ git_check.stdout }}"` resolve across tasks.
  Previously each task ran as its own play and could not see prior
  registered values.
- `from_playbook(...)` now translates Jinja-style attribute access
  (`when: git_check.rc == 0`) on registered names into Python bracket
  access (`when: git_check['rc'] == 0`) so Burr's `expr()` can evaluate
  the predicate.
- `examples/from_playbook/`: a small playbook (`command` + `register` +
  `ignore_errors` + `changed_when: false` + Jinja-templated debug
  messages + `when: register.attr`) and a `run.py` that converts and
  runs it.
- README "From an existing playbook" section showing the inline YAML,
  the two-line `from_playbook(...)` conversion, and sample output.

### Documented

- The single-Application serial execution contract. Two `@module_action`
  calls against the same host running concurrently can collide on the
  shared SSH ControlPath; parallel Applications across distinct hosts
  is fine.

## [0.0.1] - 2026-05-21

Initial alpha release.

### Added

- `module_action` decorator wrapping `ansible-runner` as a Burr `@action`.
  Supports `reads`, `writes`, `register`, `host`, `connection`, `become`,
  `check_mode`, `diff`.
- `host()` factory plus `Host` dataclass for connection profiles. Captures
  Ansible hostvars and `become` once; exposes shorthand decorators
  (`.module`, `.service`, `.systemd`, `.shell`, `.command`, `.copy`,
  `.template`, `.file`, `.find`, `.slurp`, `.uri`).
- `Host.gather_facts()` runs `ansible.builtin.setup` and flattens facts
  into top-level State keys. `Host.initial_facts()` seeds placeholder
  values so transitions can read fact keys before the gather has executed.
- Ambient sentinel state keys written by every `@module_action`:
  `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`,
  `_last_msg`. `initial_sentinels()` provides placeholder seeds.
- `snapshot_sentinels(write="failure_reason")` pure-Python action that
  preserves the current diagnostic into a durable state key, surviving
  any recovery actions that overwrite the sentinels.
- `wait_until(name, check, condition_expr, max_attempts, interval_s,
  on_success, on_timeout)` polling sub-graph builder. Returns a
  `WaitGraph` to merge into an `ApplicationBuilder`. Each poll attempt
  is a discrete Burr step.
- `from_playbook(path)` forward converter. Parses a YAML playbook and
  returns a runnable `burr.core.Application`. Supports single play with
  flat task list, `when:`, `register:`, `failed_when:`, `become:`,
  `gather_facts:`, and play-level `vars:`. Raises
  `UnsupportedPlaybookConstruct` for blocks, loops, handlers, includes,
  roles, and multi-play structures.
- ControlPersist and pipelining enabled by default for SSH connection
  reuse across modules targeting the same host.
- Eleven self-contained example FSMs in `examples/` spanning
  `ansible.builtin`, `community.crypto`, `community.docker`,
  `community.general`, and `ansible.posix`.
- VHS-recorded GIFs for two demos (`fact_driven_inspect`,
  `plan_then_apply`) plus the tape sources.
- pytest suite with smoke, failure-mode, and converter coverage.
- GitHub Actions CI for Python 3.11, 3.12, 3.13 on Linux.

### Known gaps

- Single-host only. Multi-host inventories and parallel fan-out are not
  yet supported; one Burr `Application` per host is the current pattern.
- No reverse converter. `from_playbook` covers the playbook-to-FSM
  direction; FSM-to-playbook emission is on the roadmap.
- The Burr dependency is pinned to a tight range. Burr is incubating at
  Apache, and an API change in a release ansiburr does not yet support
  will break installs at version-resolution time rather than at runtime.

[0.0.1]: https://github.com/msradam/ansiburr/releases/tag/v0.0.1
[0.0.2]: https://github.com/msradam/ansiburr/releases/tag/v0.0.2
[0.0.3]: https://github.com/msradam/ansiburr/releases/tag/v0.0.3
[0.0.4]: https://github.com/msradam/ansiburr/releases/tag/v0.0.4
[0.0.5]: https://github.com/msradam/ansiburr/releases/tag/v0.0.5
[Unreleased]: https://github.com/msradam/ansiburr/compare/v0.0.5...HEAD
