"""Thin wrapper around ansible-runner that invokes a single module and returns its result."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ansible_runner  # type: ignore[import-untyped]
import yaml

# Cross-process stable ControlPath so all ansiburr-issued SSH sessions to the
# same (host, port, user) tuple share one multiplexed connection. Without this
# every @module_action does a fresh handshake; with it, only the first action
# in a sequence pays handshake cost. Subsequent calls multiplex through the
# persistent master for ``ControlPersist`` seconds.
_ANSIBURR_CONTROL_DIR = Path.home() / ".ssh" / ".ansiburr-cm"
_DEFAULT_SSH_ARGS = (
    "-C "
    "-o ControlMaster=auto "
    "-o ControlPersist=120s "
    f"-o ControlPath={_ANSIBURR_CONTROL_DIR}/%h-%p-%r"
)

# Default envvars applied on every run_module call. ansible-runner forwards
# these to the ansible-playbook subprocess. Anything the caller wants to
# override they can do via ``ansible_ssh_common_args`` on the connection.
_DEFAULT_ENVVARS: dict[str, str] = {
    "ANSIBLE_HOST_KEY_CHECKING": "False",
    "ANSIBLE_PIPELINING": "True",  # ~30-50% fewer SSH round-trips per module
    "ANSIBLE_SSH_ARGS": _DEFAULT_SSH_ARGS,
}


def _ensure_control_dir() -> None:
    """``~/.ssh/.ansiburr-cm/`` needs to exist before OpenSSH writes a socket
    into it. Mode 0700 so other users can't hijack the multiplexed sessions."""
    _ANSIBURR_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_ANSIBURR_CONTROL_DIR, 0o700)


def run_module(
    module: str,
    args: dict[str, Any],
    *,
    host: str = "localhost",
    connection: Mapping[str, Any] | None = None,
    become: bool = False,
    check_mode: bool = False,
    diff: bool = False,
) -> dict[str, Any]:
    """Run an Ansible module against a host and return its result dict.

    ``connection`` is a dict of Ansible hostvars (``ansible_host``,
    ``ansible_port``, ``ansible_user``, ``ansible_ssh_private_key_file``,
    ``ansible_ssh_common_args``, etc.) written as host_vars/<host>.yml.
    When ``connection`` is None and ``host`` is localhost, ``ansible_connection=local``
    is set so no ssh round-trip is needed.

    ``check_mode=True`` makes the module report what it would change without
    making changes (equivalent to ``ansible-playbook --check``). Pair with
    ``diff=True`` to also capture structured before/after content under the
    result's ``diff`` key. Together they implement the plan-before-apply
    pattern.

    The result always includes Ansible's diagnostic fields on failure
    (``failed``, ``msg``, optionally ``unreachable``) so callers can branch
    on them via Burr transitions instead of catching exceptions.
    """
    with tempfile.TemporaryDirectory(prefix="ansiburr-") as tmp:
        tmp_path = Path(tmp)

        inv_dir = tmp_path / "inventory"
        inv_dir.mkdir()
        (inv_dir / "hosts").write_text(f"{host}\n")

        hostvars: dict[str, Any] = dict(connection) if connection else {}
        if connection is None and host in ("localhost", "127.0.0.1"):
            hostvars["ansible_connection"] = "local"
            # Default to the venv interpreter so controller-side collections
            # (community.docker, community.crypto, ...) see the same packages
            # our user installed via uv/pip. Otherwise Ansible's auto-discovery
            # picks the first python on PATH, which is usually the system Python
            # without our dependencies.
            hostvars.setdefault("ansible_python_interpreter", sys.executable)
        if hostvars:
            host_vars_dir = inv_dir / "host_vars"
            host_vars_dir.mkdir()
            (host_vars_dir / f"{host}.yml").write_text(yaml.safe_dump(hostvars))

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        task: dict[str, Any] = {"name": "ansiburr", module: args}
        if check_mode:
            task["check_mode"] = True
        if diff:
            task["diff"] = True
        play: list[dict[str, Any]] = [
            {
                "hosts": host,
                "gather_facts": False,
                "become": become,
                "tasks": [task],
            }
        ]
        (project_dir / "playbook.yml").write_text(yaml.safe_dump(play))

        _ensure_control_dir()
        runner_kwargs: dict[str, Any] = {
            "private_data_dir": str(tmp_path),
            "playbook": "playbook.yml",
            "quiet": True,
            "envvars": _DEFAULT_ENVVARS,
        }
        if diff:
            # ansible-runner forwards --diff via the cmdline flag rather than via
            # the play structure; the task-level ``diff: true`` covers the play
            # side but the runner also needs to pass --diff for the cli switch.
            runner_kwargs["cmdline"] = "--diff"
        runner = ansible_runner.run(**runner_kwargs)
        return _extract_result(runner)


def _extract_result(runner: Any) -> dict[str, Any]:
    last_result: dict[str, Any] = {}
    for event in runner.events:
        ev = event.get("event", "")
        if ev not in ("runner_on_ok", "runner_on_failed", "runner_on_unreachable"):
            continue
        res = event.get("event_data", {}).get("res", {})
        if not isinstance(res, dict):
            continue
        last_result = dict(res)
        if ev == "runner_on_failed":
            last_result.setdefault("failed", True)
        elif ev == "runner_on_unreachable":
            last_result.setdefault("unreachable", True)
            last_result.setdefault("failed", True)

    if not last_result:
        return {
            "failed": True,
            "msg": f"ansible-runner produced no module result; status={runner.status}",
        }
    return last_result
