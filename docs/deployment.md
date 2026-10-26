# Deploy a node

Configuring, running, verifying, and tuning one node. Build and install the
package first with [install.md](install.md); when this node is working,
[cluster-rollout.md](cluster-rollout.md) covers the rest of the fleet.

A compute node caps its CPU maximum frequency from one signal:

| Setup | Description | Use case |
|-------|-------------|----------|
| **CO₂ threshold** (shipped config, `[PIPELINE]`) | Throttle while Wattnet CO₂ is above `THRESHOLD` | Carbon: slow down only on dirty electricity |
| `control_mode=co2` (no `[PIPELINE]`) | Price + CO₂ rating 1–10 | Cost and carbon, throttles daily |
| `control_mode=temperature` (no `[PIPELINE]`) | Temperature regulator + NVIDIA GPU power | Thermal management, datacenter continuity |

The CPU is driven by one of them, never two. GPU power regulation rides along
with `temperature` mode only (NVIDIA GPUs only; safely no-ops on nodes
without them).

Neither mode is a fallback for the other. Pick per node according to what
actually constrains it. What each mode does with its input is explained in
[regulation-modes.md](regulation-modes.md).

---

## 1. Configure

Edit `/etc/hpc_eff/config.ini`. If it is missing for any reason:

```bash
sudo install -D -m 640 /etc/hpc_eff/config.ini.example /etc/hpc_eff/config.ini
```

The shipped file is the CO₂ threshold setup below. `[MODE] control_mode` is
needed only if you remove `[PIPELINE]` to use one of the older presets; then
an unset or unknown value aborts the run.

`[SYSTEM] POWERREADINGCMD` (`ipmitool dcmi power reading`) is logging-only.
Leave it as is if you have no BMC; the failure is caught.

### CO₂ threshold (shipped default)

What to set:

