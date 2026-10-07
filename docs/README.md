# ThrottlePoint documentation

| Document | What it covers |
|---|---|
| **[install.md](install.md)** | **Start here.** Prerequisites, building the RPM or DEB, what lands on disk. |
| [deployment.md](deployment.md) | Configure, first run, verify, tune: one node, either mode. |
| [cluster-rollout.md](cluster-rollout.md) | Many nodes, upgrades, uninstall, first-week health checks. |
| [configuration.md](configuration.md) | Every section and key of `/etc/hpc_eff/config.ini`, with defaults and units. |
| [regulation-modes.md](regulation-modes.md) | What each mode does with its input: the rating maths, the thermal bands. |
| [plugins.md](plugins.md) | The score/action pipeline: built-in plugins, writing your own, the JSON contract. |
| [monitoring.md](monitoring.md) | Database schema, `state.json`, and the SQL worth running once it is live. |
| [troubleshooting.md](troubleshooting.md) | Symptom-first: what to check when a step above does not work. |

## The short version

ThrottlePoint runs from cron as root every 10 minutes and caps the CPU's
maximum frequency. What drives the cap depends on `[MODE] control_mode`:

- `co2`: grades electricity price against a trailing year and carbon
  intensity against the last 24 hours into a rating from 1 (cheap and clean)
  to 10 (expensive and dirty).
- `temperature`: reads one sensor and picks one of three frequency bands,
  also limiting NVIDIA GPU power. No external APIs.

```bash
make                                  # or: make deb && sudo dpkg -i ../hpc-eff_*.deb
sudo vi /etc/hpc_eff/config.ini       # pick control_mode, then fill in its section
sudo hpc-eff                          # one real run: applies a cap immediately
sudo hpc-eff --enable                 # install /etc/cron.d/hpc-eff
```

Each step in full, including what to verify in between:
[install.md](install.md), then [deployment.md](deployment.md).
