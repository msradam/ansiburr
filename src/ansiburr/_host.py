"""``host()`` — a small connection profile that captures hostvars once and emits
configured ``@module_action`` decorators bound to that host.

Without this, every ``@module_action`` targeting the same remote repeats the
connection dict (``ansible_host``, ``ansible_port``, ``ansible_user``,
``ansible_ssh_private_key_file``, ``ansible_python_interpreter``, become flags).
With it, a single ``Host`` captures that once and every action is one line of
intent::

    target = ansiburr.host("oom-target", ansible_host="127.0.0.1", ...)

    @target.module("ansible.builtin.service")
    def restart_nginx(state):
        return {"name": "nginx", "state": "restarted"}

The Host also exposes ``.shell``, ``.command``, ``.copy``, ``.template``,
``.file``, ``.systemd``, ``.service``, ``.uri``, ``.slurp``, ``.find`` as
shorthands for the most common ``ansible.builtin`` modules.

``Host.gather_facts()`` runs ``ansible.builtin.setup`` and flattens its
``ansible_facts`` payload into top-level State keys — bringing Ansible's
fact/var model into Burr's State the way they were always conceptually
aligned (gather_facts is, structurally, just "observe the world and populate
state with what you found"). ``Host.initial_facts()`` seeds placeholder
values so transitions can read those keys before gather_facts has run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from burr.core import State, action

from ansiburr._action import (
    SENTINEL_KEYS,
    ModuleArgsBuilder,
    WrappedAction,
    module_action,
)
from ansiburr._runner import run_module

# Common Ansible facts a state-aware FSM is likely to branch on. Not exhaustive
# (the ``setup`` module returns 100+ keys depending on host), just the ones we
# expect transitions to reference. Users can extend via ``extra=`` on
# ``gather_facts()`` and ``initial_facts()``.
DEFAULT_FACT_KEYS: tuple[str, ...] = (
    "ansible_os_family",
    "ansible_distribution",
    "ansible_distribution_version",
    "ansible_distribution_major_version",
    "ansible_pkg_mgr",
    "ansible_service_mgr",
    "ansible_system",
    "ansible_kernel",
    "ansible_architecture",
    "ansible_processor_count",
    "ansible_processor_cores",
    "ansible_memtotal_mb",
    "ansible_hostname",
    "ansible_fqdn",
    "ansible_user_id",
    "ansible_python_version",
    "ansible_virtualization_type",
)


@dataclass(frozen=True)
class Host:
    """Connection profile bound to a single inventory host.

    All fields except ``name`` map to standard Ansible hostvars (the same dict
    you'd pass as ``connection=`` to ``@module_action``). ``become`` is hoisted
    to the dataclass because it's a per-action play-level setting, not a hostvar.
    """

    name: str
    ansible_host: str | None = None
    ansible_port: int | None = None
    ansible_user: str | None = None
    ansible_ssh_private_key_file: str | None = None
    ansible_ssh_common_args: str | None = None
    ansible_python_interpreter: str | None = None
    become: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    def _connection(self) -> dict[str, Any] | None:
        conn: dict[str, Any] = {
            k: getattr(self, k)
            for k in (
                "ansible_host",
                "ansible_port",
                "ansible_user",
                "ansible_ssh_private_key_file",
                "ansible_ssh_common_args",
                "ansible_python_interpreter",
            )
            if getattr(self, k) is not None
        }
        conn.update(self.extra)
        if not conn and self.name in ("localhost", "127.0.0.1"):
            return None
        return conn

    def module(
        self,
        module: str,
        *,
        reads: Sequence[str] = (),
        writes: Sequence[str] | Mapping[str, str] = (),
        register: str | None = None,
        become: bool | None = None,
        check_mode: bool = False,
        diff: bool = False,
    ) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        """``@module_action`` configured for this host.

        ``become`` defaults to the Host's ``become`` field but can be overridden
        per-action when a specific module needs root and the host is otherwise
        run unprivileged (or vice versa). ``check_mode`` + ``diff`` enable
        plan-before-apply: the module reports what it would change without
        making changes, and the structured diff lands at ``state['_last_diff']``.
        """
        return module_action(
            module,
            reads=reads,
            writes=writes,
            register=register,
            host=self.name,
            connection=self._connection(),
            become=self.become if become is None else become,
            check_mode=check_mode,
            diff=diff,
        )

    # ---- Shorthands for commonly-used ansible.builtin modules. -----------
    def service(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.service", **kw)

    def systemd(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.systemd", **kw)

    def shell(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.shell", **kw)

    def command(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.command", **kw)

    def copy(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.copy", **kw)

    def template(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.template", **kw)

    def file(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.file", **kw)

    def find(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.find", **kw)

    def slurp(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.slurp", **kw)

    def uri(self, **kw: Any) -> Callable[[ModuleArgsBuilder], WrappedAction]:
        return self.module("ansible.builtin.uri", **kw)

    # ---- gather_facts: state-expansion semantics -------------------------
    def gather_facts(
        self,
        *,
        extra: Sequence[str] = (),
        all_facts: bool = True,
    ) -> WrappedAction:
        """Run ``ansible.builtin.setup`` and flatten facts into top-level State keys.

        Conceptually equivalent to a playbook's ``gather_facts: yes`` step.
        The setup module returns ~100 keys under ``result['ansible_facts']``;
        this action lifts ``DEFAULT_FACT_KEYS + extra`` to top-level state so
        transitions can branch on them naturally (``expr("ansible_pkg_mgr == 'apt'")``).

        When ``all_facts`` is True (the default), the entire ``ansible_facts``
        dict also lands at ``state['facts']`` for inspection and any uncommon
        keys the caller wants to reach without declaring upfront.

        Use ``initial_facts(extra=...)`` to seed placeholder values in
        ``with_state(...)`` so reads can resolve before this action has run.
        """
        keys = list(DEFAULT_FACT_KEYS) + list(extra)
        writes_keys = ["facts", *keys, *SENTINEL_KEYS] if all_facts else [*keys, *SENTINEL_KEYS]
        connection = self._connection()
        host_name = self.name
        become = self.become

        @action(reads=[], writes=writes_keys)
        def _gather(state: State) -> tuple[dict[str, Any], State]:
            result = run_module(
                "ansible.builtin.setup",
                {},
                host=host_name,
                connection=connection,
                become=become,
            )
            facts: dict[str, Any] = result.get("ansible_facts", {}) or {}
            update: dict[str, Any] = {k: facts.get(k) for k in keys}
            if all_facts:
                update["facts"] = facts
            update["_last_action"] = "gather_facts"
            update["_last_failed"] = bool(result.get("failed"))
            update["_last_changed"] = bool(result.get("changed"))
            update["_last_unreachable"] = bool(result.get("unreachable"))
            update["_last_msg"] = str(result.get("msg") or "")
            return result, state.update(**update)

        return _gather

    def initial_facts(
        self,
        *,
        extra: Sequence[str] = (),
        all_facts: bool = True,
    ) -> dict[str, Any]:
        """Placeholder values for fact keys ``gather_facts()`` will populate.

        Use in ``with_state(**target.initial_facts(), other_state=...)`` so
        transitions referencing facts can resolve their reads before the
        gather_facts action has actually executed.
        """
        keys = list(DEFAULT_FACT_KEYS) + list(extra)
        seed: dict[str, Any] = dict.fromkeys(keys, "")
        if all_facts:
            seed["facts"] = {}
        return seed


def host(name: str, **kwargs: Any) -> Host:
    """Build a :class:`Host` connection profile. Accepts any field name from
    :class:`Host` as a keyword; any remaining keys land in ``extra`` so users
    can pass uncommon Ansible hostvars (vault, custom connection plugins, etc.).
    """
    known = {
        f.name
        for f in Host.__dataclass_fields__.values()  # type: ignore[attr-defined]
        if f.name != "extra"
    }
    explicit = {k: v for k, v in kwargs.items() if k in known}
    extra = {k: v for k, v in kwargs.items() if k not in known}
    return Host(name=name, **explicit, extra=extra)
