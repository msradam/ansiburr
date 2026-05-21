# ansiburr

Run Ansible modules as Burr state-machine actions in Python.

A single decorator wraps an Ansible module call as a Burr `@action`. The module runs through `ansible-runner` against the target host. Its result projects into Burr's `State`, and the action's `_last_failed`, `_last_changed`, and `_last_msg` flags become available to downstream transitions. The output is a standard Burr `Application` that runs, persists, traces, and serves like any other Burr graph.

## What you can build

- Self-healing service workflows that observe, decide, and remediate one Ansible module at a time, with every step visible in Burr's tracker.
- SRE agents where an LLM picks one label from a fixed allow-list of remediation actions and the FSM (not the model) enforces termination and retry policy.
- Cross-platform automation that gathers facts on the target up front and dispatches to the right modules based on the OS family, init system, or package manager.
- Plan-then-apply pipelines using Ansible's `--check` and `--diff` with a deterministic review gate before any change runs.
- Polling sub-graphs (port readiness, service health, file existence) where every poll attempt is a discrete step in the trace.

## Why not just call ansible-runner directly

A direct `ansible_runner.run(...)` call inside a Burr `@action` works for one or two modules. Beyond that, the same bookkeeping gets repeated each time. ansiburr collects it into a small set of conventions:

- `host()` declares connection metadata once and exposes module shorthands (`.service`, `.copy`, `.shell`, `.template`, etc.) bound to that host.
- Ambient `_last_failed`, `_last_changed`, `_last_unreachable`, and `_last_msg` state keys make transitions readable without per-action declarations.
- `gather_facts()` flattens facts into top-level state keys so transitions can branch on `ansible_pkg_mgr` or `ansible_os_family`.
- `register=` captures the full module result dict when transitions need it.
- `check_mode=True` and `diff=True` project Ansible's structured diff into a state field for plan-before-apply patterns.
- `wait_until()` materializes polling loops as observable FSM steps instead of one opaque blocking module call.
- ControlPersist and pipelining are enabled by default for SSH connection reuse across modules targeting the same host.

## Install

```sh
uv add ansiburr
# or
pip install ansiburr
```

`ansible-core` is pulled in transitively as a runtime requirement. Install additional collections via `ansible-galaxy`:

```sh
ansible-galaxy collection install community.general community.crypto community.docker ansible.posix
```

## Quickstart

An FSM that gathers facts from a remote host and dispatches to a distro-appropriate package inspection command:

```python
from burr.core import ApplicationBuilder, action, expr
from burr.tracking import LocalTrackingClient
from ansiburr import host, initial_sentinels

target = host(
    "target",
    ansible_host="server.example.com",
    ansible_user="ops",
    ansible_ssh_private_key_file="~/.ssh/id_ed25519",
    become=True,
)


@target.shell(register="pkg_inspect")
def inspect_apt(state):
    return {"cmd": "dpkg -l | wc -l"}


@target.shell(register="pkg_inspect")
def inspect_dnf(state):
    return {"cmd": "rpm -qa | wc -l"}


@action(
    reads=["ansible_distribution", "ansible_pkg_mgr", "pkg_inspect"],
    writes=["report"],
)
def summarize(state):
    count = (state["pkg_inspect"].get("stdout") or "0").strip()
    return state.update(
        report=f"{state['ansible_distribution']} ({state['ansible_pkg_mgr']}): {count} pkgs"
    )


app = (
    ApplicationBuilder()
    .with_actions(
        gather=target.gather_facts(),
        inspect_apt=inspect_apt,
        inspect_dnf=inspect_dnf,
        summarize=summarize,
    )
    .with_transitions(
        ("gather", "inspect_apt", expr("ansible_pkg_mgr == 'apt'")),
        ("gather", "inspect_dnf", expr("ansible_pkg_mgr == 'dnf'")),
        ("inspect_apt", "summarize"),
        ("inspect_dnf", "summarize"),
    )
    .with_tracker(LocalTrackingClient(project="quickstart"))
    .with_state(**initial_sentinels(), **target.initial_facts(), pkg_inspect={}, report="")
    .with_entrypoint("gather")
    .build()
)

_, _, final = app.run(halt_after=["summarize"])
print(final["report"])
```

The same FSM works against Debian, RHEL, Fedora, Arch, or any other distro Ansible supports. The branch is taken by the gathered fact, not by hard-coded logic.

## Recorded demos

`fact_driven_inspect`: gather facts on the target, branch on `ansible_pkg_mgr`, run the apt-specific inspection, summarize.

![fact_driven_inspect demo](vhs/fact_driven_inspect.gif)

`plan_then_apply`: three scenarios. A small diff under policy gets approved and applied. An oversized diff is rejected by the review action before any apply runs. A re-run with the default value is idempotent.

![plan_then_apply demo](vhs/plan_then_apply.gif)

The tape scripts that produced these are in `vhs/`. Re-record with `vhs vhs/<name>.tape`.

## Demo corpus

`examples/` contains eleven self-contained FSMs. Most run against a local Docker container set up by `examples/service_remediation/setup.sh`.

