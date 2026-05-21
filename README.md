# ansiburr

Ansible-module-backed Burr state machines, with the state model both libraries already share made explicit.

ansiburr is a thin adapter between two libraries that — once you look closely at them — turn out to be talking about the same thing. Ansible's vars/facts/registered universe is its state model: gather observations, project them into named keys, branch on them. Burr's `State` is also a state model: a flat dict that flows between actions, with transitions that branch on it. ansiburr is the bridge that recognizes the alignment and exposes it explicitly. Ansible modules become Burr actions; `gather_facts` becomes state expansion; `register:` becomes a state-key projection; `when:` becomes a transition predicate. Most of what you can do in a playbook you can do directly in a Burr FSM, with Burr's tracker, replay, and observability for free.

## The thesis

| Ansible playbook idiom | ansiburr / Burr equivalent |
|---|---|
| `gather_facts: yes` | `host.gather_facts()` — flattens `ansible_facts` into top-level State keys |
| `vars:` block on a play | `.with_state(**kwargs)` initializer |
| `set_fact: foo: bar` | pure-Python `@action` doing `state.update(foo="bar")` |
| `register: result` | `@module_action(register="result")` or `target.shell(register="result")` |
| `when: cond` | transition predicate `expr("cond")` |
| `failed_when:` | guard transition on `_last_failed` + state expressions |
| `changed_when:` | guard on `_last_changed` + computed result fields |
| `block / rescue / always` | guarded transition sub-graph; `rescue:` becomes an edge gated on `expr("_last_failed")` |
| `notify:` + handlers | transition guarded by `expr("_last_changed")` to the handler action |
| `loop: items` | FSM iteration via state counter + back-edge |
| `wait_for: ...` | `ansiburr.wait_until(...)` polling sub-graph (each attempt is a discrete Burr step) |

Only two rows above needed new ansiburr primitives (`gather_facts()` for state expansion, `register=` for full-result capture). Everything else was already supported by Burr; ansiburr's contribution is recognizing the alignment and documenting it. See `src/ansiburr/__init__.py` for the canonical reference.

## Install

```sh
uv add ansiburr
# or
pip install ansiburr
```

ansible-core is a runtime requirement (pulled in transitively). For collections beyond `ansible.builtin`, install them via `ansible-galaxy`:

```sh
ansible-galaxy collection install community.general community.crypto community.docker ansible.posix
```

## Quickstart

A small FSM that detects the target distro, picks the right package manager, and inspects what's installed:

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

The same FSM works on Debian, RHEL, Fedora, Arch, etc. — the host is asked what it is, transitions route accordingly. No hard-coded distro logic in user code.

## Demo corpus

`examples/` contains eleven FSMs spanning multiple Ansible collections and FSM shapes. Each one runs against the local machine or a small Docker container set up by `examples/service_remediation/setup.sh`.

| Demo | What it shows | Collections used |
|---|---|---|
| `localhost_disk_check` | Linear chain, pure-Python branching on shell output | `ansible.builtin` |
| `service_remediation` | Retry loop with state counter, ssh + become, escalate after N attempts | `ansible.builtin` |
| `cert_rotation` | Linear-with-skip, idempotent multi-step rotation, pure-Python date math | `community.crypto` + `ansible.builtin` |
| `config_drift` | Handler equivalent via `_last_changed`, validate-before-apply (`nginx -t`), auto-rollback with reload-after-restore | `ansible.builtin` |
| `user_provisioning` | Iteration via state counter, mid-loop failure preserves partial-state visibility | `ansible.builtin` + `ansible.posix` |
| `sidecar_lifecycle` | Container lifecycle FSM running on the controller against local Docker | `community.docker` |
| `log_triage` | Ansible I/O wrapping a Python parser and a small-LLM classifier with a deterministic validator gate | `ansible.builtin` |
| `mast_sre_agent` | MAST-aligned deep multi-module remediation (12 Ansible modules + 7 Python actions), explicit failure-mode mapping in docstring | `ansible.builtin`, `ansible.posix`, `community.general` |
| `coffee_order_ansible` | The reversal — Burr's `coffee_order` topology with every action body swapped for an Ansible module operating on a filesystem-as-queue | `ansible.builtin` |
| `fact_driven_inspect` | `gather_facts()` state expansion; transitions branch on `ansible_pkg_mgr` to dispatch to distro-appropriate modules | `ansible.builtin` |
| `plan_then_apply` | check+diff plan, deterministic review gate, `wait_until` polling sub-graph, apply with verify | `ansible.builtin` |

Each example is self-contained and runs in seconds. They double as the test corpus for any library change.

## Library reference

The exports from `ansiburr` are small and stable. The canonical docstring lives in `src/ansiburr/__init__.py`.

