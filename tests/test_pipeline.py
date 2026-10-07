"""Tests for the plugin pipeline and the Wattnet parsing fix.

Run from the repo root:  PYTHONPATH=src python3 -m unittest discover -s tests -v

Nothing here touches the real system: cpupower/nvidia-smi calls, the network
and the config under /etc are all mocked or redirected into temp directories.
"""
import configparser
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hpc_eff.pipeline import PipelineError, build_plan, run_pipeline
from hpc_eff.pipeline import builtins as pb
from hpc_eff.utils.create_log_db import create_log_db
from hpc_eff.utils import co2_value as co2_mod

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"


def make_config(text):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.chmod(self.tmp, 0o755)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir(mode=0o755)
        os.chmod(self.plugins, 0o755)

    def plugin(self, name, body, mode=0o755):
        path = self.plugins / name
        path.write_text("#!/usr/bin/env python3\n" + body)
        os.chmod(path, mode)
        return str(path)

    def example(self, name):
        dest = self.plugins / name
        shutil.copy(EXAMPLES / name, dest)
        os.chmod(dest, 0o755)
        return str(dest)


def quiet(_message):
    pass


class WattnetParsingTest(unittest.TestCase):
    """fetch_wattnet_24h must treat the NEWEST hour as 'current'."""

    def test_current_is_newest_hour(self):
        end = datetime.now(timezone.utc).replace(microsecond=0)
        samples = []
        for i in range(96):                      # 96 x 15 min = 24 h, rising 100 -> 195
            ts = (end - timedelta(minutes=15 * (95 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
            samples.append([ts, 100 + i])
        response = mock.Mock()
        response.json.return_value = [{"series": [{"values": samples}]}]
        response.raise_for_status.return_value = None
        cfg = make_config("[CO2_API]\nTYPE=wattnet\nWATTNET_API_KEY=x\n")

        with mock.patch.object(co2_mod.requests, "get", return_value=response):
            history, current, _median, grade = co2_mod.co2_value(cfg)

        self.assertEqual(len(history), 24)
        self.assertEqual(current, history[0])
        self.assertGreater(current, 190)         # newest hour (was ~102 before the fix)
        self.assertEqual(grade, 10)


class CombineRatingsTest(unittest.TestCase):
    def cfg(self, rating_type):
        return make_config(f"[aggregation]\nrating_type={rating_type}\n")

    def test_co2_only(self):
        self.assertEqual(pb.combine_ratings(2, 9, self.cfg("co2"), quiet), 9)

    def test_co2_falls_back_to_price_then_neutral(self):
        self.assertEqual(pb.combine_ratings(4, None, self.cfg("co2"), quiet), 4)
        self.assertEqual(pb.combine_ratings(None, None, self.cfg("co2"), quiet), 5)

    def test_existing_types_unchanged(self):
        self.assertEqual(pb.combine_ratings(2, 9, self.cfg("max"), quiet), 9)
        self.assertEqual(pb.combine_ratings(2, 9, self.cfg("price"), quiet), 2)
        self.assertEqual(pb.combine_ratings(5, 10, self.cfg("average"), quiet), 7)   # (0.6*5+0.4*10)/1


class PlanTest(TempDirCase):
    def names(self, cfg):
        plan = build_plan(cfg)
        return {stage: [p.name for p in plugins] for stage, plugins in plan.items()}

    def test_default_co2_mode(self):
        cfg = make_config("[FEATURES]\nENABLE_TEMP_CPU=no\nENABLE_PRICE_CO2_CPU=yes\nENABLE_TEMP_GPU=no\n")
        self.assertEqual(self.names(cfg), {
            "read": ["power", "cpufreq"], "score": ["price", "co2", "combine"],
            "act": ["cpu_freq"], "record": ["db_log", "state_json"]})

    def test_default_temperature_mode(self):
        cfg = make_config("[FEATURES]\nENABLE_TEMP_CPU=yes\nENABLE_PRICE_CO2_CPU=no\nENABLE_TEMP_GPU=yes\n")
        self.assertEqual(self.names(cfg), {
            "read": [], "score": [], "act": ["cpu_thermo", "gpu_power"],
            "record": ["db_log", "state_json"]})

    def test_explicit_pipeline_record_defaults(self):
        cfg = make_config("[PIPELINE]\nscore = co2, combine\nact = cpu_freq, gpu_power\n")
        names = self.names(cfg)
        self.assertEqual(names["read"], [])
        self.assertEqual(names["record"], ["db_log", "state_json"])

    def test_executable_overrides_builtin_name(self):
        path = self.example("threshold_score.py")
        cfg = make_config(f"[PIPELINE]\nscore = co2\n[plugin:co2]\nexec = {path}\n")
        self.assertEqual(build_plan(cfg)["score"][0].exec, path)

    def test_config_errors_abort(self):
        bad = {
            "unknown key": "[PIPELINE]\nacts = cpu_freq\n",
            "unknown plugin": "[PIPELINE]\nact = nope\n",
            "wrong stage": "[PIPELINE]\nscore = cpu_freq\n",
            "relative path": "[PIPELINE]\nscore = x\n[plugin:x]\nexec = plugins/x.py\n",
            "missing file": "[PIPELINE]\nscore = x\n[plugin:x]\nexec = /nonexistent/x.py\n",
            "bad timeout": "[PIPELINE]\nscore = co2\n[plugin:co2]\ntimeout = soon\n",
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(PipelineError):
                build_plan(make_config(text))

    def test_unsafe_permissions_rejected(self):
        path = self.plugin("w.py", "pass\n", mode=0o775)             # group-writable
        with self.assertRaises(PipelineError):
            build_plan(make_config(f"[PIPELINE]\nscore = w\n[plugin:w]\nexec = {path}\n"))
        path = self.plugin("w2.py", "pass\n", mode=0o755)
        os.chmod(self.plugins, 0o777)                                  # world-writable dir
        self.addCleanup(os.chmod, self.plugins, 0o755)
        with self.assertRaises(PipelineError):
            build_plan(make_config(f"[PIPELINE]\nscore = w2\n[plugin:w2]\nexec = {path}\n"))

    def test_not_executable_rejected(self):
        path = self.plugin("n.py", "pass\n", mode=0o644)
        with self.assertRaises(PipelineError):
            build_plan(make_config(f"[PIPELINE]\nscore = n\n[plugin:n]\nexec = {path}\n"))


class RunPipelineTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "history.db"
        create_log_db(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.state = self.tmp / "state.json"
        self.logs = []

    def run_cfg(self, text, **patches):
        cfg = make_config(f"[logging]\nstate_json_path={self.state}\nhistory_length=5\n" + text)
        static = {"score_name": "t", "score_value": 1.0, "power_cmd": "", "plugins": {}}
        co2 = patches.get("co2", (list(range(24)), 300, 150, 7))
        co2_patch = {"side_effect": co2} if isinstance(co2, Exception) else {"return_value": co2}
        with mock.patch.object(pb, "co2_value", **co2_patch), \
             mock.patch.object(pb, "set_cpu_freq", return_value=2200000) as set_freq:
            run_pipeline(self.conn, cfg, static, self.logs.append)
        self.set_freq = set_freq

    def entry(self):
        return json.loads(self.state.read_text())["current"]

    def row(self):
        self.conn.row_factory = sqlite3.Row
        return dict(self.conn.execute("SELECT * FROM hpc_eff_log").fetchone())

    def test_co2_only_pipeline_fetches_no_price(self):
        with mock.patch.object(pb, "get_current_energy_price", side_effect=AssertionError("price fetched")):
            self.run_cfg("[PIPELINE]\nscore = co2\nact = cpu_freq\n")
        self.set_freq.assert_called_once()
        self.assertEqual(self.set_freq.call_args[0][0], 7)       # rating = CO2 grade
        entry = self.entry()
        self.assertEqual(entry["rating"], 7)
        self.assertEqual(entry["co2_current"], 300)
        self.assertEqual(entry["selected_freq_khz"], 2200000)
        self.assertNotIn("price", entry)
        self.assertNotIn("plugin_errors", entry)
        self.assertEqual(self.row()["rating"], 7)
        self.assertEqual(self.row()["freq_max"], 2200000)
        static = json.loads(self.state.read_text())["static"]
        self.assertEqual(static["plugins"]["score"], ["co2"])

    def test_combine_after_co2_still_applies_aggregation(self):
        with mock.patch.object(pb, "get_current_energy_price", side_effect=AssertionError("price fetched")):
            self.run_cfg("[aggregation]\nrating_type=max\n[PIPELINE]\nscore = co2, combine\nact = cpu_freq\n")
        self.assertEqual(self.set_freq.call_args[0][0], 7)       # no price plugin: CO2 passes through

    def test_co2_failure_falls_back_to_neutral_rating(self):
        self.run_cfg("[PIPELINE]\nscore = co2\nact = cpu_freq\n", co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 5)
        self.assertEqual(self.entry()["co2_grade"], "unknown")

    def test_executable_score_plugin_sets_rating(self):
        path = self.example("threshold_score.py")
        self.run_cfg(f"[PIPELINE]\nscore = co2, threshold_score\nact = cpu_freq\n"
                     f"[plugin:threshold_score]\nexec = {path}\nlow = 100\nhigh = 500\n")
        # 300 g/kWh between 100 and 500 -> halfway -> 1 + round(4.5) = 6
        self.assertEqual(self.set_freq.call_args[0][0], 6)
        self.assertIn("rating 6", self.entry()["summary"])

    def test_executable_action_plugin_receives_rating(self):
        path = self.example("echo_action.py")
        self.run_cfg(f"[PIPELINE]\nscore = co2, combine\nact = echo_action\n"
                     f"[plugin:echo_action]\nexec = {path}\n")
        self.assertEqual(self.entry()["action_echo"], "would act on rating 7")
        self.set_freq.assert_not_called()

    def test_failing_plugins_are_isolated(self):
        crash = self.plugin("crash.py", "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
        slow = self.plugin("slow.py", "import time\ntime.sleep(5)\n")
        junk = self.plugin("junk.py", "print('not json')\n")
        wild = self.plugin("wild.py", "import json\nprint(json.dumps({'set': {'rating': 11}}))\n")
        future = self.plugin("future.py", "import json\nprint(json.dumps({'version': 2}))\n")
        names = ["crash", "slow", "junk", "wild", "future"]
        paths = dict(zip(names, [crash, slow, junk, wild, future]))
        sections = "".join(f"[plugin:{n}]\nexec = {p}\ntimeout = 0.5\n" for n, p in paths.items())
        self.run_cfg(f"[PIPELINE]\nscore = co2, combine, {', '.join(names)}\nact = cpu_freq\n" + sections)
        errors = self.entry()["plugin_errors"]
        self.assertEqual(len(errors), 5)
        self.assertIn("exit code 3: boom", errors[0])
        self.assertIn("timed out", errors[1])
        self.assertIn("not valid JSON", errors[2])
        self.assertIn("rating must be", errors[3])
        self.assertIn("unsupported contract version", errors[4])
        # the evaluation still completed with the rating computed before the failures
        self.assertEqual(self.set_freq.call_args[0][0], 7)
        self.assertEqual(self.row()["rating"], 7)

    def test_plugin_environment_is_scrubbed(self):
        probe = self.plugin(
            "env.py",
            "import json, os\nprint(json.dumps({'state': {'leak': os.environ.get('HPC_EFF_SECRET', 'none')}}))\n")
        with mock.patch.dict(os.environ, {"HPC_EFF_SECRET": "hunter2"}):
            self.run_cfg(f"[PIPELINE]\nscore = env\n[plugin:env]\nexec = {probe}\n")
        self.assertEqual(self.entry()["leak"], "none")

    def test_default_plan_still_runs_price_and_cpu_freq(self):
        with mock.patch.object(pb, "get_current_energy_price", return_value=2000.0), \
             mock.patch.object(pb, "get_averages_year", return_value=[1500.0] * 12), \
             mock.patch.object(pb, "get_power_reading", return_value={"instantaneous": 321}), \
             mock.patch.object(pb, "get_cpu_frequency", return_value=2400):
            self.run_cfg("[SYSTEM]\nPOWERREADINGCMD=true\n"
                         "[FEATURES]\nENABLE_TEMP_CPU=no\nENABLE_PRICE_CO2_CPU=yes\nENABLE_TEMP_GPU=no\n"
                         "[aggregation]\nrating_type=average\n")
        entry = self.entry()
        for key in ("price", "rating_price", "rating_co2", "rating", "power_w", "cpu_freq_current",
                    "selected_freq_khz", "co2_current", "summary", "action_price"):
            self.assertIn(key, entry)
        self.assertEqual(entry["power_w"], 321)
        self.assertEqual(self.row()["power_w"], 321)


if __name__ == "__main__":
    unittest.main()