| Demo | What it shows | Collections used |
|---|---|---|
| `localhost_disk_check` | Linear chain, pure-Python branching on shell output | `ansible.builtin` |
| `service_remediation` | Retry loop with state counter, ssh plus become, escalate after N attempts | `ansible.builtin` |
| `cert_rotation` | Linear-with-skip, idempotent multi-step rotation, pure-Python date math | `community.crypto`, `ansible.builtin` |
| `config_drift` | Handler equivalent via `_last_changed`, validate-before-apply (`nginx -t`), rollback with reload-after-restore | `ansible.builtin` |
| `user_provisioning` | Iteration via state counter, mid-loop failure preserves partial state | `ansible.builtin`, `ansible.posix` |
| `sidecar_lifecycle` | Container lifecycle FSM running on the controller against local Docker | `community.docker` |
| `log_triage` | Ansible I/O wrapping a Python parser and a Granite-classifier with a deterministic validator gate | `ansible.builtin` |
| `mast_sre_agent` | MAST-aligned deep multi-module remediation (12 Ansible modules plus 7 Python actions) | `ansible.builtin`, `ansible.posix`, `community.general` |
| `coffee_order_ansible` | Burr's `coffee_order` topology with every action body swapped for an Ansible module operating on a filesystem queue | `ansible.builtin` |
| `fact_driven_inspect` | `gather_facts()` state expansion; transitions branch on `ansible_pkg_mgr` | `ansible.builtin` |
| `plan_then_apply` | check+diff plan, deterministic review gate, `wait_until` polling sub-graph, apply with verify | `ansible.builtin` |

Each example runs in seconds. Run an individual demo with `uv run python examples/<name>/fsm.py`.

## Library reference

- `module_action(module, reads, writes, register, host, connection, become, check_mode, diff)`. The core decorator. Wraps a function returning module args into a Burr `@action` that invokes the module via ansible-runner.
- `host(name, **hostvars)`. Connection profile. Captures Ansible hostvars plus `become` once; exposes `.module(fqcn, ...)` and shorthands (`.service`, `.copy`, `.shell`, `.command`, `.template`, `.file`, `.find`, `.slurp`, `.uri`, `.systemd`).
- `host.gather_facts()`. Runs `ansible.builtin.setup`. Flattens facts into top-level State keys; the full dict lands at `state['facts']`.
- `host.initial_facts()`. Placeholder seeds for `with_state(...)`, so transitions can read fact keys before the gather has executed.
- `initial_sentinels()`. Placeholder seeds for the ambient `_last_*` keys (`_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`).
- `snapshot_sentinels(write="failure_reason")`. Pure-Python `@action` that persists the current sentinels into a durable state key. Used when a recovery action would otherwise overwrite the original failure diagnostic.
- `wait_until(name, check, condition_expr, max_attempts, interval_s, on_success, on_timeout)`. Polling sub-graph builder. Returns a `WaitGraph(actions, transitions, entry, initial_state)` to merge into an `ApplicationBuilder`. Each attempt is one Burr step.
- `run_module(module, args, host, connection, become, check_mode, diff)`. The underlying runner, exposed for callers that want the raw ansible-runner call without the decorator.

Every `@module_action` writes the ambient sentinels: `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`. Burr's tracker captures the full Ansible module result dict per step, so the trace shows `stdout`, `stderr`, `rc`, `diff`, `changed`, and any module-specific fields alongside the state snapshot.

## Reference: Ansible playbook idioms in ansiburr

| Ansible playbook idiom | ansiburr / Burr equivalent |
|---|---|
| `gather_facts: yes` | `host.gather_facts()` flattens `ansible_facts` into top-level State keys |
| `vars:` block on a play | `.with_state(**kwargs)` initializer |
| `set_fact: foo: bar` | pure-Python `@action` doing `state.update(foo="bar")` |
| `register: result` | `@module_action(register="result")` or `target.shell(register="result")` |
| `when: cond` | transition predicate `expr("cond")` |
| `failed_when:` | guard transition on `_last_failed` plus state expressions |
| `changed_when:` | guard on `_last_changed` plus computed result fields |
| `block / rescue / always` | guarded transition sub-graph; `rescue:` is an edge gated on `expr("_last_failed")` |
| `notify:` plus handlers | transition guarded by `expr("_last_changed")` to the handler action |
| `loop: items` | FSM iteration via state counter and back-edge |
| `wait_for: ...` | `ansiburr.wait_until(...)` polling sub-graph (each attempt is one Burr step) |

Two of these required new ansiburr primitives: `gather_facts()` and `register=`. The rest are supported by Burr directly.

## Dependencies and licensing

ansiburr is licensed under the Apache License 2.0. See `LICENSE` for the full text.

ansiburr imports only Apache-2.0 and MIT licensed code (`ansible-runner`, `burr`, `pyyaml`). At runtime it requires `ansible-core`, which is licensed under GPL-3.0-or-later and is invoked as a separate subprocess by `ansible-runner` rather than imported directly. Anyone redistributing an installed ansiburr environment should be aware that the bundled `ansible-core` component carries GPL-3.0+ obligations, and that individual Ansible collections in the user's `ansible_collections` path may have their own licenses.

The `NOTICE` file contains the canonical attribution and license summary.

This README is engineering documentation, not legal advice.

## Development

```sh
git clone https://github.com/<owner>/ansiburr
cd ansiburr
uv sync
uv run pytest
uv run ruff check .
uv run mypy src/ansiburr
```

Most examples require a small Docker container. `examples/service_remediation/setup.sh` builds the image and generates a per-clone SSH key; `examples/service_remediation/start.sh` runs the container.

ansiburr was developed with significant AI assistance (Anthropic's Claude). All changes were reviewed and committed by the project owner.

## Acknowledgements

- The Burr team (Apache Software Foundation) for the FSM substrate.
- The Ansible community for the module ecosystem.
- IBM Research and UC Berkeley for the MAST failure-mode taxonomy ([blog](https://huggingface.co/blog/ibm-research/itbenchandmast), [arXiv:2503.13657](https://arxiv.org/abs/2503.13657)).

## License

Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
