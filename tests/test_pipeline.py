"""Tests for the plugin pipeline and the Wattnet parsing fix.

Run from the repo root:  PYTHONPATH=src python3 -m unittest discover -s tests -v

Nothing here touches the real system: cpupower/nvidia-smi calls, the network
and the config under /etc are all mocked or redirected into temp directories.
"""
import base64
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
        response = mock.Mock(status_code=200, is_redirect=False)
        response.json.return_value = [{"series": [{"values": samples}]}]
        response.raise_for_status.return_value = None
        cfg = make_config("[CO2_API]\nTYPE=wattnet\n")

        with mock.patch.object(co2_mod, "_wattnet_token", return_value="x"), \
             mock.patch.object(co2_mod.requests, "get", return_value=response):
            history, current, _median, grade = co2_mod.co2_value(cfg)

        self.assertEqual(len(history), 24)
        self.assertEqual(current, history[0])
        self.assertGreater(current, 190)         # newest hour (was ~102 before the fix)
        self.assertEqual(grade, 10)


class WattnetSeriesTest(unittest.TestCase):
    """Live responses split one range into final, pending and preview series."""

    def test_merges_all_series_and_groups_by_clock_hour(self):
        end = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)
        points = [(end - timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(97)][::-1]
        final = [[ts, 400] for ts in points[:81]]                 # .. 08:30
        pending = [[ts, 320] for ts in points[81:94]]             # 08:45 .. 11:45
        preview = [[ts, 300] for ts in points[94:]]               # 12:00 .. 12:30
        response = mock.Mock(status_code=200, is_redirect=False)
        response.json.return_value = [{"series": [
            {"valid": True, "zone_status": "complete", "values": final},
            {"valid": False, "zone_status": "complete", "values": pending},
            {"valid": False, "zone_status": "preview", "values": preview}]}]
        cfg = make_config("[CO2_API]\n")
        with mock.patch.object(co2_mod, "_wattnet_token", return_value="x"), \
             mock.patch.object(co2_mod.requests, "get", return_value=response):
            history, current = co2_mod.fetch_wattnet_24h(cfg)
        self.assertEqual(len(history), 24)
        self.assertEqual(current, 300)                            # 12:00-12:30 preview hour, not 08:30
        self.assertEqual(history[1], 320)                         # 11:00 hour, pending
        self.assertEqual(history[4], round((400 * 3 + 320) / 4))  # 08:00 hour: final 08:00-08:30 + pending 08:45

    def test_final_value_wins_on_overlap(self):
        ts = [(datetime(2026, 10, 13, tzinfo=timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
              for h in range(24)]
        response = mock.Mock(status_code=200, is_redirect=False)
        response.json.return_value = [{"series": [
            {"valid": False, "values": [[ts[0], 999]]},
            {"valid": True, "values": [[t, 100] for t in ts]}]}]
        with mock.patch.object(co2_mod, "_wattnet_token", return_value="x"), \
             mock.patch.object(co2_mod.requests, "get", return_value=response):
            _, current = co2_mod.fetch_wattnet_24h(make_config("[CO2_API]\n"))
        self.assertEqual(current, 100)


class WattnetTokenTest(TempDirCase):
    """Login, cache and refresh of the 24 h Wattnet token."""

    def cfg(self, extra=""):
        return make_config(f"[CO2_API]\nTYPE=wattnet\nWATTNET_EMAIL=bot@x\nWATTNET_PASSWORD=pw\n"
                           f"WATTNET_TOKEN_CACHE={self.tmp / 'tok.json'}\n" + extra)

    def login_response(self, token="T1", hours=24):
        r = mock.Mock(status_code=200)
        r.json.return_value = {"access_token": token,
                               "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()}
        return r

    def test_login_then_cache_hit(self):
        with mock.patch.object(co2_mod.requests, "post", return_value=self.login_response()) as post:
            self.assertEqual(co2_mod._wattnet_token(self.cfg()), "T1")
            self.assertEqual(co2_mod._wattnet_token(self.cfg()), "T1")
        post.assert_called_once()                                   # second call served from cache
        self.assertEqual(post.call_args.kwargs["json"], {"email": "bot@x", "password": "pw"})
        self.assertEqual(os.stat(self.tmp / "tok.json").st_mode & 0o777, 0o600)

    def test_near_expiry_logs_in_again(self):
        with mock.patch.object(co2_mod.requests, "post",
                               side_effect=[self.login_response("OLD", hours=0.1), self.login_response("NEW")]):
            co2_mod._wattnet_token(self.cfg())
            self.assertEqual(co2_mod._wattnet_token(self.cfg()), "NEW")   # 6 min left < 10 min margin

    def test_expiry_from_jwt_when_response_has_none(self):
        exp = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
        body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        r = mock.Mock(status_code=200)
        r.json.return_value = {"token": f"h.{body}.s"}
        with mock.patch.object(co2_mod.requests, "post", return_value=r):
            co2_mod._wattnet_token(self.cfg())
        cached = json.loads((self.tmp / "tok.json").read_text())
        self.assertEqual(int(datetime.fromisoformat(cached["expires_at"]).timestamp()), exp)

    def test_bad_login_is_a_clear_error(self):
        with mock.patch.object(co2_mod.requests, "post", return_value=mock.Mock(status_code=401)):
            with self.assertRaisesRegex(ValueError, "login rejected"):
                co2_mod._wattnet_token(self.cfg())

    def test_missing_login_is_a_clear_error(self):
        with mock.patch.object(co2_mod.requests, "post", side_effect=AssertionError("login attempted")):
            with self.assertRaisesRegex(ValueError, "WATTNET_EMAIL and WATTNET_PASSWORD"):
                co2_mod._wattnet_token(make_config("[CO2_API]\nWATTNET_EMAIL=bot@x\n"))

    def test_401_on_data_call_relogs_once(self):
        denied = mock.Mock(status_code=401, is_redirect=False)
        ok = mock.Mock(status_code=200, is_redirect=False)
        end = datetime.now(timezone.utc)
        ok.json.return_value = [{"series": [{"values": [
            [(end - timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 300] for i in range(96)]}]}]
        with mock.patch.object(co2_mod.requests, "post",
                               side_effect=[self.login_response("A"), self.login_response("B")]), \
             mock.patch.object(co2_mod.requests, "get", side_effect=[denied, ok]) as get:
            co2_mod.fetch_wattnet_24h(self.cfg())
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer B")


class WattnetRejectedTokenTest(TempDirCase):
    """Wattnet answers a rejected token with a 302 to its login page, not 401."""

    def cfg(self):
        return make_config("[CO2_API]\nWATTNET_EMAIL=bot@x\nWATTNET_PASSWORD=pw\n"
                           f"WATTNET_TOKEN_CACHE={self.tmp / 'tok.json'}\n")

    def login(self, token):
        r = mock.Mock(status_code=200)
        r.json.return_value = {"access_token": token,
                               "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()}
        return r

    def data_ok(self):
        r = mock.Mock(status_code=200, is_redirect=False)
        end = datetime.now(timezone.utc)
        r.json.return_value = [{"series": [{"values": [
            [(end - timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 300] for i in range(96)]}]}]
        return r

    def test_redirect_to_login_triggers_relogin(self):
        redirect = mock.Mock(status_code=302, is_redirect=True)
        with mock.patch.object(co2_mod.requests, "post", side_effect=[self.login("OLD"), self.login("NEW")]) as post, \
             mock.patch.object(co2_mod.requests, "get", side_effect=[redirect, self.data_ok()]) as get:
            _, current = co2_mod.fetch_wattnet_24h(self.cfg())
        self.assertEqual(current, 300)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer NEW")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        # the new token replaced the dead one in the cache
        self.assertEqual(json.loads((self.tmp / "tok.json").read_text())["token"], "NEW")

    def test_fresh_token_still_rejected_is_a_clear_error(self):
        redirect = mock.Mock(status_code=302, is_redirect=True)
        with mock.patch.object(co2_mod.requests, "post", side_effect=[self.login("A"), self.login("B")]), \
             mock.patch.object(co2_mod.requests, "get", side_effect=[redirect, redirect]):
            with self.assertRaisesRegex(ValueError, "rejected a freshly issued token"):
                co2_mod.fetch_wattnet_24h(self.cfg())

    def test_html_body_is_a_clear_error(self):
        html = mock.Mock(status_code=200, is_redirect=False)
        html.headers = {"content-type": "text/html"}
        html.json.side_effect = ValueError("Expecting value")
        with mock.patch.object(co2_mod.requests, "post", return_value=self.login("A")), \
             mock.patch.object(co2_mod.requests, "get", return_value=html):
            with self.assertRaisesRegex(ValueError, "non-JSON data \\(text/html\\)"):
                co2_mod.fetch_wattnet_24h(self.cfg())


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

    def seed_history(self, *entries):
        """entries: (minutes_ago, co2_grade, co2_fallback), newest first."""
        now = datetime.now(timezone.utc)
        history = [{"timestamp": (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "co2_grade": g, "co2_fallback": fb} for m, g, fb in entries]
        self.state.write_text(json.dumps({"static": {}, "history": history, "current": history[0]}))

    CO2_ONLY = "[PIPELINE]\nscore = co2\nact = cpu_freq\n"

    def test_co2_failure_without_history_does_not_slow_down(self):
        self.run_cfg(self.CO2_ONLY, co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 1)
        entry = self.entry()
        self.assertEqual(entry["co2_grade"], "unknown")
        self.assertTrue(entry["co2_fallback"])
        self.assertIn("no CO2 data within max age", entry["summary"])

    def test_co2_failure_reuses_recent_grade(self):
        self.seed_history((30, 9, False))
        self.run_cfg(self.CO2_ONLY, co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 9)
        self.assertIn("reusing grade 9 from 30 min ago", self.entry()["summary"])

    def test_co2_failure_ignores_grade_older_than_max_age(self):
        self.seed_history((121, 9, False))
        self.run_cfg(self.CO2_ONLY, co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 1)

    def test_co2_max_age_is_configurable(self):
        self.seed_history((170, 9, False))
        self.run_cfg("[CO2_API]\nMAX_AGE_MIN=180\n" + self.CO2_ONLY, co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 9)

    def test_co2_fallback_grade_is_not_chained(self):
        # 10 min ago was itself a reuse of a grade now 130 min old: must not extend it
        self.seed_history((10, 9, True), (130, 9, False))
        self.run_cfg(self.CO2_ONLY, co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 1)

    def test_co2_failure_with_price_still_falls_back_to_price(self):
        with mock.patch.object(pb, "get_current_energy_price", return_value=3000), \
             mock.patch.object(pb, "get_averages_year", return_value=[1000] * 12), \
             mock.patch.object(pb, "classify_price_by_median", return_value=("expensive", 8)):
            self.run_cfg("[aggregation]\nrating_type=average\n[PIPELINE]\n"
                         "score = price, co2, combine\nact = cpu_freq\n", co2=RuntimeError("401"))
        self.assertEqual(self.set_freq.call_args[0][0], 8)        # price only, no fake CO2 = 1

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
