# Logging & monitoring

Every run leaves two records: one row in an SQLite database (the full history)
and a rewritten JSON state file (the current picture, for external monitoring
to scrape). Both paths are set in `[logging]`.

| | Database | State file |
|---|---|---|
| Default path | `/var/lib/hpc_eff/history.db` | `/var/lib/hpc_eff/state.json` |
| Retention | unbounded, one row per run | last `history_length` entries (default 10) |
| Written | append | rewritten in full each run |
| Permissions | root-owned | chmod 644 |
| For | analysis, tuning, reporting | dashboards, Prometheus textfile, Icinga checks |

There is no log file. Cron discards stdout and stderr
(`>/dev/null 2>&1`), so anything the run printed is gone: these two records
are your only evidence that a scheduled run happened at all.

---

## The database

One table, `hpc_eff_log`, one row per evaluation:

```bash
sudo sqlite3 /var/lib/hpc_eff/history.db ".schema hpc_eff_log"
```

| Column | Unit | Written in | Meaning |
|---|---|---|---|
| `id` | | always | autoincrement |
| `timestamp` | UTC ISO 8601 | always | when the row was inserted |
| `hostname` | | always | `socket.gethostname()` |
| `freq_min` | kHz | both modes | **always `0`** (a placeholder, not a real minimum) |
| `freq_max` | kHz | both modes | the cap actually applied this run |
| `score_name`, `score_value` | | always | node's benchmark label/score, set manually in `[SYSTEM]`; see [configuration.md](configuration.md#system) |
| `price` | CZK/MWh | `co2` | current spot price |
| `co2_current` | gCO₂eq/kWh | `co2` | current grid carbon intensity |
| `co2_median` | gCO₂eq/kWh | `co2` | median of the last 24 h |
| `co2_grade` | 1–10 | `co2` | CO₂ percentile grade, **TEXT column**, holds `unknown` when the API failed |
| `power_w` | W | `co2` | instantaneous node power from `POWERREADINGCMD` |
| `cpu_freq_current` | MHz | `co2` | frequency observed *before* this run's change |
| `rating` | 1–10 | always | `co2`: the combined rating that chose `freq_max`. `temperature`: always `5`, an unused leftover; the thermal bands choose `freq_max` instead |
| `rating_price` | 1–10 | `co2` | price component; NULL if the price fetch failed |
| `rating_co2` | 1–10 | `co2` | CO₂ component; NULL if the CO₂ fetch failed |
| `temperature` | °C | `temperature` | the sensor reading |
| `gpu_power_limit` | W | `temperature` | limit in effect this run (== `gpu_target_power`) |
| `gpu_target_power` | W | `temperature` | limit this run aimed for |
| `gpu_state` | | `temperature` | `HIGH` / `MID` / `LOW` / `UNKNOWN` |
| `gpu_count` | | `temperature` | GPUs found |
| `gpu_changed`, `gpu_success` | 0/1 | `temperature` | whether a change was attempted and whether it worked |

Mind the units: **`freq_max` is kHz, `cpu_freq_current` is MHz.** They differ
by 1000 and both describe frequency, which makes them easy to plot against each
other incorrectly.

Columns belonging to the inactive mode stay NULL. A `co2`-mode node has NULL
`temperature` and NULL `gpu_*` for its whole life; that is expected, not a
fault.

### Schema migration

Nothing to do by hand. On every start `hpc-eff` renames a legacy
`cpu_settings_log` table to `hpc_eff_log` and `ALTER TABLE`s in any column the
schema has gained since the file was created. History is preserved in place.

### Size

One row per run: roughly 52 000 rows/year at a 10-minute interval, a handful
of megabytes. There is no automatic pruning. If it matters, rotate it yourself:

```bash
sudo sqlite3 /var/lib/hpc_eff/history.db \
  "DELETE FROM hpc_eff_log WHERE timestamp < date('now','-1 year'); VACUUM;"
```

