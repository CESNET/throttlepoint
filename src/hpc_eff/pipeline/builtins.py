"""Built-in plugins: today's hardwired behaviour, behind the plugin contract.

Every built-in is `fn(request, config, runtime) -> {"set": {...}, "state": {...}}`.
`request["context"]` holds the values produced so far. `set` is merged into the
context (and from there into the DB row); `state` is merged into the
state.json entry. Built-ins run in-process and receive the live ConfigParser;
executable plugins get the same request/response as JSON instead.
"""

import json
import traceback
from pathlib import Path

from ..utils.energy_price import get_current_energy_price, get_averages_year, classify_price_by_median
from ..utils.power_reader import get_power_reading
from ..utils.frequency_reader import get_cpu_frequency
from ..utils.co2_value import co2_value
from ..utils.cpu_thermo import apply_cpu_thermo
from ..utils.set_cpu import set_cpu_freq, log_setting

DEFAULT_STATE_PATH = "/var/lib/hpc_eff/state.json"


def combine_ratings(rating_price, rating_co2, config, debug_log):
    """Combine the price and CO2 ratings into the final rating (1-10).

    Controlled by the [aggregation] section:
    - rating_type=price   : price rating only (default, legacy behaviour)
    - rating_type=co2     : CO2 rating only
    - rating_type=average : weighted average of price and CO2 ratings
    - rating_type=max     : the worse (higher) of the two ratings

    Falls back to whichever rating is available when the preferred one could
    not be computed (e.g. CO2 API failure, or a pipeline without a price
    plugin), and to the neutral 5 when neither is.
    """
    rating_type = config.get("aggregation", "rating_type", fallback="price")

    if rating_type == "co2" and rating_co2 is not None:
        return rating_co2

    if rating_type == "price" or rating_co2 is None:
        if rating_type != "price" and rating_co2 is None:
            debug_log("Aggregation: CO2 rating unavailable, falling back to price rating")
        return rating_price if rating_price is not None else 5

    if rating_price is None:
        debug_log("Aggregation: price rating unavailable, falling back to CO2 rating")
        return rating_co2

    if rating_type == "max":
        return max(rating_price, rating_co2)

    if rating_type == "average":
        w_price = config.getfloat("aggregation", "weight_price", fallback=0.6)
        w_co2 = config.getfloat("aggregation", "weight_co2", fallback=0.4)
        combined = (w_price * rating_price + w_co2 * rating_co2) / (w_price + w_co2)
        return max(1, min(10, round(combined)))

    debug_log(f"Aggregation: unknown rating_type '{rating_type}', using price rating")
    return rating_price


def update_state_json(new_entry, state_file_path, history_length, static_context, debug_log):
    """Update the JSON state file with history."""
    state_path = Path(state_file_path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)

        data = {"static": static_context, "history": [], "current": {}}
        if state_path.exists():
            try:
                with open(state_path, "r") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        data["current"] = new_entry
        data["history"].insert(0, new_entry)
        data["history"] = data["history"][:history_length]
        data["static"] = static_context

        with open(state_path, "w") as f:
            json.dump(data, f, indent=4)

        state_path.chmod(0o644)
        debug_log(f"Updated state JSON at {state_file_path}")
    except Exception as e:
        debug_log(f"Error updating state JSON: {e}")


# ---------------------------------------------------------------- read stage

def power(req, config, rt):
    """Read the node's instantaneous power draw via [SYSTEM] POWERREADINGCMD."""
    watts = None
    try:
        data = get_power_reading(config.get("SYSTEM", "POWERREADINGCMD"))
        watts = data.get("instantaneous")
        rt.debug_log(f"Current power usage (Watts): {watts}")
    except Exception as e:
        rt.debug_log(f"Error reading power data: {e}")
    return {"set": {"power_w": watts}, "state": {"power_w": watts}}


