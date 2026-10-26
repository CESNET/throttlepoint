# Regulation modes

> **The shipped default does not use `[MODE]`.** `config.ini.example` contains a
> `[PIPELINE]` (`score = co2`, `act = cpu_freq`) with `[CO2_API] THRESHOLD`: the
> CPU is throttled only while Wattnet CO₂ is above the threshold, see
> [configuration.md](configuration.md#threshold-when-to-throttle). This page
> describes the `[MODE] control_mode` presets used when `[PIPELINE]` is removed.

`[MODE] control_mode` is the one switch that decides how a node is regulated.
There are two values, and they are mutually exclusive by construction: a node
is driven either by carbon/price or by temperature, never by both.

| `control_mode` | CPU | GPU | Inputs |
|---|---|---|---|
| `co2` | rating 1–10 → max-frequency cap | untouched | spot price + grid carbon intensity |
| `temperature` | hysteresis band → max frequency | NVIDIA power limit | one temperature sensor |

The value is required. An unset or unknown one exits with a config error rather
than running a no-op cycle.

---

## `co2` mode

Deployment walkthrough: [deployment.md](deployment.md#1-configure).
This section explains what the numbers mean.

### The pipeline

```
spot price (CZK/MWh) ──► classify vs 12 monthly averages ──► rating_price 1–10 ─┐
                                                                                ├─► combine ──► rating 1–10 ──► frequency table ──► scaling_max_freq
grid CO₂ (gCO₂eq/kWh) ─► percentile within last 24 h ───────► rating_co2  1–10 ─┘
```

Rating 1 = cheapest/cleanest = **highest** clock. Rating 10 = worst =
**lowest** clock.

`co2` mode has no thermal cutoff: it never reads temperature. The BMC and
the CPU's own throttling are its only thermal protection
([install.md](install.md#prerequisites)). If you need an explicit
thermal cutoff, use `temperature` mode instead.

### Price rating

Two endpoints of [spotovaelektrina.cz](https://spotovaelektrina.cz), which
republishes Czech OTE spot market prices:

- **Current price**: `/api/v1/price/get-actual-price-czk` returns the current
  hour's price as a plain integer (CZK/MWh), no JSON wrapper.
- **Historical baseline**: `/historicke-ceny/<year>/1`, scraped from HTML;
  there is no public history API. Each row is a month, the first `⌀ … Kč` cell
  is its average. The whole calendar year appears on every page regardless of
  the month in the path.

The baseline is built from the current date: both the current and previous
year's page are fetched and the most recent 12 monthly averages kept. That
keeps the window at a trailing ~year even in January, when the current-year
page alone would hold a few weeks.

`classify_price_by_median` then places the current price relative to the
median of those 12 values:

| Price relative to baseline | Rating | Label |
|---|---|---|
| At the 12-month minimum | 1 | cheap |
| At the median | 5 | average |
| At the 12-month maximum | 10 | expensive |

Above the median the rating scales 5→10 across `median…max`; below it scales
5→1 across `min…median`. Ratings 1–3 read as *cheap*, 4–7 *average*, 8–10
*expensive*.

Because the history is scraped, a site redesign can silently break it. The
parser guards against this: fewer than 6 monthly values raises an explicit
error, the controller logs it, and the cycle falls back to the CO₂ rating (or
the neutral 5) rather than regulating against a corrupt baseline. Watch for
`rating_price` going NULL in the database.

### CO₂ rating

The last 24 hours of grid carbon intensity are fetched, and the current value
is graded as its percentile within that window:

```
grade = round(fraction_of_last_24h_values_below_current × 9) + 1
```

So 1 = among the cleanest hours of the day, 10 = among the dirtiest.

### Why price is weighted higher than carbon

The default is `rating_type=average` with `weight_price=0.6`,
`weight_co2=0.4`. That asymmetry is deliberate, and it is about the baselines,
not about caring less for carbon.

| | Price rating | CO₂ rating |
|---|---|---|
| Baseline | 12 monthly averages | last 24 hours |
| Character | absolute, stable | relative, resets daily |
| A 9 means | genuinely expensive compared to the whole year | dirtier than most of *today*, even on a uniformly clean day |

The CO₂ grade is a percentile over a 24-hour window, so it sweeps the full
1–10 range every single day by construction: the cleanest hour of a clean day
still grades 1 and the dirtiest hour of that same clean day still grades 10.
Weighting it equally or higher would let that day-relative volatility dominate
the frequency cap regardless of whether the grid was actually dirty.

The 0.6/0.4 split keeps the long-term price signal in charge while letting
intraday carbon intensity nudge the cap toward cleaner hours. In the Czech grid
the two correlate anyway, both spike when fossil plants set the marginal price,
so in practice the CO₂ term acts as fine-tuning.

All three values (`rating_price`, `rating_co2`, `rating`) are written to the
database and `state.json`, so you can re-tune the weights against your own
history instead of guessing. See
[monitoring.md](monitoring.md#did-the-weights-do-anything).

### Choosing `rating_type`

| Value | Behaviour | Effect on the node |
|---|---|---|
| `price` | price rating only | Pure cost optimisation. Carbon ignored. |
| `average` | weighted mean, rounded, clamped 1–10 | Balanced. Recommended default. |
| `max` | the worse of the two | Most aggressive: throttles if *either* signal is bad, so the node spends more time capped. |

---

## `temperature` mode

Deployment walkthrough:
[deployment.md](deployment.md#mode-temperature).

For thermally-constrained rooms rather than carbon or cost. It reads one
temperature (from `[TEMPERATURE_SOURCE]`) and drives two independent
regulators: CPU max frequency and NVIDIA GPU power limit.

Nothing here touches price or CO₂: no external APIs are called at all, which
also makes this the mode to use on nodes with no outbound internet access.

### The pipeline

```
[TEMPERATURE_SOURCE] ──► CPU band  ──► scaling_max_freq
[TEMPERATURE_SOURCE] ──► GPU band  ──► nvidia-smi -pl
```

Two independent band decisions, each reading `[TEMPERATURE_SOURCE]` on its
own: `apply_cpu_thermo` and `regulate_gpus` each call `read_temperature()`
separately, once per run. For a stable source both calls return the same
value in practice, but it is two reads, not one shared reading, so a flaky
source could in principle answer them differently. Cool = **highest** clock
and full power; hot = lowest. The two regulators have separate limits in
`[CPU_THERMO]` and `[GPU_POWER]` and do not have to agree, but since both
read the same sensor, their limits must be on the same scale to both work.

### CPU bands and hysteresis

Three bands from `[CPU_THERMO]`:

| Temperature | Target |
|---|---|
| `≥ HIGH_LIMIT` | `LOW_FREQUENCY` |
| `≥ MID_LIMIT` | `MID_FREQUENCY` |
| below `MID_LIMIT` | `HIGH_FREQUENCY`, but only via the hysteresis rule below |

**Downshifts are immediate; upshifts require 2 °C of headroom.** Coming down
from a hot state, the node only steps up one level once the temperature is at
or below `MID_LIMIT - 2`, and it steps up **one level per run**: LOW → MID →
HIGH, not LOW → HIGH. At a 10-minute cron interval, recovering from the lowest
band therefore takes two cycles of sustained cool.

The current state is inferred each run by comparing `scaling_max_freq` against
the three configured values, so the state survives across cron invocations
without any stored state. The corollary: if something else changes the max
frequency out from under the tool, the state reads as `UNKNOWN` and the next
run jumps straight to `HIGH_FREQUENCY`.

The frequency is applied per-CPU across all cores found in sysfs. A Slack
webhook, if configured, fires only when the frequency actually changes.

### GPU power limiting

Same shape, applied to `nvidia-smi` power limits:

| Temperature | Target power |
|---|---|
| `≥ HIGH_LIMIT` (default 80 °C) | `LOW_POWER` (default 40 %) |
| `≥ MID_LIMIT` (default 70 °C) | `MID_POWER` (default 70 %) |
| below `MID_LIMIT` | `HIGH_POWER` (default 100 %), with the same 2 °C upshift hysteresis |

Two things to be clear about:

- The temperature is the *same* ambient reading the CPU regulator uses, not
  GPU die temperature. `nvidia-smi` is used to *set* power, never to read
  temperature.
- All GPUs in a node get the same limit. There is no per-GPU decision.

Unlike the CPU regulator, the GPU state machine starts each run at `UNKNOWN`,
which resolves to `HIGH` when the temperature is below `MID_LIMIT`. Under cron
this means the up-shift hysteresis effectively does not apply across runs: a
cool reading restores full power in one cycle.

Nodes without NVIDIA GPUs no-op cleanly; a missing `nvidia-smi` is caught and
logged, not fatal.

---

## Which mode for which situation

| Situation | Mode |
|---|---|
| Cutting carbon footprint / grid-aware operation | `co2` |
| Cutting electricity bills against spot prices | `co2` with `rating_type=price` |
| Room or cooling loop is the binding constraint | `temperature` |
| GPU nodes in a hot room | `temperature` |
| No outbound internet access | `temperature` (`co2` cannot work) |

Need both on one node? You can't: split the cluster instead, thermally
exposed nodes in `temperature` mode and the rest in `co2`.

---

## Next

[monitoring.md](monitoring.md): database schema, `state.json`, and the SQL
worth running once it is live.
