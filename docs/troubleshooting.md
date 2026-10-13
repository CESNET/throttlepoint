# Troubleshooting

**First move, always:** run it by hand with debug on. Cron sends every message
to `/dev/null`, so a scheduled run tells you nothing.

```bash
sudo sed -i 's/^DEBUG=.*/DEBUG=yes/' /etc/hpc_eff/config.ini
sudo hpc-eff
```

Remember this is a real run: it applies a frequency change.

---

## The run aborts immediately

```
Config error: [MODE] control_mode must be one of: temperature|co2 (got None).
```

`[MODE] control_mode` is unset or misspelled. It is required and there is no
default. Check the section header spelling too: configparser will not warn you
about a `[MODEL]` typo; it finds no key and the value reads as unset.

---

## Nothing seems to happen at all

The tool is built to fail soft: almost every step catches its own exception,
logs it, and carries on. A "silent" run is nearly always a caught failure.

Check in this order:

```bash
sudo hpc-eff 2>&1 | grep -i -e error -e warn -e fail
sudo sqlite3 -readonly /var/lib/hpc_eff/history.db \
  "SELECT timestamp, rating, rating_price, rating_co2, freq_max FROM hpc_eff_log ORDER BY id DESC LIMIT 3;"
```

If rows are being written but `freq_max` never changes, the rating is not
moving: jump to [the rating is always 5](#the-rating-is-always-5).

---

## The cap is logged but the CPU is unchanged

The log says `Set max frequency: cpupower frequency-set -u 2600000`, but:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq   # unchanged
```

| Cause | Check | Fix |
|---|---|---|
| No cpufreq at all | `ls /sys/devices/system/cpu/cpu0/cpufreq/` | Enable frequency scaling in BIOS. Virtual machines usually cannot do this at all. |
| `intel_pstate` in HWP/passive mismatch | `cat .../scaling_driver` | Try the command by hand and read its output; the tool discards stderr from the frequency tool. |
| Requested value outside the hardware range | `cat .../cpuinfo_{min,max}_freq` | Fix `[frequency_tables]`: values are **MHz** in the config, kHz on the wire. |
| Another agent fighting for it | `systemctl list-units '*power*' '*tuned*'` | `tuned`, a vendor power daemon, or a second copy of this tool will overwrite the cap. Pick one owner. |

Reproduce the exact command manually; it is the fastest diagnosis:

```bash
sudo cpupower frequency-set -u 2600000      # EL
sudo cpufreq-set --max 2600000              # Debian
```

A frequency written in MHz where kHz was meant (`2600` instead of `2600000`)
is the single most common version of this problem.

### "No CPU frequency tool found"

Neither `cpupower` nor `cpufreq-set` is on `PATH`:

```bash
sudo dnf install kernel-tools          # EL, provides cpupower
sudo apt install cpufrequtils          # Debian ≤12, provides cpufreq-set
sudo apt install linux-cpupower        # Debian 13
```

Note the cron file sets a fixed `PATH` of
`/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin`. A tool
installed somewhere else works interactively and fails under cron.

---

## The rating is always 5

5 is the neutral fallback used when **neither** the price nor the CO₂ rating
could be computed. The node is not regulating.

```bash
sudo sqlite3 -readonly /var/lib/hpc_eff/history.db \
  "SELECT COUNT(*), SUM(rating_price IS NULL), SUM(rating_co2 IS NULL) FROM hpc_eff_log;"
```

Test each source directly:

```bash
curl -v https://spotovaelektrina.cz/api/v1/price/get-actual-price-czk
curl -v -H "X-Api-Key: $KEY" -H "User-Agent: test" \
     https://www.nowtricity.com/api/current-emissions/czech-republic/
# Wattnet: log in, then query with the returned token
curl -X POST https://api.wattnet.eu/token-request/get_token \
     -H "Content-Type: application/json" -d '{"email": "<email>", "password": "<password>"}'
```

Usual causes: no outbound HTTPS from compute nodes (very common on HPC
fabrics), a proxy that is not configured for cron, or a firewall that allows
your interactive session but not the cron environment.

There is no proxy setting in the config. If you need one, put it in the cron
file:

```
*/10 * * * * root https_proxy=http://proxy.example.org:3128 /usr/bin/hpc-eff >/dev/null 2>&1
```

---

## `rating_co2` is always NULL, `rating_price` is fine

The CO₂ backend alone is failing; the run falls back to the price rating, which
is why the node keeps working and nothing looks broken.

| Backend | Likely cause |
|---|---|
| `nowtricity` | Missing/expired `NOWTRICITY_API_KEY`, or the quota is exhausted. Each run makes **two** calls: 288/day/node at a 10-minute interval. |
| `nowtricity` | Wrong `NOWTRICITY_ZONE` slug. |
| `wattnet` | `Wattnet needs WATTNET_EMAIL and WATTNET_PASSWORD`: the login is not set. |
| `wattnet` | `Wattnet login rejected`: wrong email/password, or the account is not registered at [api.wattnet.eu/token-request](https://api.wattnet.eu/token-request/). |
| `wattnet` | `Wattnet rejected a freshly issued token`: login works but the account has no API access. Contact Wattnet. (A rejected *cached* token is handled automatically: Wattnet answers it with a redirect to its login page, and ThrottlePoint logs in again once.) |
| `wattnet` | Fewer than 24 hourly values returned; the call raises rather than working with a partial window. |
| both | Egress blocked to that host specifically. |

On a large cluster, check the API quota before blaming the code: hundreds of
nodes each polling every 10 minutes adds up fast.

---

## `rating_price` is always NULL

Either the current-price endpoint is unreachable, or, more interestingly, the
**historical scrape broke**. The monthly averages are parsed out of
spotovaelektrina.cz's HTML; there is no history API. A site redesign changes
the markup and the parser stops finding rows.

That failure is deliberately loud rather than silent: fewer than 6 monthly
values raises `Only parsed N monthly averages … the page layout may have
changed`. Look for that line in a manual run.

There is no workaround in config: the parser in
[`energy_price.py`](../src/hpc_eff/utils/energy_price.py) needs updating. In
the meantime the node keeps regulating on the CO₂ rating alone.

---

## `power_w` is NULL

`POWERREADINGCMD` failed or its output did not match. Run it by hand:

```bash
ipmitool dcmi power reading
```

It must produce the standard four lines including
`Instantaneous power reading:`. Nodes without a DCMI-capable BMC cannot supply
this at all.

**This is cosmetic.** The power reading is logged and never acted on: a NULL
column costs you the energy-accounting queries and nothing else.

---

## Cron is not running it

```bash
cat /etc/cron.d/hpc-eff
systemctl status crond    # EL
systemctl status cron     # Debian
grep -i hpc-eff /var/log/cron /var/log/syslog 2>/dev/null | tail
```

| Symptom | Cause |
|---|---|
| No such file | `hpc-eff --enable` was never run. On RPM this is manual; only the DEB enables it automatically at install. |
| File exists, no runs | cron daemon not enabled, or the file's permissions are wrong (must be 0644, root-owned). |
| Ran once, then stopped | Nothing in the tool disables itself; look for config management overwriting `/etc/cron.d/`. |

`hpc-eff --enable` and `--disable` both require root and raise a clear
`Permission denied` otherwise.

---

## Temperature mode reads nothing

`read_temperature` returns `None` on any failure, since every source catches
its own exceptions, so `apply_cpu_thermo` returns without raising and **no frequency
is applied**. A row is still written, with `temperature` NULL, and `state.json`
records `Temp None C`. Nothing is logged above `logger.warning`, which cron
discards. A wrong sensor name therefore looks exactly like a healthy quiet
node.

```bash
ipmitool sensor reading "INLET_AIR_TEMP"     # exact name matters
ipmitool sensor list | head -40              # find the real one
```

For `TYPE=http_api`, `curl` the URL and confirm `HTTP_JSON_PATH` matches the
structure. For `TYPE=custom`, import your module by hand:

```bash
sudo python3 -c "import sys; sys.path.insert(0,'/opt/mysensors'); import my_reader; print(my_reader.read())"
```

Also confirm the section is complete: if any of `MID_LIMIT`, `HIGH_LIMIT`,
`HIGH_FREQUENCY`, `MID_FREQUENCY`, `LOW_FREQUENCY` is missing, `[CPU_THERMO]`
skips itself with only a debug-level message.

---

## GPU power is not changing

```bash
nvidia-smi --query-gpu=power.limit,power.max_limit --format=csv
sudo nvidia-smi -i 0 -pl 200        # does a manual set work?
```

| Cause | Note |
|---|---|
| `control_mode` is not `temperature` | GPU regulation only runs in that mode. |
| No temperature reading | Same root cause as above: the GPU regulator uses the *same* ambient sensor. |
| Thresholds never reached | `[GPU_POWER]` defaults are 70/80 °C. If your `[TEMPERATURE_SOURCE]` reports a lower range, they never trigger. Compare the `temperature` column against the limits and set them against the range your sensor actually produces. |
| Persistence mode off | Some drivers reject `-pl` without it: `nvidia-smi -pm 1`. |
| No GPUs | The module no-ops by design. |

---

## The config file seems to be ignored

`hpc-eff` reads `/etc/hpc_eff/config.ini`, and **falls back to
`src/hpc_eff/config.ini.example` relative to the current working directory** if
that file is absent. Running from a git checkout with no installed config picks
up the example, with a placeholder API key and whatever mode the example
ships.

```bash
ls -l /etc/hpc_eff/config.ini
```

If it is missing, create it:

```bash
sudo install -D -m 640 /etc/hpc_eff/config.ini.example /etc/hpc_eff/config.ini
```

---

## After a package upgrade

The config is a conffile in both packages (`%config(noreplace)` / dpkg
conffile), so your settings survive. Database migration is automatic on the
next run.

If a run starts failing right after an upgrade, check that a new config key is
not required, and compare against the shipped example:

```bash
diff <(grep -o '^\[.*\]\|^[A-Za-z_]*=' /etc/hpc_eff/config.ini) \
     <(grep -o '^\[.*\]\|^[A-Za-z_]*=' /etc/hpc_eff/config.ini.example)
```

---

## Getting back to a clean state

```bash
sudo hpc-eff --disable
sudo cpupower frequency-set -u $(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)
# Debian: sudo cpufreq-set --max $(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)

# temperature mode, on GPU nodes: the power limit is left where it was too
sudo nvidia-smi -pl $(nvidia-smi --query-gpu=power.max_limit --format=csv,noheader,nounits | head -1)
```

Removing the package does **not** restore the frequency or the GPU power
limit: whatever the last run applied stays in place until it is reset
explicitly or the node reboots.
