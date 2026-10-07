#!/usr/bin/env python3
"""Example score plugin: rating from absolute gCO2/kWh thresholds.

Reads `co2_current` (set earlier by the built-in `co2` plugin) and maps it
linearly onto 1..10: <= `low` is 1 (run fast), >= `high` is 10 (throttle most).

Pipeline use:
    [PIPELINE]
    score = co2, threshold_score

    [plugin:threshold_score]
    exec = /etc/hpc_eff/plugins/threshold_score.py
    low = 150
    high = 450
"""
import json
import sys

request = json.load(sys.stdin)
options = request["config"]
low = float(options.get("low", 100))
high = float(options.get("high", 500))
current = request["context"].get("co2_current")

if current is None:
    # No data: say nothing, so the rating set by earlier plugins (or the neutral 5) stands.
    print(json.dumps({"version": 1, "set": {}, "state": {"summary": "threshold_score: no CO2 value"}}))
    sys.exit(0)

fraction = min(1.0, max(0.0, (float(current) - low) / (high - low)))
rating = 1 + int(fraction * 9 + 0.5)   # half-up, not banker's rounding
print(json.dumps({
    "version": 1,
    "set": {"rating": rating},
    "state": {"rating": rating, "summary": f"CO2 {current} g/kWh -> rating {rating} (thresholds {low:g}-{high:g})"},
}))
