# ansiburr

> *Pardon me, are you Ansi-Burr, Sir?*

ansiburr makes Ansible's catalog of modules the action space for AI agents that operate infrastructure. An LLM picks one label from a fixed allow-list, an explicit Burr state machine decides what is reachable next, and every step is captured in the trace with full Ansible module output. The FSM owns termination, retry policy, validation-before-done, and recovery edges, not the model.

The same primitives also work for non-agent IT automation. Ansible modules expose as Burr `@action` objects, ordinary Python `@action` functions compose freely between them, and the resulting graph runs, persists, and traces through Burr's standard tooling. The conversion path lifts an existing playbook into the same observable Burr `Application` without rewriting any YAML.

![mast_sre_agent: LLM picks a remediation label, a validator gates the pick, the FSM enforces verify-before-done](https://raw.githubusercontent.com/msradam/ansiburr/main/vhs/sre_agent.gif)

The recording above is `examples/mast_sre_agent/` running end-to-end. A Granite model served by Ollama parses a log summary and picks one label from a fixed allow-list (the magenta lines). A deterministic validator action checks the pick against the allow-list and writes a `validation_note`. The FSM then routes through the chosen remediation chain (six Ansible modules for the OOM path), runs an external verification step, and only declares `done` when the verify came back HTTP 200. Off-script picks from the model would route to `escalate` instead, never to `done`.

## What you can build

- SRE agents where an LLM picks one label from a fixed allow-list and the FSM (not the model) enforces termination, retry policy, and verification before declaring success. The relevant research arguments are the MAST failure taxonomy (arXiv:2503.13657) and STRATUS on FSM-structured SRE agents (arXiv:2506.02009).
- Self-healing service workflows that observe, decide, and remediate one Ansible module at a time, with every step visible in Burr's tracker.
- Cross-platform automation that gathers facts on the target up front and dispatches to the right modules based on the OS family, init system, or package manager.
- Plan-then-apply pipelines using Ansible's `--check` and `--diff` with a deterministic review gate before any change runs.
- Polling sub-graphs (port readiness, service health, file existence) where every poll attempt is a discrete step in the trace.

## Why the FSM owns the control flow

The dominant pattern for LLM-driven infrastructure agents today is a ReAct-style loop. The model writes JSON tool calls into a chat history, the runtime executes them, the result feeds back into the context, repeat. That model has well-documented failure modes (Multi-Agent System failure Taxonomy, IBM Research + UC Berkeley, arXiv:2503.13657):

- **FM-1.5 Unaware of Termination Conditions**. The model loops past where it should stop, or stops before it has verified the work.
- **FM-2.6 Reasoning-Action Mismatch**. The model picks an action that does not match the intent it just stated.
- **FM-3.3 Incorrect Verification**. The model declares success without running a check that proves the work landed.

ansiburr addresses these structurally rather than behaviorally. The FSM is the substrate, not the chat history. The LLM is a labeled choice between typed transitions, not a free-running interpreter.

- Termination lives in the FSM. The model picks one label from a fixed allow-list, the FSM transitions on the label, and the model cannot pick an action that is not reachable from the current state, cannot loop forever, and cannot skip a verification step the FSM places between remediation and success.
- A deterministic validator action sits between the model's pick and the consumption. It checks the label against the allow-list. Off-script picks route to a recovery branch instead of being treated as valid.
- Verification is its own FSM node with its own pre-condition. The remediation must have run, and the only transition out of remediation passes through verify.
- The trace shows which label the model picked, whether the validator accepted it, which Ansible modules ran, what each module returned, which transition fired next, and when the FSM declared the work done. None of that is model self-report.

`examples/mast_sre_agent/` runs this end-to-end against a Granite model via Ollama. The block / rescue / always lowering aligns with STRATUS's Transactional No-Regression formalism: a failure inside a block routes to its rescue chain, a successful rescue clears the latched failure, and an always chain runs in both cases.

For workflows without a model in the loop, the same primitives still buy observability and composability. Plain IT automation gets per-step traces, persistence, and free composition between Ansible modules and Python `@action` functions through Burr's standard tooling.

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

If you already have an Ansible playbook, `from_playbook(...)` lifts it into a runnable Burr `Application` without rewriting the YAML. The full demo lives in [`examples/from_playbook/`](./examples/from_playbook/).

![ansiburr converting a multi-feature Ansible playbook into a Burr FSM and walking it step by step](https://raw.githubusercontent.com/msradam/ansiburr/main/vhs/conversion.gif)

The recording above is the converter walking a single Ansible playbook (`set_fact`, `block`, `loop`, `notify`/`handlers`, `changed_when`) lifted into a Burr `Application`. Every Ansible task is a discrete observable Burr step, and so is every loop iteration, every notify marker, and every handler.

For a sense of how much of the wild ansiburr ingests: **all six** of the most-downloaded `geerlingguy` roles (the de facto Galaxy benchmark) convert directly from their unmodified published source.

| Galaxy role | Converted graph |
|---|---|
| `geerlingguy.ansible-role-docker` | 63 actions, 205 transitions |
| `geerlingguy.ansible-role-mysql` | 78 actions, 265 transitions |
| `geerlingguy.ansible-role-nginx` | 40 actions, 153 transitions |
| `geerlingguy.ansible-role-postgresql` | 63 actions, 186 transitions |
| `geerlingguy.ansible-role-redis` | 13 actions, 27 transitions |
| `geerlingguy.ansible-role-php` | 101 actions, 312 transitions |

Here's the smaller demo playbook from `examples/from_playbook/`:

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

`examples/` contains twelve self-contained FSMs plus three playbook-conversion demos. Most of the FSMs run against a local Docker container set up by `examples/service_remediation/setup.sh`; the conversion demos and a few others run locally.

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
| `from_playbook_role` | Multi-file role-style playbook with `ansible.builtin.include_tasks` dispatch to `tasks/setup-<distro>.yml`, mirroring the geerlingguy role shape | `ansible.builtin` |

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