| Section / key | Set it to | Notes |
|---|---|---|
| `[CO2_API] WATTNET_EMAIL`, `WATTNET_PASSWORD` | your Wattnet login | The only required edit. Wattnet has no API keys; register at [api.wattnet.eu/token-request](https://api.wattnet.eu/token-request/), see [configuration.md](configuration.md#wattnet). |
| `[frequency_tables]` | frequencies for your CPU, MHz, highest first | See [the frequency table](#co2-mode-the-frequency-table). |
| `[CO2_API] THRESHOLD`, `HYSTERESIS` | gCO₂/kWh | Shipped `430` and `5`. See below. |

Minimum working config (CO₂ only, the shipped default):

```ini
[CO2_API]
WATTNET_EMAIL=<your Wattnet login>
WATTNET_PASSWORD=<password>
THRESHOLD=430
HYSTERESIS=5

[frequency_tables]
amd_epyc=2400,2000,1500
default=3000,2800,2700,2600,2400,2200

[PIPELINE]
read  = power, cpufreq
score = co2
act   = cpu_freq
```

> **Caution:** the config file holds the Wattnet password and the package installs it
> mode 644. Tighten it with `sudo chmod 640 /etc/hpc_eff/config.ini`. Nothing
> needs it world-readable; it only ever runs as root.

#### Choosing the threshold

A higher `THRESHOLD` throttles less often. On the CZ data of the GreenDIGIT
simulator (Nov 2025 – Feb 2026):

| `THRESHOLD` | Throttled time | Days with throttling (of 120) |
|---|---|---|
| 430 | 13.5 % | 31 (all in Jan–Feb) |
| 440 | 4.9 % | 15 |
| 450 | 2.3 % | 8 |

The value can be changed on a running node: the next cron run (within
10 minutes) uses it. Details: [configuration.md](configuration.md#threshold-when-to-throttle).

`[aggregation]` (price + CO₂ weights) is not used by this setup; see
[configuration.md](configuration.md) (`[aggregation]`) if you
blend price and CO₂.

### Mode: `temperature`

The two things to set beyond the mode itself:

| Section / key | Set it to | Notes |
|---|---|---|
| `[TEMPERATURE_SOURCE]` | `TYPE` = `ipmi`, `http_api`, or `custom`, plus its keys | The only input this mode has. See below. |
| `[CPU_THERMO]` | band limits (°C) and target frequencies for your CPU | See [the thermal bands](#temperature-mode-the-thermal-bands). |

`[GPU_POWER]` matters only on NVIDIA nodes; the section is safely ignored
elsewhere. No `[CO2_API]`, `[aggregation]`, or `[frequency_tables]` keys are
read in this mode.

Minimum working `temperature` config:

```ini
[SYSTEM]
SCORENAME=SPEC2017
SCORE=3.8
POWERREADINGCMD=ipmitool dcmi power reading
DEBUG=yes

[MODE]
control_mode=temperature

[TEMPERATURE_SOURCE]
TYPE=ipmi
IPMI_SENSOR_NAME=INLET_AIR_TEMP

[CPU_THERMO]
MID_LIMIT=29
HIGH_LIMIT=32
HIGH_FREQUENCY=3.10GHz
MID_FREQUENCY=2.30GHz
LOW_FREQUENCY=1.50GHz
#SLACK_URL=

[GPU_POWER]
MID_LIMIT=70
HIGH_LIMIT=80
HIGH_POWER=100%
MID_POWER=70%
LOW_POWER=40%

[logging]
db_path=/var/lib/hpc_eff/history.db
state_json_path=/var/lib/hpc_eff/state.json
history_length=1000
log_level=INFO
```

#### Picking a temperature source

The reading is pluggable ("bring your own reader"):

| `TYPE` | Keys | Notes |
|---|---|---|
| `ipmi` | `IPMI_SENSOR_NAME` | Runs `ipmitool sensor reading "<name>"` and takes the first number in the output. Confirm the exact sensor name with `ipmitool sensor list` first; it varies by vendor. |
| `http_api` | `HTTP_URL`, optional `HTTP_JSON_PATH` | For a room or facility sensor exposed over HTTP. `HTTP_JSON_PATH=temperature.value` pulls `25.5` out of `{"temperature": {"value": 25.5}}`; omit it if the endpoint returns a plain number. |
| `custom` | `MODULE`, optional `FUNCTION` | `MODULE` is a path to a `.py` file or an importable dotted name; `FUNCTION` (default `read`) must return Celsius as a number. |

> **Warning:** verify the source before enabling cron. Every reader swallows
> its own exceptions and returns nothing on failure, so the regulator applies
> **no frequency change at all** and the node keeps whatever cap it already
> had. A row is still written with `temperature` NULL, and `state.json` shows
> `Temp None C`. A wrong sensor name therefore looks exactly like a healthy
> quiet node. Unlike `co2` mode, there is no second signal to fall back on.

Every section is documented in [configuration.md](configuration.md).

---

## 2. First run

```bash
sudo hpc-eff
```

> **Warning:** there is no `--dry-run` flag; this applies the frequency cap
> immediately. Do it on one node first, ideally a drained one.

With `DEBUG=yes` you get the whole pipeline on stdout. With the shipped
config, at 437 g/kWh (above 430) on a Xeon Gold:

```
[DEBUG] Starting HPC efficiency evaluator...
[DEBUG] [PIPELINE] present and no control_mode: using the pipeline as configured
[DEBUG] Pipeline: {'read': ['power', 'cpufreq'], 'score': ['co2'], 'act': ['cpu_freq'], 'record': ['db_log', 'state_json']}
[DEBUG] Last 24 hours CO2 values (g CO2eq/kWh): [...]
[DEBUG] Current CO2 value (g CO2eq/kWh): 437.2
[DEBUG] Current rating: 10
2026-10-26 10:43:15 INFO     | Set max frequency: cpupower frequency-set -u 2200000
2026-10-26 10:43:15 INFO     | DB log entry inserted
```

The `state.json` summary says why:
`CO2 437.2 g/kWh, threshold 430 (release below 425): throttled (rating 10)`.

In `temperature` mode there are no price or CO₂ lines. You get the reading and
the band decision instead:

```
[DEBUG] Starting HPC efficiency evaluator...
[DEBUG] control_mode='temperature' applied -> FEATURES {...}
[DEBUG] cpu_thermo applied target 2.30GHz
2026-08-21 10:00:03 INFO     | DB log entry inserted
```

Only an actual band change logs `cpu_thermo applied target`. A steady node
logs `Temperature 27.0°C → target 3.10GHz already set.` instead, which is the
normal case, not a fault.

`Config error: [MODE] control_mode must be one of: …` means `[PIPELINE]` was
removed and no `control_mode` is set; see [step 1](#1-configure).

---

## 3. Verify

Three independent checks. Do all three.

**The cap actually reached the hardware:**

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq   # kHz
```

This must match the frequency the run applied. If the log says success but
sysfs is unchanged, the frequency tool is a no-op on this node; see
[troubleshooting.md](troubleshooting.md).

**The database got a row.** With the shipped config:

```bash
sudo sqlite3 /var/lib/hpc_eff/history.db \
  "SELECT timestamp, rating, co2_current, freq_max
   FROM hpc_eff_log ORDER BY id DESC LIMIT 5;"
```

`rating` is 10 (throttled) or 1 (full speed). `co2_current` being NULL means
the Wattnet call failed; the reason is in the `state.json` summary
(`CO2 unavailable (...)`). If it is NULL every time, the login or egress is
wrong.

In `temperature` mode the rating and price columns stay NULL (nothing
computes them), so look at the temperature columns instead:

```bash
sudo sqlite3 /var/lib/hpc_eff/history.db \
  "SELECT timestamp, temperature, freq_max, gpu_state, gpu_power_limit
   FROM hpc_eff_log ORDER BY id DESC LIMIT 5;"
```

`temperature` being NULL means the source could not be read. The reason is not
in the database (the `action_temp` note goes to `state.json` only), so check
there, or rerun by hand with `DEBUG=yes`.

**The state file is current:**

```bash
sudo cat /var/lib/hpc_eff/state.json | python3 -m json.tool | head -30
```

Field-by-field meaning: [monitoring.md](monitoring.md).

---

## 4. Tune the frequencies

This step determines your actual energy saving. The shipped values are
placeholders; both modes need the real frequencies for your specific CPU,
stored in different sections.

> **Note:** the Intel and AMD caveats below apply to **both** modes. Read
> [the frequency table](#co2-mode-the-frequency-table) even if you are
> deploying `temperature` mode, because that is where finding a trustworthy
> frequency figure is explained.

### `co2` mode: the frequency table

The CPU model is auto-detected from `/proc/cpuinfo` into one of four keys:

| Detected | Match rule |
|---|---|
| `amd_epyc` | model name contains `EPYC` |
| `intel_e5` | contains `E5-` and `V3` |
| `intel_xeon_gold` | contains `GOLD` |
| `default` | anything else |

Anything not in that list lands on `default`, so on a Xeon Platinum or a
Sapphire Rapids part, the `default` row is what you are tuning.

Building this table from sysfs alone is unreliable, and the two driver
families mislead you in different directions. Both were hit during CESNET's own
rollout across three worker-node generations:

```bash
cpupower frequency-info
```

**On AMD (`acpi-cpufreq` driver):** `scaling_available_frequencies` exists and
lists the real, discrete P-states, usually three, matching what
`cpupower frequency-set -u <TAB>` offers. Read it directly:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies
```

**On Intel (`intel_cpufreq`/`intel_pstate` drivers):** that sysfs file does not
exist. Intel exposes a continuous range instead of discrete steps, so there is
no list to read. Worse, the range `cpupower frequency-info` reports as
"hardware limits" (and `cpuinfo_max_freq`) **includes Turbo Boost headroom**. A
Xeon E5-2650v3 with a 2.3 GHz base clock reports a range going up to 3.0 GHz;
take that as your ceiling and the node spends most of its "capped" time still
boosting.

The reliable way to find the real base clock under load is `turbostat`'s
`TSC_MHz` column, which holds steady at the base frequency regardless of Turbo:

```bash
turbostat --quiet --show CPU,frequency sleep 1
```

Cross-check against the CPU model's datasheet base and turbo figures before
committing a table: `cpupower`'s own output cannot be trusted alone on Intel.
This is also why the CPU-model keys in `[frequency_tables]` are hand-curated
per model rather than derived automatically at runtime. An auto-detected table
built from `cpupower` output would silently include Turbo range on Intel
hardware.

Then write frequencies in **MHz, highest first**:

```ini
[frequency_tables]
amd_epyc=2400,2000,1500
```

The rating picks an index across the list: rating 1 → first entry, rating 10 →
last entry, everything else scaled evenly between. With the shipped
`THRESHOLD` only ratings 1 and 10 occur, so only the first entry (full speed)
and the last entry (throttled) matter. The number of entries is
your granularity: three entries means the node only ever has three clock
ceilings. Six to eight gives a smooth response; two gives an on/off switch.

The lowest entry is your worst-case performance floor. Pick it deliberately:
it is what the node runs at during the dirtiest, most expensive hour of the
year, for as long as that hour lasts.

### `temperature` mode: the thermal bands

`[CPU_THERMO]` takes three frequencies rather than a table, and they are
written as strings with a unit (`3.10GHz`), not bare MHz integers as in
`[frequency_tables]`:

```ini
[CPU_THERMO]
MID_LIMIT=29
HIGH_LIMIT=32
HIGH_FREQUENCY=3.10GHz
MID_FREQUENCY=2.30GHz
LOW_FREQUENCY=1.50GHz
```

| Reading | Applied |
|---|---|
| `≥ HIGH_LIMIT` | `LOW_FREQUENCY` |
| `≥ MID_LIMIT` | `MID_FREQUENCY` |
| below `MID_LIMIT` | `HIGH_FREQUENCY`, once 2 °C of headroom is there |

You can use the same `turbostat` and datasheet cross-check from
[the frequency table](#co2-mode-the-frequency-table) to pick the three values;
the Intel Turbo problem is identical here.

The two limits are the part that needs your own numbers, because a temperature
threshold only means something against a particular sensor.

The shipped example pairs `MID_LIMIT=29` / `HIGH_LIMIT=32` with
`IPMI_SENSOR_NAME=INLET_AIR_TEMP`. Different sensors read very different
ranges, so set these from what your sensor actually reports rather than
copying the example as-is.

```bash
# a quick sense of the range your sensor actually covers
ipmitool sensor reading "INLET_AIR_TEMP"
```

> **Caution:** `[GPU_POWER]` has its own `MID_LIMIT`/`HIGH_LIMIT`, defaulting
> to 70/80 °C, a different scale from `[CPU_THERMO]`'s 29/32. Both sections
> read the *same* sensor, so at most one pair of defaults can suit it. Set
> both pairs against the range your sensor actually produces.

Power values take either a percentage (`70%`) or absolute Watts (`200`).
A percentage resolves against `nvidia-smi --query-gpu=power.max_limit` read
from the **first GPU only** (the code assumes every GPU in the node is the
same model), and the resulting absolute value is then applied to all of them.
On a node with mixed GPU models, neither form adapts per card.

---

## 5. Enable the periodic run

```bash
sudo hpc-eff --enable                                # every 10 min
sudo hpc-eff --enable --cron-interval 5              # every 5 min (1–60)
sudo hpc-eff --enable --cron-path /custom/path       # non-default location
sudo hpc-eff --disable                               # remove the cron file
```

This writes `/etc/cron.d/hpc-eff`:

```
*/10 * * * * root /usr/bin/hpc-eff >/dev/null 2>&1
```

Interval guidance differs by mode. With the CO₂ setup the Wattnet data has
15-minute resolution and the signal is a 1 h average, so anything below about
5 minutes adds API traffic without changing the decision. In `temperature` mode the interval is
also your reaction time to a hot room, and it sets how long recovery takes:
upshifts step one band per run, so at 10 minutes a node needs 20 minutes of
sustained cool to get from `LOW_FREQUENCY` back to `HIGH_FREQUENCY`. Shorten
it if that is too slow: no API is being hammered in this mode.

10 minutes is the sensible default for both.

> **Note:** the cron line discards all output. Debug lines, `logger` output,
> and tracebacks all go to `/dev/null`. The database and `state.json` are your
> only record of scheduled runs. To see the output, edit the cron file and drop
> the redirect, or run `sudo hpc-eff` by hand when investigating.

---

## Next

[cluster-rollout.md](cluster-rollout.md): many nodes, upgrades, uninstall.
