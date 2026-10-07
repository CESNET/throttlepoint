import os
import configparser
import sys
from pathlib import Path
import sqlite3 
import argparse

from .utils.create_log_db import create_log_db
from .utils.controller import run_evaluation
from .pipeline import PipelineError
from .utils.cron_control import enable_cron, disable_cron

# [MODE] control_mode is the SINGLE user-facing switch. It expands into the
# internal regulator flags below (the user never sets these directly):
#   temperature -> CPU thermal control + NVIDIA GPU power regulation
#   co2         -> CPU price/CO2 frequency control only
# CPU regulation is mutually exclusive (temperature OR price), so a mode can
# never enable both CPU regulators at once.
CONTROL_MODE_PRESETS = {
    "temperature": {"ENABLE_TEMP_CPU": "yes", "ENABLE_PRICE_CO2_CPU": "no", "ENABLE_TEMP_GPU": "yes"},
    "co2":         {"ENABLE_TEMP_CPU": "no",  "ENABLE_PRICE_CO2_CPU": "yes", "ENABLE_TEMP_GPU": "no"},
}


CONFIG_PATH = "/etc/hpc_eff/config.ini"
if not os.path.isfile(CONFIG_PATH):
    CONFIG_PATH = "src/hpc_eff/config.ini.example"

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

DB_PATH_STR = config.get("logging", "db_path", fallback="history.db")
DB_PATH = Path(DB_PATH_STR).resolve()

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Always run: creates the DB if missing and migrates an existing one
# (adds any columns added to the schema since the DB was created).
create_log_db(DB_PATH)

conn = sqlite3.connect(DB_PATH_STR)
conn.execute("PRAGMA journal_mode=WAL;")

power_cmd = config.get("SYSTEM", "POWERREADINGCMD", fallback="")
static_context = {
    "score_name": config.get("SYSTEM", "SCORENAME", fallback="unknown"),
    "score_value": config.getfloat("SYSTEM", "SCORE", fallback=None),
    "power_cmd": power_cmd,
    # "plugins" (the active plugin names per stage) is filled in by the pipeline
    "plugins": {},
}

debug = config.get("SYSTEM", "DEBUG", fallback="no").lower() == "yes"

def debug_log(message):
    """Log message if debugging is enabled."""
    if debug:
        print(f"[DEBUG] {message}")

def resolve_control_mode(config):
    """Expand [MODE] control_mode into the internal regulator flags.

    control_mode is the only user-facing switch (temperature|co2). It is
    required: a missing or unknown value aborts the run rather than silently
    doing nothing. The expanded flags are written to an internal [FEATURES]
    section consumed by the controller.
    """
    mode = config.get("MODE", "control_mode", fallback=None)
    if mode:
        mode = mode.strip().lower()
    elif config.has_section("PIPELINE"):
        # An explicit [PIPELINE] replaces the control_mode presets entirely.
        debug_log("[PIPELINE] present and no control_mode: using the pipeline as configured")
        return
    if config.has_section("PIPELINE"):
        debug_log("[PIPELINE] takes precedence over control_mode; control_mode is ignored")
    preset = CONTROL_MODE_PRESETS.get(mode)
    if preset is None:
        valid = "|".join(CONTROL_MODE_PRESETS)
        sys.exit(
            f"Config error: [MODE] control_mode must be one of: {valid} "
            f"(got {mode!r})."
        )
    if not config.has_section("FEATURES"):
        config.add_section("FEATURES")
    for key, value in preset.items():
        config.set("FEATURES", key, value)
    debug_log(f"control_mode='{mode}' applied -> FEATURES {preset}")

def main():
    debug_log("Starting HPC efficiency evaluator...")
    
    parser = argparse.ArgumentParser(prog="hpc-eff", description="HPC efficiency evaluator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true", help="Enable cronjob (/etc/cron.d/hpc-eff) to run every 10 minutes")
    group.add_argument("--disable", action="store_true", help="Disable cronjob and remove /etc/cron.d/hpc-eff")
    parser.add_argument("--cron-path", default="/etc/cron.d/hpc-eff", help="Path to cron file to create/remove")
    parser.add_argument("--cron-interval", type=int, default=10, help="Interval in minutes for cron schedule (1-60)")

    args, _ = parser.parse_known_args()

    if args.enable:
        try:
            enable_cron(cron_path=args.cron_path, command="/usr/bin/hpc-eff", interval_minutes=args.cron_interval)
            print(f"Cronjob enabled at {args.cron_path}")
        except PermissionError:
            print("Permission denied: enabling cron requires root. Run with sudo.")
            raise
        return

    if args.disable:
        try:
            disable_cron(cron_path=args.cron_path)
            print(f"Cronjob disabled and {args.cron_path} removed (if existed)")
        except PermissionError:
            print("Permission denied: disabling cron requires root. Run with sudo.")
            raise
        return

    # Expand [MODE] control_mode into internal flags, then delegate
    resolve_control_mode(config)
    try:
        run_evaluation(conn, config, static_context, debug_log)
    except PipelineError as e:
        sys.exit(f"Config error: {e}")

if __name__ == "__main__":
    main()
    conn.close()