---

## The state file

A JSON file, rewritten in full on every run:

```json
{
  "static": {
    "score_name": "SPEC2017",
    "score_value": 3.8,
    "power_cmd": "ipmitool dcmi power reading",
    "plugins": { "read": ["power", "cpufreq"], "score": ["price", "co2", "combine"],
                 "act": ["cpu_freq"], "record": ["db_log", "state_json"] }
  },
  "current": {
    "timestamp": "2026-08-21T10:00:03Z",
    "rating": 5, "rating_price": 6, "rating_co2": 4,
    "selected_freq_khz": 2600000,
    "summary": "Price 2431 is average; Combined rating 5 (price 6, CO2 4)",
    "action_price": "Price 2431 is average; Combined rating 5 (price 6, CO2 4)",
    "price": 2431, "co2_current": 412, "co2_median": 448, "co2_grade": 4,
    "power_w": 287, "cpu_freq_current": 2712
  },
  "history": [ "…same shape, newest first, up to history_length entries…" ]
}
```

`current` is a copy of `history[0]`. `static` is refreshed every run, so it
also serves as a liveness marker for the config the node is running with.

The example above is a `co2`-mode node. A `temperature`-mode node carries
`temperature`, `thermo_freq_limit`, and `action_temp` instead, plus the `gpu_*`
fields on GPU nodes; the price and rating keys are absent entirely.

`summary` (and `action_temp` in `temperature` mode) is the human-readable
summary of what the run decided: the fastest thing to eyeball when something
looks wrong. **`action_temp` exists only here, not in the database**, so it is
the only place a thermal failure explains itself.

`action_price` is a **deprecated** copy of `summary`, written only when the
`price` plugin runs. It will be removed in a later release; read `summary`.

Each run reads the whole file, replaces `current`, prepends the new entry to
`history` (trimmed to `history_length`), refreshes `static`, and writes the
whole file back. If the file does not exist yet, it is created with empty
`history`/`current`; if it exists but fails to parse, that run silently
starts over from an empty structure rather than failing.

The write is a plain truncate-and-rewrite, not atomic: a poller reading at
the exact moment of a write can see a truncated or invalid-JSON file. A
consumer that parses this file on a timer should tolerate an occasional
parse failure and retry, rather than treat one as a fault.

The file is world-readable (chmod 644). A stale-state check only needs
`current.timestamp` compared against `now`; alert if it's older than a few
cron intervals.

---

## Queries worth having