def cpufreq(req, config, rt):
    """Read the current CPU frequency (MHz)."""
    freq = None
    try:
        freq = get_cpu_frequency()
        rt.debug_log(f"Current CPU frequency (MHz): {freq}")
    except Exception as e:
        rt.debug_log(f"Error reading CPU frequency: {e}")
    return {"set": {"cpu_freq_current": freq}, "state": {"cpu_freq_current": freq}}


# --------------------------------------------------------------- score stage

def price(req, config, rt):
    """Electricity price, rated against a year of monthly averages."""
    current = None
    try:
        current = get_current_energy_price()
        rt.debug_log(f"Current electricity price (CZK/MWh): {current}")
    except Exception as e:
        rt.debug_log(f"Error fetching electricity price: {e}")

    averages = []
    try:
        averages = get_averages_year()
        rt.debug_log(f"Average monthly prices last year (CZK/MWh): {averages}")
    except Exception as e:
        rt.debug_log(f"Error fetching average prices: {e}")

    rating_price = None
    state = {}
    try:
        classify, rating_price = classify_price_by_median(current, averages)
        rt.debug_log(f"The current price of {current} is {classify} ({rating_price}).")
        state["summary"] = f"Price {current} is {classify}"
    except Exception as e:
        rt.debug_log(f"Error fetching classify price and rating: {e}")

    values = {"price": current, "rating_price": rating_price}
    state.update(values)
    return {"set": values, "state": state}


def co2(req, config, rt):
    """CO2 intensity (Nowtricity or Wattnet per [CO2_API]), graded 1-10 as a 24 h percentile."""
    try:
        history, current, median, grade = co2_value(config)
        rt.debug_log(f"Last 24 hours CO2 values (g CO2eq/kWh): {history}")
        rt.debug_log(f"Current CO2 value (g CO2eq/kWh): {current}")
        rt.debug_log(f"Median value from 24 hours values (g CO2eq/kWh): {median}")
        rt.debug_log(f"Current CO2 value grade from 1 (low) to 10 (high): {grade}")
    except Exception as e:
        current, median, grade = None, None, "unknown"
        rt.debug_log(f"Error fetching co2 values and rating: {e}")

    values = {
        "co2_current": current,
        "co2_median": median,
        "co2_grade": grade,
        # CO2 grade (1 low - 10 high) doubles as the CO2 rating
        "rating_co2": grade if isinstance(grade, int) else None,
    }
    return {"set": values, "state": dict(values)}


def combine(req, config, rt):
    """Merge rating_price / rating_co2 into the final `rating` per [aggregation]."""
    ctx = req["context"]
    rating_price = ctx.get("rating_price")
    rating_co2 = ctx.get("rating_co2")
    if "rating_price" not in ctx:
        # No price plugin ran (e.g. a CO2-only pipeline): there is nothing to
        # combine with, so [aggregation] does not apply.
        rating = rating_co2 if rating_co2 is not None else 5
        return {"set": {"rating": rating}, "state": {"rating": rating}}
    rating = combine_ratings(rating_price, rating_co2, config, rt.debug_log)

    state = {"rating": rating}
    if rating != rating_price:
        state["summary"] = f"Combined rating {rating} (price {rating_price}, CO2 {rating_co2})"
    return {"set": {"rating": rating}, "state": state}


# ---------------------------------------------------------------- act stage

def cpu_freq(req, config, rt):
    """Cap the CPU max frequency from `rating` via [frequency_tables]."""
    rating = req["context"].get("rating", 5)
    rt.debug_log(f"Current rating: {rating}")
    selected = set_cpu_freq(rating, config)

    values = {}
    state = {"selected_freq_khz": selected}
    if selected:
        values = {"freq_min": 0, "freq_max": selected}
        state["summary"] = f"Rating {rating} set {selected}kHz"
    return {"set": values, "state": state}


