# Changelog

All notable changes to ansiburr are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.0.1]: https://github.com/amsrahman/ansiburr/releases/tag/v0.0.1
[Unreleased]: https://github.com/amsrahman/ansiburr/compare/v0.0.1...HEAD