Most of these read the `co2`-mode columns; the
[temperature-mode ones](#temperature-mode-queries) are further down.

### Is it running, and is it healthy?

```sql
SELECT date(timestamp) AS day,
       COUNT(*)                  AS runs,
       SUM(rating_price IS NULL) AS price_fail,
       SUM(rating_co2 IS NULL)   AS co2_fail,
       ROUND(AVG(rating), 2)     AS avg_rating
FROM hpc_eff_log
GROUP BY day ORDER BY day DESC LIMIT 14;
```

144 runs/day at a 10-minute interval. `avg_rating` pinned at exactly 5 with
both failure counts high means the node is not regulating; see
[troubleshooting.md](troubleshooting.md#the-rating-is-always-5).

### How much time is spent at each cap?

```sql
SELECT freq_max/1000 AS mhz,
       COUNT(*)      AS runs,
       ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM hpc_eff_log), 1) AS pct
FROM hpc_eff_log
WHERE freq_max IS NOT NULL
GROUP BY mhz ORDER BY mhz DESC;
```

If one row holds >90 % of the time, the frequency table is too coarse or the
rating never moves; revisit
[deployment.md](deployment.md#co2-mode-the-frequency-table).

### Did the weights do anything?

```sql
SELECT rating_price, rating_co2, rating, COUNT(*) AS n
FROM hpc_eff_log
WHERE rating_co2 IS NOT NULL
GROUP BY rating_price, rating_co2, rating
ORDER BY n DESC LIMIT 20;
```

This is the table to tune `weight_price`/`weight_co2` against: it shows how
often the CO₂ term actually shifted the combined rating away from the price
rating. If `rating` equals `rating_price` in nearly every row, the CO₂ signal
is not earning its weight on your grid.

### Daily profile: when is the node throttled?

```sql
SELECT strftime('%H', timestamp) AS hour_utc,
       ROUND(AVG(rating), 2)     AS avg_rating,
       ROUND(AVG(freq_max)/1000) AS avg_mhz
FROM hpc_eff_log
GROUP BY hour_utc ORDER BY hour_utc;
```

### Rough energy accounting

```sql
SELECT date(timestamp) AS day,
       ROUND(AVG(power_w))                     AS avg_w,
       ROUND(AVG(power_w) * 24 / 1000.0, 1)    AS approx_kwh,
       ROUND(AVG(power_w) * 24 / 1000.0 * AVG(co2_current) / 1000.0, 2) AS approx_kg_co2
FROM hpc_eff_log
WHERE power_w IS NOT NULL
GROUP BY day ORDER BY day DESC LIMIT 30;
```

Estimate only, not metering-grade: `power_w` is one BMC sample per cron
interval, assumed to hold for the whole interval.

### Temperature-mode queries

Health check, the equivalent of the first query above:

```sql
SELECT date(timestamp) AS day,
       COUNT(*)                     AS runs,
       SUM(temperature IS NULL)     AS read_fail,
       ROUND(MIN(temperature), 1)   AS min_c,
       ROUND(MAX(temperature), 1)   AS max_c,
       COUNT(DISTINCT freq_max)     AS caps_used
FROM hpc_eff_log
GROUP BY day ORDER BY day DESC LIMIT 14;
```

Any `read_fail` at all means the sensor was unreadable and **no cap was
applied that run**: there is no fallback in this mode. `caps_used` of 1 in a
stable room is the intended steady state, not a fault; `caps_used` of 3 every
day means your limits sit below the room's normal range.

Where the bands actually sit, which is how you re-tune `MID_LIMIT`/`HIGH_LIMIT`
against real readings rather than guesses:

```sql
SELECT freq_max/1000            AS mhz,
       COUNT(*)                 AS runs,
       ROUND(MIN(temperature),1) AS min_c,
       ROUND(MAX(temperature),1) AS max_c
FROM hpc_eff_log
WHERE temperature IS NOT NULL
GROUP BY mhz ORDER BY mhz DESC;
```

GPU regulation, if you have it:

```sql
SELECT gpu_state, COUNT(*) AS runs,
       SUM(gpu_changed) AS changes,
       SUM(gpu_success = 0) AS failures
FROM hpc_eff_log
WHERE gpu_count > 0
GROUP BY gpu_state;
```

`gpu_state` stuck at `HIGH` forever usually means the `[GPU_POWER]` thresholds
(70/80 °C by default) sit above anything the shared sensor ever reports.
Compare them against the `temperature` column; see
[troubleshooting.md](troubleshooting.md#gpu-power-is-not-changing).

### Fleet-wide

`hostname` is on every row, so per-node databases can be `scp`'d in and
combined with SQLite's `ATTACH`: one `INSERT ... SELECT` per node into a
shared table.

---

## Reading the database on a running node

The database is opened in WAL mode, so concurrent reads are safe: you do not
need to stop cron, and you do not need to copy the file first:

```bash
sudo sqlite3 -readonly /var/lib/hpc_eff/history.db "SELECT * FROM hpc_eff_log ORDER BY id DESC LIMIT 5;"
```

If you do want a copy, take it properly (a plain `cp` of a WAL database can
miss the most recent transactions):

```bash
sudo sqlite3 /var/lib/hpc_eff/history.db ".backup /tmp/history.db"
```

---

## Next

[troubleshooting.md](troubleshooting.md): symptom-first, what to check when a
step above does not work.
