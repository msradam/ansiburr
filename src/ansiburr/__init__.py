"""ansiburr: Ansible-module-backed Burr actions.

The core observation behind this library: **Ansible's vars/facts/registered
universe is its state model**, and it's structurally compatible with Burr's
``State``. ansiburr is the adapter that makes the two the same thing — host
observations land in State, transitions branch on them, and the Burr tracker
captures the full module results for free.

Mapping from Ansible-playbook idioms to ansiburr / Burr idioms::

    Ansible playbook                  ansiburr / Burr
    ------------------------------    -------------------------------------------
    gather_facts: yes                 host.gather_facts() — flattens ansible_facts
                                      into top-level State keys
    vars: foo: bar                    ApplicationBuilder().with_state(foo="bar")
    host_vars/<host>.yml              fields on host() (connection vars today;
                                      domain vars on roadmap)
    set_fact: foo: bar                @action def f(state): return state.update(foo="bar")
    register: result_name             @module_action(register="result_name") /
                                      target.shell(register="...") etc.
    when: condition                   transition predicate: expr("condition")
    failed_when: X                    guard transition on expr("_last_failed") +
                                      conditions on result fields written via writes=
    changed_when: X                   same — guard on expr("_last_changed") + computed
                                      result fields
    block / rescue / always           guarded transition sub-graph; ``rescue:`` =
                                      edge guarded by expr("_last_failed")
    notify: handler                   transition guarded by expr("_last_changed")
                                      to the handler action
    loop: items                       FSM iteration via state counter + back-edge
                                      (see examples/user_provisioning/)
    block validate-then-apply         see examples/config_drift/ — render with
                                      backup, validate (nginx -t), then reload-if-ok
                                      with snapshot-on-failure rollback

Most rows above are not new primitives — they're idioms Burr already supports
(``with_state``, ``@action``, ``expr``). The genuinely new ones are
``gather_facts()`` (state expansion) and ``register=`` (full-result capture).
The rest of the value is "Ansible and Burr agree on what state is; ansiburr
just exposes it that way."
"""

from ansiburr._action import (
    SENTINEL_KEYS,
    initial_sentinels,
    module_action,
    snapshot_sentinels,
)
from ansiburr._host import DEFAULT_FACT_KEYS, Host, host
from ansiburr._runner import run_module
from ansiburr._wait import WaitGraph, wait_until

__all__ = [
    "DEFAULT_FACT_KEYS",
    "SENTINEL_KEYS",
    "Host",
    "WaitGraph",
    "host",
    "initial_sentinels",
    "module_action",
    "run_module",
    "snapshot_sentinels",
    "wait_until",
]
