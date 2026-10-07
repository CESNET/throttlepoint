#!/usr/bin/env python3
"""Example action plugin: reports what it would do for the rating. Changes nothing.

Copy it as a starting point for a real action (lower frequencies, reconfigure
cpufreqd, call a site tool ...). It receives the rating and everything else
gathered so far; whatever it returns under "state" lands in state.json.

Pipeline use:
    [PIPELINE]
    act = cpu_freq, echo_action

    [plugin:echo_action]
    exec = /etc/hpc_eff/plugins/echo_action.py
"""
import json
import sys

request = json.load(sys.stdin)
rating = request["context"].get("rating")
print(json.dumps({
    "version": 1,
    "state": {"action_echo": f"would act on rating {rating}"},
}))