- `module_action(module, reads=..., writes=..., register=..., host=..., connection=..., become=..., check_mode=..., diff=...)` — the core decorator. Wraps a function returning module args into a Burr `@action` that invokes the module via ansible-runner.
- `host(name, **hostvars)` — connection profile. Captures Ansible hostvars + `become` once; exposes `.module(fqcn, ...)` and shorthands (`.service`, `.copy`, `.shell`, `.command`, `.template`, `.file`, `.find`, `.slurp`, `.uri`, `.systemd`).
- `host.gather_facts()` — runs `ansible.builtin.setup`, flattens facts into top-level State keys, also drops full dict at `state['facts']`.
- `host.initial_facts()` — placeholder seeds for `with_state(...)` so transitions can read fact keys before the gather has executed.
- `initial_sentinels()` — placeholder seeds for the ambient `_last_*` keys (`_last_action`, `_last_failed`, `_last_changed`, `_last_unreachable`, `_last_msg`).
- `snapshot_sentinels(write="failure_reason")` — pure-Python `@action` that persists the current sentinels into a durable state key, useful when a recovery action would otherwise overwrite the original failure diagnostic.
- `wait_until(name, check, condition_expr, max_attempts, interval_s, on_success, on_timeout)` — polling sub-graph builder. Returns a `WaitGraph(actions, transitions, entry, initial_state)` to merge into your `ApplicationBuilder`. Each attempt is a discrete Burr step.
- `run_module(module, args, host=..., connection=..., become=..., check_mode=..., diff=...)` — the underlying runner, exposed for users who want the raw ansible-runner call without the decorator.

The ambient sentinels (`_last_action` / `_last_failed` / `_last_changed` / `_last_unreachable` / `_last_msg`) are written by every `@module_action` execution. Burr's tracker captures the full Ansible module result dict per step, so the trace shows `stdout`, `stderr`, `rc`, `diff`, `changed`, and any module-specific fields alongside the state snapshot.

## Why ansiburr (vs. just calling ansible-runner from a Burr action)

A reasonable Python developer could embed `ansible_runner.run(...)` inside a regular Burr `@action` in an afternoon. ansiburr is the bookkeeping that becomes painful around the third action:

- Connection metadata duplication across actions targeting the same host. `host()` captures it once.
- Failure-vs-change-vs-unreachable bookkeeping per action. Ambient sentinels mean transitions read `_last_failed` without each user opting in.
- The `gather_facts` → state expansion idiom. Without ansiburr, you either store all 100+ facts in one opaque dict (transitions can't easily branch on them) or hand-write a projector. ansiburr does the curated projection out of the box.
- The plan-before-apply pattern (`check_mode=True` + `diff=True`) wiring into Burr's tracker so the diff is visible as a first-class state field.
- The polling-loop pattern for `wait_for`-style behavior, expressed as observable FSM transitions rather than one opaque blocking step.
- ControlPersist + pipelining defaults so multi-module remediations on the same host don't redo SSH handshakes every time.

None of these are individually hard. ansiburr is the bundle, with the conventions chosen so they compose.

## Dependencies and licensing

ansiburr is **Apache License 2.0**. See `LICENSE` for full text.

ansiburr imports only Apache-2.0 and MIT licensed code (`ansible-runner`, `burr`, `pyyaml`). At runtime it requires `ansible-core`, which is licensed under **GPL-3.0-or-later** and is invoked as a separate subprocess by `ansible-runner` rather than imported directly. Users redistributing an installed ansiburr environment should be aware of the GPL-3.0+ obligations carried by `ansible-core` and any GPL-licensed Ansible collections in their `ansible_collections` path.

The `NOTICE` file in this repository contains the canonical attribution and license summary.

This README documents engineering practice. It does not constitute legal advice.

## Development

```sh
git clone https://github.com/<owner>/ansiburr
cd ansiburr
uv sync
uv run ruff check .
uv run mypy src/ansiburr
```

Most examples require a small Docker container (`examples/service_remediation/setup.sh` builds it, `start.sh` runs it). Run an individual demo with `uv run python examples/<name>/fsm.py`.

ansiburr was developed with substantial AI assistance (Anthropic's Claude). Design exploration, implementation, and documentation were paired with the model; every change was reviewed and committed by the project owner.

## Acknowledgements

- The Burr team (Apache Software Foundation) for the FSM substrate.
- The Ansible community for the module ecosystem.
- IBM Research and UC Berkeley for the MAST failure-mode taxonomy ([blog](https://huggingface.co/blog/ibm-research/itbenchandmast), [arXiv:2503.13657](https://arxiv.org/abs/2503.13657)), which crystallized several of the architectural choices in this library.

## License

Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
