# Configuration reference

Everything lives in a single INI file:

```
/etc/hpc_eff/config.ini
```

If that file does not exist, `hpc-eff` falls back to
`src/hpc_eff/config.ini.example` **relative to the current working
directory**, which is how you can run it straight from a git checkout, and
also why a run from an unexpected directory can appear to ignore your settings.

The whole file is parsed upfront, so a syntax error anywhere (even in a
section nothing reads) crashes the run.

Comments must be on their own line. `KEY=430   # note` makes the value
`430   # note`; for a number that is a config error.

The shipped `config.ini.example` regulates on Wattnet CO₂ against a
threshold, through `[PIPELINE]`. The `[MODE]` presets are the older way and
apply only when `[PIPELINE]` is absent. Which sections are read:

| Section | Shipped default (`[PIPELINE]`, CO₂ threshold) | `control_mode=co2` (price + CO₂) | `control_mode=temperature` |
|---|:--:|:--:|:--:|
| `[CO2_API]` | yes | yes | n/a |
| `[frequency_tables]` | yes | yes | n/a |
| `[PIPELINE]`, `[plugin:NAME]` | yes | n/a | n/a |
| `[logging]` | yes | yes | yes |
| `[SYSTEM]` | power reading only | yes | yes |
| `[MODE]` | n/a | yes | yes |
| `[aggregation]` | n/a | yes | n/a |
| `[TEMPERATURE_SOURCE]` | n/a | n/a | yes |
| `[CPU_THERMO]` | n/a | n/a | yes |
| `[GPU_POWER]` | n/a | n/a | yes |

---

## `[MODE]`

Selects a preset when there is no `[PIPELINE]` section (the shipped config has
one, so `[MODE]` is not needed there). Without `[PIPELINE]` it is
**required**: an unset or unrecognised value makes the run exit with
`Config error: [MODE] control_mode must be one of: temperature|co2` rather
than silently doing nothing. With `[PIPELINE]` present, `[MODE]` is ignored.

```ini
[MODE]
control_mode=co2
```

| Value | CPU regulator | GPU regulator |
|---|---|---|
| `co2` | price/CO₂ rating → max-frequency cap | off |
| `temperature` | hysteresis thermal control | NVIDIA power limiting, on |

CPU regulation is mutually exclusive by construction: one mode, one CPU
regulator.

---

## `[SYSTEM]`

```ini
[SYSTEM]
SCORENAME=SPEC2017
SCORE=3.8
POWERREADINGCMD=ipmitool dcmi power reading
DEBUG=yes
```

| Key | Default | Meaning |
|---|---|---|
| `SCORENAME` / `SCORE` | `unknown` / *(none)* | The node's benchmark name and score (e.g. its SPEC CPU2017 result), stamped onto every log row so you can later compare throttling impact against a node's rated performance. Set manually; never read back by the tool itself. |
| `POWERREADINGCMD` | *(empty)* | Command string (split on whitespace, no shell; no pipes/`$()`) whose output is parsed for power draw. |
| `DEBUG` | `no` | `yes` prints the full evaluation trace to stdout. |

`POWERREADINGCMD` output must contain the four `ipmitool dcmi power reading`
lines; only `Instantaneous power reading` is stored (as `power_w`), logged
only, never acted on.

`DEBUG=yes` is safe to leave on; cron discards stdout regardless.

---

## `[CO2_API]`: carbon intensity (Wattnet)

Wattnet is the only CO₂ backend. (Nowtricity was removed: a config that still
sets `TYPE=nowtricity` fails with a clear error in the `state.json` summary;
delete `TYPE` and the `NOWTRICITY_*` keys.)

### Wattnet

**Wattnet requires a login (email and password). There are no API keys.**

| Key | Default |
|---|---|
| `WATTNET_EMAIL` | none, **required** |
| `WATTNET_PASSWORD` | none, **required** |
| `WATTNET_TOKEN_CACHE` | `/var/lib/hpc_eff/wattnet_token.json` |
| `WATTNET_TOKEN_URL` | `https://api.wattnet.eu/token-request/get_token` |
| `WATTNET_URL` | `https://api.wattnet.eu/v1/footprints` |
| `WATTNET_ZONE` | `CZ` |
| `WATTNET_FOOTPRINT_TYPE` | `carbon` |
| `WATTNET_SCOPE` | `operational` |

```ini
[CO2_API]
WATTNET_EMAIL=throttlepoint@your-site.example
WATTNET_PASSWORD=<password>
```

