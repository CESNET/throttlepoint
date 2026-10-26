# Install ThrottlePoint

Prerequisites, building the RPM or DEB, and what lands on disk. When the
package is installed, continue with [deployment.md](deployment.md) to
configure and start it.

ThrottlePoint is not a daemon. It is a short-lived program that root runs
periodically from `/etc/cron.d/hpc-eff`, every 10 minutes by default. What it
does on each run depends on the shipped `[PIPELINE]` (CO₂ only by default) or,
if you remove it, on `[MODE] control_mode`; see
[regulation-modes.md](regulation-modes.md).

---

## Prerequisites

Required in both modes:

| Requirement | Why | Check |
|---|---|---|
| Root (cron runs as root) | Writing `scaling_max_freq` and `/etc/cron.d` | `id -u` |
| Python ≥ 3.9, `python3-numpy`, `python3-requests` | Runtime | `python3 -c 'import numpy, requests'` |
| `cpupower` (EL) or `cpufreq-set` (Debian) | Applying the cap | `command -v cpupower cpufreq-set` |
| A writable cpufreq interface | Otherwise the cap silently does nothing | `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq` |
| `ipmitool` + DCMI-capable BMC | Power reading for the log (optional) | `ipmitool dcmi power reading` |

Additionally, for CO₂ regulation (the default):

| Requirement | Why | Check |
|---|---|---|
| Outbound HTTPS | CO₂ data (and price, only if you enable it) | see below |
| A Wattnet login | CO₂ rating | email + password, registered at https://api.wattnet.eu/token-request/ (see [configuration.md](configuration.md#wattnet)) |

Additionally, for `control_mode = temperature`:

| Requirement | Why | Check |
|---|---|---|
| A working temperature source | The only input this mode has | depends on `[TEMPERATURE_SOURCE] TYPE`, see [deployment.md](deployment.md#picking-a-temperature-source) |
| `ipmitool` (only if `TYPE=ipmi`) | Reading the sensor: required here, unlike the optional power reading above | `ipmitool sensor reading "INLET_AIR_TEMP"` |
| `nvidia-smi` (only on GPU nodes) | Applying the GPU power limit | `nvidia-smi --query-gpu=count --format=csv,noheader` |

### Network egress (`co2` mode only)

The node must reach these hosts directly. There is no proxy setting in the
config; set `https_proxy` in the cron environment if you need one.

| Host | Purpose | Required? |
|---|---|---|
| `api.wattnet.eu` | CO₂ intensity and the daily token login | Yes |
| `spotovaelektrina.cz` | Current price and historical monthly averages | Only if you add the `price` plugin (not the default) |

Pre-flight check:

```bash
curl -s https://spotovaelektrina.cz/api/v1/price/get-actual-price-czk   # expect an integer
```

### Check the cpufreq driver first

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies 2>/dev/null
```

`acpi-cpufreq` gives you a discrete list of frequencies; `intel_pstate` in
active mode accepts any value in range and rounds it. Both work.

> **Warning:** if `/sys/devices/system/cpu/cpu0/cpufreq/` does not exist at all,
> frequency scaling disabled in BIOS, or a virtualised node, this tool
> cannot do anything on that node.

---

## Build the package

```bash
git clone git@github.com:CESNET/hpc_eff.git
cd hpc_eff
```

### RHEL / AlmaLinux / Rocky (RPM)

```bash
sudo dnf install -y ipmitool make kernel-tools rpm-build rpmdevtools \
                    python3 python3-setuptools python3-numpy python3-requests

make            # clean → sdist → rpmbuild → dnf install
```

`make` builds *and installs* in one go. To build without installing:

```bash
make clean rpmdevdirs sdist rpm
# result: ~/rpmbuild/RPMS/noarch/hpc_eff-<version>-1.noarch.rpm
```

### Debian / Ubuntu (DEB)

```bash
sudo apt update
sudo apt install -y build-essential devscripts debhelper dh-make dh-python \
                    python3-all python3-setuptools fakeroot
sudo apt install -y ipmitool python3-numpy python3-requests \
                    cpufrequtils     # or linux-cpupower on Debian 13

make deb                              # → ../hpc-eff_<version>_amd64.deb
sudo dpkg -i ../hpc-eff_*.deb
sudo apt-get install -f               # pull in any missing runtime deps
```

> **Note:** build once per architecture. The RPM is `noarch` (pure Python,
> installs anywhere). The DEB is `Architecture: any` and builds as `amd64`; the
> code is still architecture-independent, but `dpkg` refuses to install an
> `amd64` .deb on an arm64 node. Build on one host of each architecture you
> run and distribute the resulting file to the rest; compute nodes do not
> need the build toolchain.

CI builds both artifacts on every push
([`.github/workflows/ci.yml`](../.github/workflows/ci.yml), Debian 13 and
Rocky 9), so you can also download them from the workflow run: open the
**Actions** tab on GitHub, pick the run for the commit/branch you want, and
grab `hpc-eff-deb` or `hpc-eff-rpm` from the **Artifacts** section at the
bottom of the run summary. Via the `gh` CLI:

```bash
gh run list --workflow=ci.yml --limit 5
gh run download <run-id> -n hpc-eff-deb   # or hpc-eff-rpm
```

### What the package puts on disk

| Path | Content |
|---|---|
| `/usr/bin/hpc-eff` | The executable |
| `/etc/hpc_eff/config.ini` | Active config (conffile, survives upgrades) |
| `/etc/hpc_eff/config.ini.example` | Reference copy (DEB only) |
| `/var/lib/hpc_eff/history.db` | SQLite log, created on first run |
| `/var/lib/hpc_eff/state.json` | Current state and rolling history, first run |
| `/etc/cron.d/hpc-eff` | Created by `hpc-eff --enable` |

> **Note:** neither the DEB nor the RPM enables cron on install. Nothing
> regulates until you run `sudo hpc-eff --enable`, after setting the Wattnet login
> and checking one manual `sudo hpc-eff` run. Upgrading keeps an existing
> cron job.

---

## Next

[deployment.md](deployment.md): configure, first run, verify, tune.
