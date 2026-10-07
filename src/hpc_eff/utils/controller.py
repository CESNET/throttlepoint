"""Controller entry point: runs one evaluation through the plugin pipeline.

The evaluation itself lives in `hpc_eff.pipeline` (read -> score -> act ->
record, each step a plugin; see docs/plugins.md). This module keeps the
long-standing `run_evaluation` entry point.

Without a [PIPELINE] section, the plan is derived from the internal [FEATURES]
flags that `main.resolve_control_mode` populates from `[MODE] control_mode`:
- ENABLE_PRICE_CO2_CPU : price/CO2 -> rating -> max-frequency cap
- ENABLE_TEMP_CPU  : temperature -> hysteresis-based max-frequency control
- ENABLE_TEMP_GPU  : temperature -> power limiting for NVIDIA GPUs
"""

from ..pipeline import run_pipeline


def run_evaluation(conn, config, static_context: dict, debug_log):
    """Run the full evaluation pipeline and apply actions depending on config.

    Args:
        conn: sqlite3.Connection for logging
        config: configparser.ConfigParser loaded in `main.py`
        static_context: dict with static values (score_name, score_value)
        debug_log: callable for debug logging

    Raises hpc_eff.pipeline.PipelineError if the pipeline configuration is
    invalid. Plugin failures at run time never raise: they are logged and the
    evaluation continues.
    """
    debug_log("Controller: starting evaluation")
    run_pipeline(conn, config, static_context, debug_log)
    debug_log("Controller: evaluation finished")
