"""Executable transport: run a plugin as a separate process, JSON in / JSON out.

The plugin receives one JSON request on stdin and must print one JSON
response on stdout (see docs/plugins.md). A non-zero exit, a timeout or
unparsable output is a plugin failure; the kernel isolates it.

The controller runs as root from cron, so before a plugin is ever started its
file and directory are checked: absolute path, owned by root (or by the user
running the controller), and not writable by group or others.
"""

import json
import os
import stat
import subprocess

CONTRACT_VERSION = 1

# Plugins never inherit the controller's environment (which may hold secrets).
_SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


class PluginError(Exception):
    """A plugin failed at run time (exit code, timeout, bad output)."""


def _check_owner_and_mode(path, what):
    st = os.stat(path)
    if st.st_uid not in (0, os.geteuid()):
        raise ValueError(f"{what} {path} must be owned by root (or the user running hpc-eff)")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{what} {path} must not be writable by group or others")


def check_executable(path):
    """Raise ValueError unless `path` is a safe plugin executable."""
    if not os.path.isabs(path):
        raise ValueError(f"plugin path must be absolute: {path!r}")
    if not os.path.isfile(path):
        raise ValueError(f"plugin not found: {path}")
    if not os.access(path, os.X_OK):
        raise ValueError(f"plugin is not executable: {path}")
    _check_owner_and_mode(path, "plugin")
    _check_owner_and_mode(os.path.dirname(path), "plugin directory")


def run_executable(path, request, timeout):
    """Run the plugin and return its parsed JSON response (a dict)."""
    try:
        proc = subprocess.run(
            [path],
            input=json.dumps(request, default=str),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_SAFE_ENV,
            cwd="/",
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise PluginError(f"timed out after {timeout}s")
    except OSError as e:
        raise PluginError(f"could not start: {e}")

    if proc.returncode != 0:
        tail = proc.stderr.strip()[-200:]
        raise PluginError(f"exit code {proc.returncode}" + (f": {tail}" if tail else ""))

    try:
        response = json.loads(proc.stdout)
    except ValueError as e:
        raise PluginError(f"stdout is not valid JSON: {e}")
    if not isinstance(response, dict):
        raise PluginError("stdout JSON must be an object")
    return response
