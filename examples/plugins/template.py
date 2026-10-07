#!/usr/bin/env python3
"""Plugin starter. Copy to /etc/hpc_eff/plugins/, chmod 755, owned by root.

The controller writes one JSON request to stdin and reads one JSON response
from stdout (full contract: docs/plugins.md). Exit non-zero, or send an
"error" string, to report a failure; the evaluation carries on without you.

Request:  {"version": 1, "stage": "...", "plugin": "...",
           "config": {<your [plugin:NAME] keys>},
           "context": {<everything produced so far, e.g. "rating">},
           "state": {<this run's state.json entry so far>}}
Response: every field optional.
    "set":   values for later plugins (and the DB), e.g. {"rating": 6}
    "state": values for state.json; "summary" text from several plugins is joined
"""
import json
import sys

request = json.load(sys.stdin)
options = request["config"]                    # strings, as written in config.ini
rating = request["context"].get("rating")

# ... your logic here: score plugins return {"rating": 1..10} under "set",
#     action plugins act on `rating` and report under "state".

print(json.dumps({
    "version": 1,
    "set": {},
    "state": {"summary": f"template saw rating {rating}"},
}))
