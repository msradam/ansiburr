# ansiburr

Run Ansible modules as Burr state-machine actions in Python.

A single decorator wraps an Ansible module call as a Burr `@action`. The module runs through `ansible-runner` against the target host. Its result projects into Burr's `State`, and the action's `_last_failed`, `_last_changed`, and `_last_msg` flags become available to downstream transitions. The output is a standard Burr `Application` that runs, persists, traces, and serves like any other Burr graph.

![ansiburr stepping through a deploy-and-wait FSM with a polling sub-graph](vhs/hero.gif)

## What you can build

- Self-healing service workflows that observe, decide, and remediate one Ansible module at a time, with every step visible in Burr's tracker.
- SRE agents where an LLM picks one label from a fixed allow-list of remediation actions and the FSM (not the model) enforces termination and retry policy.
- Cross-platform automation that gathers facts on the target up front and dispatches to the right modules based on the OS family, init system, or package manager.
- Plan-then-apply pipelines using Ansible's `--check` and `--diff` with a deterministic review gate before any change runs.
- Polling sub-graphs (port readiness, service health, file existence) where every poll attempt is a discrete step in the trace.

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

The full library API and the Ansible-playbook-idiom mapping live in [REFERENCE.md](./REFERENCE.md).

## Dependencies and licensing

ansiburr is licensed under the Apache License 2.0. See `LICENSE` for the full text.

ansiburr imports only Apache-2.0 and MIT licensed code (`ansible-runner`, `burr`, `pyyaml`). At runtime it requires `ansible-core`, which is licensed under GPL-3.0-or-later and is invoked as a separate subprocess by `ansible-runner` rather than imported directly. Anyone redistributing an installed ansiburr environment should be aware that the bundled `ansible-core` component carries GPL-3.0+ obligations, and that individual Ansible collections in the user's `ansible_collections` path may have their own licenses.

The `NOTICE` file contains the canonical attribution and license summary.

This README is engineering documentation, not legal advice.

## Development

```sh
git clone https://github.com/msradam/ansiburr
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
