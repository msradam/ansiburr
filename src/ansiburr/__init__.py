"""ansiburr: Ansible-module-backed Burr actions.

Ansible's vars, facts, and registered results form its state model. Burr's
``State`` is also a state model. ansiburr exposes the alignment: Ansible
modules become Burr actions, ``gather_facts`` becomes state expansion,
``register:`` becomes a state-key projection, ``when:`` becomes a transition
predicate.

Mapping from Ansible-playbook idioms to ansiburr / Burr idioms::

    Ansible playbook                  ansiburr / Burr
    ------------------------------    -------------------------------------------
    gather_facts: yes                 host.gather_facts() flattens ansible_facts
                                      into top-level State keys
    vars: foo: bar                    ApplicationBuilder().with_state(foo="bar")
    host_vars/<host>.yml              fields on host() (connection vars today;
                                      domain vars on roadmap)
    set_fact: foo: bar                @action def f(state): return state.update(foo="bar")
    register: result_name             @module_action(register="result_name") or
                                      target.shell(register="...") etc.
    when: condition                   transition predicate: expr("condition")
    failed_when: X                    guard transition on expr("_last_failed") plus
                                      conditions on result fields written via writes=
    changed_when: X                   guard on expr("_last_changed") plus computed
                                      result fields
    block / rescue / always           guarded transition sub-graph; ``rescue:`` is an
                                      edge guarded by expr("_last_failed")
    notify: handler                   transition guarded by expr("_last_changed")
                                      to the handler action
    loop: items                       FSM iteration via state counter and back-edge
                                      (see examples/user_provisioning/)
    block validate-then-apply         see examples/config_drift/ (render with
                                      backup, validate via nginx -t, reload-if-ok,
                                      snapshot-on-failure rollback)

Two rows above required new ansiburr primitives: ``gather_facts()`` for state
expansion and ``register=`` for full-result capture. The rest is supported by
Burr directly (``with_state``, ``@action``, ``expr``).
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
