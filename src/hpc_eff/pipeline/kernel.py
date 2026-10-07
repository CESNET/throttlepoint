"""Pipeline kernel: builds the plugin plan from config and runs it.

An evaluation runs four stages in order. Within a stage, plugins run in the
order they are listed:

  read    sources of raw measurements       (power, cpufreq, ...)
  score   turn data into a `rating` 1-10    (price, co2, combine, ...)
  act     do something with the rating      (cpu_freq, gpu_power, ...)
  record  persist the evaluation            (db_log, state_json, ...)

The kernel knows nothing about prices, CO2 or CPUs. Plugins talk to it through
one contract: a request dict in, a response dict out (see docs/plugins.md).
A plugin is either a built-in (in-process) or an executable (JSON over
stdin/stdout). A plugin that fails never aborts the evaluation: the failure is
logged and the run continues with the values it already has.
"""

import time
from dataclasses import dataclass, field

from .builtins import BUILTINS
from .exec_plugin import CONTRACT_VERSION, PluginError, check_executable, run_executable

STAGES = ("read", "score", "act", "record")
DEFAULT_TIMEOUT = 30.0
DEFAULT_RECORD = ["db_log", "state_json"]
_RESERVED_OPTIONS = ("exec", "timeout")


class PipelineError(Exception):
    """The pipeline configuration is invalid; the run is aborted before any plugin starts."""


@dataclass
class Plugin:
    name: str
    exec: str                 # "builtin:<name>" or an absolute path
    timeout: float = DEFAULT_TIMEOUT
    options: dict = field(default_factory=dict)


@dataclass
class Runtime:
    """What built-in plugins may use besides the request and the config."""
    conn: object
    debug_log: object
    static_context: dict


def _names(value):
    return [n.strip() for n in value.split(",") if n.strip()]


def default_plan(config):
    """The plan implied by [FEATURES] (set from [MODE] control_mode), when [PIPELINE] is absent."""
    temp_cpu = config.getboolean("FEATURES", "ENABLE_TEMP_CPU", fallback=True)
    price_cpu = config.getboolean("FEATURES", "ENABLE_PRICE_CO2_CPU", fallback=True)
    temp_gpu = config.getboolean("FEATURES", "ENABLE_TEMP_GPU", fallback=False)

    return {
        "read": ["power", "cpufreq"] if price_cpu else [],
        "score": ["price", "co2", "combine"] if price_cpu else [],
        "act": (["cpu_thermo"] if temp_cpu else []) + (["cpu_freq"] if price_cpu else [])
               + (["gpu_power"] if temp_gpu else []),
        "record": list(DEFAULT_RECORD),
    }


def explicit_plan(config):
    """The plan named by [PIPELINE]. `record` defaults to db_log, state_json."""
    section = config["PIPELINE"]
    unknown = [k for k in section if k not in STAGES]
    if unknown:
        raise PipelineError(
            f"[PIPELINE] unknown key(s) {unknown}; valid keys: {', '.join(STAGES)}"
        )
    plan = {stage: _names(section.get(stage, "")) for stage in STAGES}
    if "record" not in section:
        plan["record"] = list(DEFAULT_RECORD)
    return plan


def resolve(name, stage, config):
    """Turn a plugin name into a Plugin, validating it. Raises PipelineError."""
    options, target, timeout = {}, None, DEFAULT_TIMEOUT
    section = f"plugin:{name}"
    if config.has_section(section):
        options = dict(config.items(section, raw=True))
        target = options.get("exec")
        try:
            timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        except ValueError:
            raise PipelineError(f"[{section}] timeout must be a number")
    for key in _RESERVED_OPTIONS:
        options.pop(key, None)

    if not target:
        target = f"builtin:{name}"

    if target.startswith("builtin:"):
        builtin = target[len("builtin:"):]
        if builtin not in BUILTINS:
            raise PipelineError(
                f"unknown plugin {name!r}: no built-in {builtin!r} "
                f"(built-ins: {', '.join(sorted(BUILTINS))}) and no exec path in [{section}]"
            )
        if BUILTINS[builtin][0] != stage:
            raise PipelineError(
                f"plugin {name!r} is a '{BUILTINS[builtin][0]}' plugin, but is listed under '{stage}'"
            )
    else:
        try:
            check_executable(target)
        except ValueError as e:
            raise PipelineError(f"plugin {name!r}: {e}")

    return Plugin(name=name, exec=target, timeout=timeout, options=options)


def build_plan(config):
    """Return {stage: [Plugin, ...]} for this run. Raises PipelineError."""
    names = explicit_plan(config) if config.has_section("PIPELINE") else default_plan(config)
    return {stage: [resolve(n, stage, config) for n in names[stage]] for stage in STAGES}


def _valid_rating(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PluginError(f"rating must be a number from 1 to 10, got {value!r}")
    if value != int(value) or not 1 <= value <= 10:
        raise PluginError(f"rating must be a whole number from 1 to 10, got {value!r}")
    return int(value)


def _apply(response, ctx, entry):
    """Validate a plugin response and merge it into the context and state entry."""
    version = response.get("version", CONTRACT_VERSION)
    if not isinstance(version, int) or version > CONTRACT_VERSION:
        raise PluginError(f"unsupported contract version {version!r} (this controller speaks {CONTRACT_VERSION})")
    values = response.get("set", {})
    state = response.get("state", {})
    if not isinstance(values, dict) or not isinstance(state, dict):
        raise PluginError("'set' and 'state' must be JSON objects")
    if response.get("error"):
        raise PluginError(str(response["error"]))

    values = dict(values)
    if "rating" in values:
        values["rating"] = _valid_rating(values["rating"])

    ctx.update(values)
    for key, val in state.items():
        # `summary` notes from several plugins are joined, everything else overwrites
        if key == "summary" and isinstance(entry.get(key), str) and isinstance(val, str):
            entry[key] = f"{entry[key]}; {val}"
        else:
            entry[key] = val


def _call(plugin, request, config, runtime):
    if plugin.exec.startswith("builtin:"):
        return BUILTINS[plugin.exec[len("builtin:"):]][1](request, config, runtime) or {}
    return run_executable(plugin.exec, request, plugin.timeout)


def run_pipeline(conn, config, static_context, debug_log):
    """Run one evaluation. Raises PipelineError only for an invalid configuration."""
    plan = build_plan(config)
    names = {stage: [p.name for p in plan[stage]] for stage in STAGES}
    debug_log(f"Pipeline: {names}")

    static = dict(static_context, plugins=names)
    runtime = Runtime(conn=conn, debug_log=debug_log, static_context=static)

    ctx: dict = dict(static)
    ctx.update({"rating": 5, "temperature": None})    # neutral default until a plugin scores
    entry: dict = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    errors = []
    freq_setter = None

    for stage in STAGES:
        if stage == "record" and errors:
            entry["plugin_errors"] = errors
        for plugin in plan[stage]:
            request = {
                "version": CONTRACT_VERSION,
                "stage": stage,
                "plugin": plugin.name,
                "config": plugin.options,
                "context": ctx,
                "state": entry,
            }
            try:
                response = _call(plugin, request, config, runtime)
                _apply(response, ctx, entry)
                if stage == "act" and "freq_max" in response.get("set", {}):
                    if freq_setter:
                        debug_log(f"Pipeline: {plugin.name} overrides the CPU max frequency set by {freq_setter}")
                    freq_setter = plugin.name
            except Exception as e:
                message = f"{plugin.name}: {e}"
                errors.append(message)
                debug_log(f"Pipeline: plugin failed - {message}")
