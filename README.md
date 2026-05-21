# ansiburr

> *Pardon me, are you Ansi-Burr, Sir?*

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

Save as `my_fsm.py` and run with `python my_fsm.py`. No remote host or extra setup required: `ansible.builtin.ping` runs against localhost via the `ansible-runner` already pulled in by `pip install ansiburr`.

```python
from burr.core import ApplicationBuilder, action
from ansiburr import module_action, initial_sentinels


# `@module_action` turns a function that returns a dict of Ansible module
# args into a Burr `@action`. The module runs through ansible-runner and
# the result projects into State. `writes=["ping"]` projects the module's
# `ping` field; ansiburr also writes ambient `_last_*` sentinels on every
# call (`_last_failed`, `_last_changed`, `_last_msg`, etc.).
@module_action("ansible.builtin.ping", writes=["ping"])
def check(state):
    return {}


# A regular Burr `@action` is a pure-Python step. It reads from State and
# returns the new State. Mixing module actions and plain actions in the
# same graph is the common pattern.
@action(reads=["ping", "_last_failed", "_last_msg"], writes=["report"])
def summarize(state):
    if state["_last_failed"]:
        return state.update(report=f"ping failed: {state['_last_msg']}")
    return state.update(report=f"ansible reachable: ping={state['ping']!r}")


app = (
    ApplicationBuilder()
    .with_actions(check=check, summarize=summarize)
    .with_transitions(("check", "summarize"))
    .with_state(**initial_sentinels(), ping="", report="")
    .with_entrypoint("check")
    .build()
)

_, _, final = app.run(halt_after=["summarize"])
print(final["report"])
# -> ansible reachable: ping='pong'
```

From there, the moves are:

- Add `host()` to point a group of actions at a remote target without repeating the connection dict.
- Use `host.gather_facts()` to expand `ansible_facts` into top-level state keys (`ansible_pkg_mgr`, `ansible_os_family`, etc.) and branch transitions on them.
- Use `wait_until()` for polling sub-graphs where each attempt is a discrete trace step.
- Use `check_mode=True` + `diff=True` for plan-then-apply patterns with a deterministic review gate.

Working examples of each are in [`examples/`](./examples/).

## From an existing playbook

If you already have an Ansible playbook, `from_playbook(...)` lifts it into a runnable Burr `Application` without rewriting the YAML. The full demo lives in [`examples/from_playbook/`](./examples/from_playbook/); here's the shape:

```yaml
# playbook.yml
- name: tool availability check
  hosts: localhost
  gather_facts: no

  tasks:
    - name: check for git
      ansible.builtin.command:
        cmd: git --version
      register: git_check
      ignore_errors: yes
      changed_when: false

    - name: report git availability
      ansible.builtin.debug:
        msg: "git is installed: {{ git_check.stdout }}"
      when: git_check.rc == 0

    - name: check for jq
      ansible.builtin.command:
        cmd: jq --version
      register: jq_check
      ignore_errors: yes
      changed_when: false
```

```python
# run.py
import ansiburr

app = ansiburr.from_playbook("playbook.yml")
last_action, _, final = app.run(halt_after=["done", "escalate"])

print(f"git: rc={final['git_check']['rc']} {final['git_check'].get('stdout', '').strip()}")
print(f"jq:  rc={final['jq_check']['rc']}  {final['jq_check'].get('stdout', '').strip()}")
```

Output (when both binaries are present):

```
git: rc=0 git version 2.50.1 (Apple Git-155)
jq:  rc=0  jq-1.7.1-apple
```

The converter handles a substantial subset of single-play Ansible: `name`, `when:` (including attribute access on registered names), `register:`, `failed_when:`, `changed_when:`, `ignore_errors:`, `become:`, `gather_facts:`, play-level `vars:`, `block:` (group-only), `include_tasks:` and `import_tasks:` (literal paths), `notify:` plus `handlers:`, `loop:` and `with_items:` (literal lists), `set_fact:`, and Jinja2 templates in task arguments. Each task runs as its own play under the hood, so Jinja references to set_fact-written and registered values resolve through Burr state rather than ansible's cross-play context. `rescue:` / `always:`, Jinja-templated `loop:` or `include:` values, `roles:`, multi-play files, and the parallelism keywords (`serial:` / `strategy:`) raise `UnsupportedPlaybookConstruct` at conversion time with the offending node named in the message, so a partially-converted FSM never starts. The full supported-vs-rejected list is in [REFERENCE.md](./REFERENCE.md). From here, the resulting Application can be hand-edited (add transitions, swap actions, wire in policy gates) or the playbook can stay as the source of truth and be re-converted.

## CLI

`pip install ansiburr` ships an `ansiburr` command for running and inspecting FSMs without writing a wrapper script.

```sh
# Run a playbook directly (no manual conversion).
ansiburr run playbook.yml

# Run a Python module that exposes ``app`` (or a ``build_application()`` callable).
ansiburr run examples/localhost_disk_check.py

# Print the FSM structure as mermaid (default), graphviz dot, or plain text.
ansiburr graph examples/from_playbook/playbook.yml --format text
ansiburr graph examples/from_playbook/playbook.yml --format mermaid
ansiburr graph examples/from_playbook/playbook.yml --format dot
```

`ansiburr run` halts on `done` or `escalate` by default, and accepts `--halt-after ACTION` (repeatable) to override.

## Demo corpus

`examples/` contains twelve self-contained FSMs plus two playbook-conversion demos. Most of the FSMs run against a local Docker container set up by `examples/service_remediation/setup.sh`; the conversion demos and a few others run locally.

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
| `from_playbook` | Small playbook (`command` + `register` + `when` + Jinja debug) lifted via `from_playbook(...)` | `ansible.builtin` |
| `from_playbook_advanced` | Multi-feature playbook (`set_fact`, `block`, `loop`, `notify` + handlers, `changed_when`) lifted via `from_playbook(...)`; runs locally | `ansible.builtin` |

Each example runs in seconds. Run an individual demo with `uv run python examples/<name>/fsm.py` (or `run.py` for the conversion demos).

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