def cpu_thermo(req, config, rt):
    """Temperature-banded CPU max frequency with hysteresis ([CPU_THERMO])."""
    notes = []
    temperature = None
    values, state = {}, {}
    thermo_freq_limit = None

    old_temp = None
    try:
        state_path = config.get("logging", "state_json_path", fallback=DEFAULT_STATE_PATH)
        if Path(state_path).exists():
            with open(state_path, "r") as f:
                old_temp = json.load(f).get("current", {}).get("temperature")
    except Exception:
        pass

    try:
        res = apply_cpu_thermo(config)
        temperature = res.get("temperature")
        values["temperature"] = temperature
        if res.get("target_khz") is not None:
            values["freq_min"] = 0
            values["freq_max"] = res.get("target_khz")

        status_msg = f"Temp {temperature} C"
        if temperature is not None and old_temp is not None:
            if temperature >= old_temp + 2:
                status_msg += " (rising)"
            elif temperature <= old_temp - 2:
                status_msg += " (dropping)"

        if res.get("changed"):
            thermo_freq_limit = res.get("target_freq")
            rt.debug_log(f"cpu_thermo applied target {thermo_freq_limit}")
            notes.append(f"{status_msg}: Applied thermal frequency limit: {thermo_freq_limit}")
        else:
            notes.append(f"{status_msg}: Thermal state stable at {res.get('target_freq')}")
    except Exception as e:
        rt.debug_log(f"cpu_thermo error: {e}")
        notes.append(f"Thermal policy evaluation error: {e}")

    state.update({
        "temperature": temperature,
        "thermo_freq_limit": thermo_freq_limit,
        "action_temp": "; ".join(notes),
    })
    return {"set": values, "state": state}


def gpu_power(req, config, rt):
    """NVIDIA power limit from ambient temperature ([GPU_POWER])."""
    try:
        from ..utils.gpu_power import regulate_gpus
        results = regulate_gpus(config, rt.debug_log)
        rt.debug_log(f"GPU Power: finished - {len(results)} GPUs processed")
        first = results[0] if results else None
    except Exception as e:
        rt.debug_log(f"GPU Power: error during regulation - {e}")
        rt.debug_log(f"GPU Power: traceback: {traceback.format_exc()}")
        return {}

    if not first:
        return {}
    fields = {
        "gpu_power_limit": first.get("power_limit"),
        "gpu_target_power": first.get("target_power"),
        "gpu_state": first.get("state"),
        "gpu_changed": first.get("changed", False),
        "gpu_success": first.get("success", False),
        "gpu_count": first.get("gpu_count"),
    }
    return {"set": fields, "state": dict(fields)}


# ------------------------------------------------------------- record stage

def db_log(req, config, rt):
    """One unified row per evaluation in hpc_eff_log."""
    if rt.conn is None:
        return {}
    try:
        log_setting(rt.conn, **req["context"])
    except Exception as e:
        rt.debug_log(f"DB log failed: {e}")
    return {}


def state_json(req, config, rt):
    """Write the evaluation to state.json (current entry plus history)."""
    entry = req["state"]
    if "summary" in entry and "rating_price" in entry:
        # Deprecated alias of `summary`, kept for readers of the old key.
        entry["action_price"] = entry["summary"]
    path = config.get("logging", "state_json_path", fallback=DEFAULT_STATE_PATH)
    history_length = config.getint("logging", "history_length", fallback=10)
    update_state_json(entry, path, history_length, rt.static_context, rt.debug_log)
    return {}


# name -> (stage, function)
BUILTINS = {
    "power": ("read", power),
    "cpufreq": ("read", cpufreq),
    "price": ("score", price),
    "co2": ("score", co2),
    "combine": ("score", combine),
    "cpu_freq": ("act", cpu_freq),
    "cpu_thermo": ("act", cpu_thermo),
    "gpu_power": ("act", gpu_power),
    "db_log": ("record", db_log),
    "state_json": ("record", state_json),
}
