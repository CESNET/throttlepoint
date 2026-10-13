# Plugins: the score / action pipeline

Every evaluation is a pipeline of four stages. Each step in a stage is a
plugin, so what gets measured, how it becomes a score, and what is done about
it are separate choices in `config.ini`.

```
read ──▶ score ──▶ act ──▶ record
 data     rating    caps     DB + state.json
          1–10
```

| Stage | Job | Built-ins |
|---|---|---|
| `read` | Gather raw measurements | `power`, `cpufreq` |
| `score` | Turn data into a `rating` from 1 (run fast) to 10 (throttle most) | `price`, `co2`, `combine` |
| `act` | Do something with the rating | `cpu_freq`, `cpu_thermo`, `gpu_power` |
| `record` | Persist the evaluation | `db_log`, `state_json` |

Within a stage, plugins run in the order listed.

## Choosing the pipeline

**No `[PIPELINE]` section**: the plan follows `[MODE] control_mode` and behaves
exactly as before (`co2` = price + CO₂ rated and combined, then `cpu_freq`;
`temperature` = `cpu_thermo` + `gpu_power`).

**With `[PIPELINE]`**: it replaces the mode presets. `control_mode` may then be
omitted. A stage you leave out is empty, except `record`, which defaults to
`db_log, state_json`.

```ini
# Wattnet (or Nowtricity) CO2 alone drives the CPU cap. No price fetch.
[PIPELINE]
score = co2
act   = cpu_freq
```

On its own, `co2` makes its grade the `rating`. Add `combine` only when `price`
is listed too, to choose how the two mix (`[aggregation]`). To pick the data
source, set `[CO2_API] TYPE = wattnet`.

An invalid pipeline (unknown key, unknown plugin, a plugin listed under the
wrong stage, a bad executable) aborts the run before anything starts, with
`Config error: ...`. No plugin runs and no setting is changed.

## Your own plugin

A plugin is any executable. Declare it in a `[plugin:NAME]` section and list
`NAME` in the stage where it belongs:

```ini
[PIPELINE]
score = co2, threshold_score
act   = cpu_freq, echo_action

[plugin:threshold_score]
exec    = /etc/hpc_eff/plugins/threshold_score.py
timeout = 30          # seconds, default 30
low     = 150         # any other key is passed to the plugin as-is
high    = 450

[plugin:echo_action]
exec = /etc/hpc_eff/plugins/echo_action.py
```

A `[plugin:NAME]` with `exec` also **replaces a built-in of the same name**, so
`[plugin:co2] exec = ...` swaps the data source without touching the rest of
the pipeline. `exec = builtin:NAME` selects a built-in explicitly.

Working examples are in [`examples/plugins/`](../examples/plugins), installed
with the package under `/usr/share/doc/hpc-eff/examples/` (RPM: `/usr/share/doc/hpc_eff/examples/`):
`template.py` (a blank starter), `threshold_score.py` (score from absolute
gCO₂/kWh thresholds) and `echo_action.py` (an action that changes nothing).

### Installing a plugin, step by step

