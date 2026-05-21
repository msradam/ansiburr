# ansiburr

Ansible-module-backed Burr state machines.

`ansiburr.module_action` is a decorator that turns a function returning Ansible module arguments into a Burr `@action` that invokes the module via `ansible-runner`. The output is a standard Burr `Application`. Ansible's vars, facts, and registered results map onto Burr's `State`: `gather_facts` flattens facts into top-level state keys, `register:` projects a full module result, `when:` and `failed_when:` become transition predicates, `block / rescue` becomes a guarded sub-graph.

## Ansible-to-Burr mapping

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

Two rows above required new ansiburr primitives: `gather_facts()` for state expansion and `register=` for full-result capture. The rest is supported by Burr directly. See `src/ansiburr/__init__.py` for the canonical reference.

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

An FSM that gathers facts from a remote host and branches on the package manager:

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

The same FSM works against Debian, RHEL, Fedora, Arch, or any other distro Ansible supports. Transitions branch on the gathered `ansible_pkg_mgr` fact.

## Demo corpus

`examples/` contains eleven FSMs. Most run against a local Docker container set up by `examples/service_remediation/setup.sh`.

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

Each example is self-contained and runs in seconds.

## Library reference

Exports from `ansiburr`. The canonical docstring is in `src/ansiburr/__init__.py`.

- `module_action(module, reads, writes, register, host, connection, become, check_mode, diff)`. The core decorator. Wraps a function returning module args into a Burr `@action` that invokes the module via ansible-runner.
- `host(name, **hostvars)`. Connection profile. Captures Ansible hostvars plus `become` once; exposes `.module(fqcn, ...)` and shorthands (`.service`, `.copy`, `.shell`, `.command`, `.template`, `.file`, `.find`, `.slurp`, `.uri`, `.systemd`).
- `host.gather_facts()`. Runs `ansible.builtin.setup`. Flattens facts into top-level State keys; the full dict lands at `state['facts']`.
- `host.initial_facts()`. Placeholder seeds for `with_state(...)`, so transitions can read fact keys before the gather has executed.
- `initial_sentinels()`. Placeholder seeds for the ambient `_last_*` keys (`_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`).
- `snapshot_sentinels(write="failure_reason")`. Pure-Python `@action` that persists the current sentinels into a durable state key. Used when a recovery action would otherwise overwrite the original failure diagnostic.
- `wait_until(name, check, condition_expr, max_attempts, interval_s, on_success, on_timeout)`. Polling sub-graph builder. Returns a `WaitGraph(actions, transitions, entry, initial_state)` to merge into an `ApplicationBuilder`. Each attempt is one Burr step.
- `run_module(module, args, host, connection, become, check_mode, diff)`. The underlying runner, exposed for callers that want the raw ansible-runner call without the decorator.

Every `@module_action` writes the ambient sentinels: `_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`. Burr's tracker captures the full Ansible module result dict per step, so the trace shows `stdout`, `stderr`, `rc`, `diff`, `changed`, and any module-specific fields alongside the state snapshot.

## Why ansiburr versus a hand-rolled wrapper

Embedding `ansible_runner.run(...)` inside a Burr `@action` directly is straightforward. ansiburr exists for the bookkeeping that accumulates as a graph grows past a handful of actions:

- Connection metadata duplication. `host()` captures it once.
- Failure, change, and unreachable tracking per action. Ambient sentinels let transitions read `_last_failed` without each user declaring it.
- Fact gathering as state expansion. Without ansiburr, all hundred or so facts land in one opaque dict and transitions cannot branch on them directly.
- Plan-before-apply via `check_mode=True` and `diff=True`, with the structured diff projected into a state field the tracker captures.
- Polling-loop semantics for `wait_for`-style behavior, expressed as observable FSM transitions rather than one opaque blocking step.
- ControlPersist and pipelining defaults, so multi-module sequences targeting the same host reuse one SSH session.

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
uv run ruff check .
uv run mypy src/ansiburr
```

Most examples require a small Docker container. `examples/service_remediation/setup.sh` builds the image and generates a per-clone SSH key; `examples/service_remediation/start.sh` runs the container. Run an individual demo with `uv run python examples/<name>/fsm.py`.

ansiburr was developed with significant AI assistance (Anthropic's Claude). All changes were reviewed and committed by the project owner.

## Acknowledgements

- The Burr team (Apache Software Foundation) for the FSM substrate.
- The Ansible community for the module ecosystem.
- IBM Research and UC Berkeley for the MAST failure-mode taxonomy ([blog](https://huggingface.co/blog/ibm-research/itbenchandmast), [arXiv:2503.13657](https://arxiv.org/abs/2503.13657)).

## License

Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