#### Getting a login

1. Request API access at [wattnet.eu/contact](https://wattnet.eu/contact).
   Registration only works once your email is approved.
2. Register once with the token service at
   [https://api.wattnet.eu/token-request/](https://api.wattnet.eu/token-request/):
   ```bash
   curl -X POST https://api.wattnet.eu/token-request/register \
        -H "Content-Type: application/json" \
        -d '{"email": "<email>", "password": "<password>"}'
   ```
3. Put the same email and password in `[CO2_API]`.

Wattnet suggests a dedicated account for unattended services, not a personal
one. The email does not need to be a real mailbox.

#### How the token is handled

Wattnet tokens are valid for 24 hours and there are no refresh tokens. On the
first run ThrottlePoint logs in at `/token-request/get_token` and caches the
token in `WATTNET_TOKEN_CACHE` (mode 600). Later runs reuse it and log in again
about 10 minutes before it expires: one login per node per day, as Wattnet
asks. A wrong email or password fails the CO₂ fetch with
`Wattnet login rejected`.

The config file now holds a password: make it readable by root only
(`chmod 600 /etc/hpc_eff/config.ini`).

Full API documentation: [docs.wattnet.eu](https://docs.wattnet.eu/user-guide/api/api-access/).

#### Data

The 24-hour window is computed automatically. Wattnet splits the response into
several series by data state: final data ends about 4 hours ago, newer data is
pending or a preview and may still be revised slightly. All series are merged
(final data wins on overlap) so the newest point is about now. The 15-minute
samples are averaged per clock hour for the 24 h history. The "current" value
is the trailing 1 h average: the newest four 15-minute points. **If fewer than
24 hourly values come back the call fails**, and the CO₂ fallback below applies.

### Threshold: when to throttle

```ini
[CO2_API]
# gCO2/kWh, 1 h average
THRESHOLD=430
# gCO2/kWh
HYSTERESIS=5
```

Two levels only, the model of the GreenDIGIT threshold simulator:

| Current CO₂ (1 h average) | Rating | CPU max frequency |
|---|---|---|
| above `THRESHOLD` | 10 | **last** value of the CPU's row in `[frequency_tables]` |
| at or below `THRESHOLD` | 1 | **first** value (full speed) |

Once throttled, full speed returns only when the value drops below
`THRESHOLD - HYSTERESIS` (425 with the defaults), so a value hovering around
the threshold does not switch the cap on and off every run.

The value can be changed while running: cron re-reads `config.ini` on every
run, so a new `THRESHOLD` applies within 10 minutes. A value that is not a
finite number (`430g`, `nan`), a negative `HYSTERESIS` or `MAX_AGE_MIN`, or an
inline comment after the value is treated like missing data: rating 1 and
`CO2 config error ([CO2_API] KEY=...)` in the `state.json` summary.

The throttle carries over between runs only from a recent run (at most
`MAX_AGE_MIN` minutes old). After cron was off for longer, the next run starts
fresh.

`state.json` records `co2_threshold`, `co2_throttled` and a summary such as
`CO2 437.2 g/kWh, threshold 430 (release below 425): throttled (rating 10)`.

**How often it throttles** depends on the threshold. On the CZ winter data of
the GreenDIGIT simulator (Nov 2025 – Feb 2026, 1 h average, hysteresis 5):

| `THRESHOLD` | Throttled time | Days with throttling (of 120) |
|---|---|---|
| 430 | 13.5 % | 31 (all in Jan–Feb) |
| 440 | 4.9 % | 15 |
| 450 | 2.3 % | 8 |
| 460 | 0.9 % | 3 |

Without `THRESHOLD` the rating is the old 24 h percentile grade (1–10): the
current hour ranked against the last 24 hours. That throttles every day, even
on a clean day, because every day has a dirtiest hour.

### When the CO₂ fetch fails

The last real result is reused if it is at most `MAX_AGE_MIN` minutes old
(default 120, i.e. one missed hourly value plus slack): the throttle decision
with `THRESHOLD`, the grade without it. Older or none: rating 1, no slowdown
without evidence. A reused result is never reused again, so an outage cannot
extend it. Each failure logs a WARNING and a `CO2 unavailable (...)` summary in
`state.json`.

| Key | Default |
|---|---|
| `THRESHOLD` | unset (24 h ranking); shipped config: `430` |
| `HYSTERESIS` | `5` |
| `MAX_AGE_MIN` | `120` |

---

## `[frequency_tables]`: rating → frequency map

```ini
[frequency_tables]
amd_epyc=2400,2000,1500
intel_xeon_gold=3000,2900,2800,2700,2600,2200
intel_e5=3000,2800,2700,2600,2400,2300
default=3000,2800,2700,2600,2400,2200
```

Values are **MHz, highest first**. The key is chosen by matching
`/proc/cpuinfo`:

| Key | Matches when the model name contains |
|---|---|
| `amd_epyc` | `EPYC` |
| `intel_e5` | `E5-` and `V3` |
| `intel_xeon_gold` | `GOLD` |
| `default` | everything else |

Selection is `index = round((rating - 1) / 9 × (len - 1))` over the
descending-sorted list: rating 1 takes the first entry, rating 10 the last,
the rest are spread evenly. With `THRESHOLD` (the shipped default) only
ratings 1 and 10 occur, so only the first and the last entry matter: full
speed and throttled. The list length is your granularity; see
[deployment.md](deployment.md#co2-mode-the-frequency-table).

If the section or the key is missing entirely the built-in fallback
`3000,2800,2700,2600,2400,2200` applies, which may be nonsense on your CPU.
Set `default` explicitly.

---

## `[aggregation]`: combining price and CO₂

**Not used by the shipped config.** Only for the price + CO₂ blend:
`control_mode=co2` without `[PIPELINE]`, or `score = price, co2, combine`.

```ini
[aggregation]
rating_type=average
weight_price=0.6
weight_co2=0.4
```

| Key | Default | Meaning |
|---|---|---|
| `rating_type` | `price` | `price` \| `co2` \| `average` \| `max`: see [regulation-modes.md](regulation-modes.md#choosing-rating_type) |
| `weight_price` | `0.6` | Used by `average` only, normalised against `weight_co2` |
| `weight_co2` | `0.4` | Used by `average` only |

An unknown `rating_type` logs a warning and falls back to the price rating.

Fallback chain when a signal is missing: CO₂ unavailable → price rating; price
unavailable → CO₂ rating; both unavailable → **neutral 5**. The node keeps
regulating rather than freezing at its last cap.

Rationale for the default weights: [regulation-modes.md](regulation-modes.md#why-price-is-weighted-higher-than-carbon).

---

## `[logging]`

```ini
[logging]
db_path=/var/lib/hpc_eff/history.db
state_json_path=/var/lib/hpc_eff/state.json
history_length=1000
log_level=INFO
```

| Key | Default | Meaning |
|---|---|---|
| `db_path` | `history.db` | SQLite file. Parent directory is created if needed; WAL mode is enabled. |
| `state_json_path` | `/var/lib/hpc_eff/state.json` | JSON state file, rewritten each run, chmod 644. |
| `history_length` | `10` | How many past entries `state.json` keeps. |
| `log_level` | `INFO` | Python level for the `hpc_eff` logger (stderr). |

`history_length` only bounds the JSON file, not the database; see
[monitoring.md](monitoring.md#the-database) for growth/pruning.

`log_level` affects stderr only, which cron discards.

---

## `[TEMPERATURE_SOURCE]`: pluggable temperature reading

Used only in `temperature` mode. The CPU thermal regulator and the GPU power
regulator each call this independently, once per run; they don't share a
single reading, though for a stable source both calls return the same value
in practice.

```ini
[TEMPERATURE_SOURCE]
TYPE=ipmi
IPMI_SENSOR_NAME=INLET_AIR_TEMP
```

| `TYPE` | Keys | Behaviour |
|---|---|---|
| `ipmi` | `IPMI_SENSOR_NAME` | Runs `ipmitool sensor reading "<name>"` and takes the first number in the output. |
| `http_api` | `HTTP_URL`, `HTTP_JSON_PATH` *(optional)* | GETs the URL (5 s timeout). Tries a bare number, then JSON, then any number in the body. |
| `custom` | `MODULE`, `FUNCTION` *(default `read`)* | Loads your code and calls it. |

HTTP example, for `{"temperature": {"value": 25.5}}`:

```ini
TYPE=http_api
HTTP_URL=http://192.168.1.100/api/greendigit/data
HTTP_JSON_PATH=temperature.value
```

Without `HTTP_JSON_PATH` the reader tries the top-level keys `temperature`,
`temp`, `value`, `data` before falling back to a regex for the first number in
the response.

Custom reader: `MODULE` is a path to a `.py` file or an importable dotted
module name; `FUNCTION` (default `read`) must return Celsius as a number, or
`None`:

```ini
TYPE=custom
MODULE=/opt/mysensors/my_reader.py
FUNCTION=read
```

Every source swallows its own exceptions and returns `None` on failure, which
makes the whole thermal regulation a no-op for that cycle. A typo in a sensor
name therefore looks like "nothing happens", not like an error; check with
`ipmitool sensor reading "<name>"` by hand.

---

## `[CPU_THERMO]`: thermal CPU control

```ini
[CPU_THERMO]
MID_LIMIT=29
HIGH_LIMIT=32
HIGH_FREQUENCY=3.10GHz
MID_FREQUENCY=2.30GHz
LOW_FREQUENCY=1.50GHz
#SLACK_URL=
```

| Key | Meaning |
|---|---|
| `MID_LIMIT` / `HIGH_LIMIT` | Band edges in °C |
| `HIGH_/MID_/LOW_FREQUENCY` | Target max frequency per band. Accepts `3.10GHz`, `2300MHz`, `2300000KHz`, or a bare number (MHz below 10000, else kHz). |
| `SLACK_URL` | Optional incoming webhook, posted to only when the frequency actually changes. |

The example limits (29/32 °C) are calibrated for the example sensor shipped
alongside them, not a recommendation for any other sensor; see
[deployment.md](deployment.md#temperature-mode-the-thermal-bands) for why
that matters and how to pick your own.

If any of the five keys is missing the regulator logs a debug line and skips,
silently, as far as cron is concerned.

Band logic and hysteresis: [regulation-modes.md](regulation-modes.md#temperature-mode).

---

## `[GPU_POWER]`: NVIDIA power limiting

Active in `temperature` mode only, and a no-op on nodes without NVIDIA GPUs.

```ini
[GPU_POWER]
MID_LIMIT=70
HIGH_LIMIT=80
HIGH_POWER=100%
MID_POWER=70%
LOW_POWER=40%
# SLACK_URL=
```

| Key | Default | Meaning |
|---|---|---|
| `MID_LIMIT` | `70` | °C, start reducing power above this |
| `HIGH_LIMIT` | `80` | °C, aggressive reduction above this |
| `HIGH_/MID_/LOW_POWER` | `100%` / `70%` / `40%` | Absolute watts (`250`) or a percentage of the GPU's max limit (`70%`) |

Percentages resolve against `nvidia-smi --query-gpu=power.max_limit` read from
the **first GPU only** (the code assumes all GPUs in the node are the same
model), falling back to that GPU's current limit, and finally to 300 W. All
GPUs then get the same absolute limit, derived from the one ambient
temperature: this is not per-GPU regulation, and it does not read GPU die
temperature.

The temperature comes from `[TEMPERATURE_SOURCE]`, the same reading the CPU
thermal regulator uses. The two sections' defaults are on visibly different
scales (`[CPU_THERMO]` at 29/32 °C, `[GPU_POWER]` at 70/80 °C), so they cannot
both be right for one sensor. Depending on the range your sensor actually
produces, one section's thresholds may never trigger while the other's fire
constantly. Set both pairs against that range.

---

## `[PIPELINE]` and `[plugin:NAME]`: choosing and extending the steps

Optional. Replaces the `[MODE] control_mode` presets with an explicit list of
plugins per stage.

```ini
[PIPELINE]
score = co2
act   = cpu_freq

# only for your own executables
[plugin:my_score]
exec    = /etc/hpc_eff/plugins/my_score.py
timeout = 30
```

| Key | Default | Meaning |
|---|---|---|
| `read`, `score`, `act` | empty | Comma-separated plugin names, run in order |
| `record` | `db_log, state_json` | Same, for the persistence stage |
| `[plugin:NAME] exec` | built-in `NAME` | Absolute path to an executable, or `builtin:NAME` |
| `[plugin:NAME] timeout` | `30` | Seconds before the plugin is killed |
| other `[plugin:NAME]` keys | none | Passed to the plugin as-is |

An invalid pipeline aborts the run with `Config error: ...` before any plugin
starts. Built-in plugin names, the JSON contract and the security rules:
[plugins.md](plugins.md).

---

## Next

[regulation-modes.md](regulation-modes.md): what each mode does with these
keys, the rating maths, the thermal bands.
