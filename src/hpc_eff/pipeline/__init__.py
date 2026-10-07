"""Plugin pipeline: read -> score -> act -> record. See docs/plugins.md."""

from .kernel import PipelineError, build_plan, run_pipeline

__all__ = ["PipelineError", "build_plan", "run_pipeline"]