1. **Put the executable in a root-owned directory.** The file and its
   directory must both be owned by root and not writable by group or others,
   or the run aborts (see [Security](#security)).

   ```bash
   sudo mkdir -p /etc/hpc_eff/plugins
   sudo cp my_score.py /etc/hpc_eff/plugins/
   sudo chown -R root:root /etc/hpc_eff/plugins
   sudo chmod 755 /etc/hpc_eff/plugins /etc/hpc_eff/plugins/my_score.py
   ```

   The first line of the script must be a shebang such as `#!/usr/bin/env python3`.

2. **Declare it in `/etc/hpc_eff/config.ini`** (the same file as `[PIPELINE]`;
   back it up first). The name after `plugin:` is the name you list in the
   stage:

   ```ini
   [PIPELINE]
   score = co2, my_score
   act   = cpu_freq

   [plugin:my_score]
   exec = /etc/hpc_eff/plugins/my_score.py
   ```

3. **Test it by hand** before the next cron run. A plugin is a plain program,
   so you can feed it a request yourself:

   ```bash
   echo '{"version":1,"stage":"score","plugin":"my_score","config":{},"context":{"co2_current":300},"state":{}}' \
     | sudo /etc/hpc_eff/plugins/my_score.py
   ```

   It should print one JSON object. To try the whole pipeline without
   touching the CPU, temporarily set `act = echo_action` (it only reports).

4. **Run once and look at the result.** With `DEBUG=yes` in `[SYSTEM]`, run
   `sudo hpc-eff`, then read `state_json_path` (default
   `/var/lib/hpc_eff/state.json`): your `summary` appears under `current`, and
   any failure is listed under `plugin_errors`.

An error such as `Config error: ...` means the pipeline did not start and
nothing was changed. Typical causes: the plugin name has no `[plugin:NAME]`
section, it is listed under the wrong stage, or the file or its directory is
not root-owned.

### The contract (version 1)

The controller starts the executable, writes **one JSON request** to its stdin
and reads **one JSON response** from its stdout.

Request:

```json
{
  "version": 1,
  "stage": "score",
  "plugin": "threshold_score",
  "config": { "low": "150", "high": "450" },
  "context": { "co2_current": 300, "rating": 5, "...": "everything produced so far" },
  "state": { "timestamp": "2026-10-07T12:00:00Z" }
}
```

Response (every field optional):

```json
{
  "version": 1,
  "set":   { "rating": 6 },
  "state": { "summary": "CO2 300 g/kWh -> rating 6" }
}
```

| Field | Meaning |
|---|---|
| `set` | Merged into the context. Later plugins see it, and `db_log` stores the known columns (`rating`, `co2_current`, `freq_max`, ...). |
| `state` | Merged into this run's `state.json` entry. Any key is allowed. Strings under `summary` from several plugins are joined with `; `. |
| `error` | Non-empty string: report a failure. The run continues. |
| `version` | Contract version the plugin speaks. Omit it or send `1`. A higher number than the controller knows is rejected. |

`rating`, when set, must be a whole number from 1 to 10. Anything else
rejects that plugin's whole response.

### Failure handling

A plugin never aborts the evaluation. Each of these is logged, listed under
`plugin_errors` in the `state.json` entry, and the run goes on with the values
it already has:

- a non-zero exit code (the last 200 characters of stderr are kept),
- no response within `timeout`,
- output that is not a JSON object,
- an invalid `rating` or unsupported `version`.

If no plugin scores, `rating` stays at the neutral default of 5 (the same
fallback the built-ins use).

### Security

The controller runs as root from cron, so executables are checked **when the
pipeline is built**, and a failing check aborts the run:

- the path is absolute and the file is executable,
- the file **and its directory** are owned by root (or the user running
  `hpc-eff`) and are not writable by group or others,
- it is started without a shell, with working directory `/` and a minimal
  environment (`PATH`, `LANG` only; your shell environment is not inherited).

Put plugins in a root-owned directory such as `/etc/hpc_eff/plugins/`
(`chmod 755`). Configuration values, including anything secret in a
`[plugin:NAME]` section, reach the plugin on stdin, never on the command
line. Keep `config.ini` readable by root only if it holds credentials.

### Several actions, one knob each

A stage takes a comma-separated list and runs it in the order written, so
`act` can hold more than one plugin:

```ini
act = cpu_freq, echo_action
```

Here `cpu_freq` caps the CPU first, then `echo_action` reports. The rule is
**one action per resource**: if two actions set the same thing, the later one
overwrites the earlier one.

| Combination | Result |
|---|---|
| `cpu_freq, echo_action` | fine: `echo_action` changes nothing |
| `cpu_freq, gpu_power` | fine: they change different hardware (CPU and GPU) |
| `cpu_freq, cpu_thermo` | conflict: both set the CPU max frequency, the last one wins |

Whenever two actions report a `freq_max`, a debug line names both. The same
order rule applies to `score`: the last plugin that sets `rating` wins, which
is how a plugin listed after `combine` can override the built-in rating.

## Built-in plugins

| Name | Stage | Reads | Produces |
|---|---|---|---|
| `power` | read | `[SYSTEM] POWERREADINGCMD` | `power_w` |
| `cpufreq` | read | the running CPU frequency | `cpu_freq_current` |
| `price` | score | electricity price vs. a year of monthly averages | `price`, `rating_price` |
| `co2` | score | `[CO2_API]` (Nowtricity or Wattnet), graded against the last 24 h | `co2_current`, `co2_median`, `co2_grade`, `rating_co2` |
| `combine` | score | `rating_price`, `rating_co2`, `[aggregation]` | `rating` |
| `cpu_freq` | act | `rating`, `[frequency_tables]` | `selected_freq_khz`, `freq_max` |
| `cpu_thermo` | act | `[TEMPERATURE_SOURCE]`, `[CPU_THERMO]` | `temperature`, `freq_max` |
| `gpu_power` | act | `[TEMPERATURE_SOURCE]`, `[GPU_POWER]` | `gpu_*` fields |
| `db_log` | record | the whole context | one row in `hpc_eff_log` |
| `state_json` | record | this run's entry | `state.json` |

### What `act = cpu_freq` does

The `score` stage only produces a number, `rating`. Nothing on the machine
changes until an `act` plugin uses it, and `cpu_freq` is the built-in that
does:

1. It reads `rating` (1 to 10; 5 if no score plugin set one).
2. It looks up a maximum frequency for that rating in the list for your CPU
   model in `[frequency_tables]`. Rating 1 gets the highest entry, rating 10
   the lowest, and ratings in between are spread across the list.
3. It sets the CPU's **maximum** frequency to that value with
   `cpupower frequency-set`. The CPU can still clock down below the cap; it
   just cannot go above it.
4. It reports the chosen value as `selected_freq_khz` (stored as `freq_max`).

So a dirty or expensive hour (high rating) caps the CPU low, and a clean or
cheap hour (low rating) lets it run at full speed. Without any `act` plugin
the scores are still computed and logged, but the CPU is left alone. Setting
the cap needs root.

`[aggregation] rating_type` accepts `price`, `co2`, `average` and `max`.
`co2` uses the CO₂ rating alone and falls back to the price rating, then to 5,
when CO₂ data is missing.

## Limits to know about

- The database has a fixed set of columns. A custom plugin's extra `set`
  values are not stored in SQLite; put anything you want kept under `state`
  (it lands in `state.json`).
- The `gpu_power` built-in follows ambient temperature, not the rating. A
  GPU cap driven by the rating needs its own action plugin.
- The contract is version 1. New fields will only be added, never changed, so
  a plugin written against version 1 keeps working.
