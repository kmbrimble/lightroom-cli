"""Programmatic image-quality metrics (exposure / focus / tilt / noise). See docs/quality.md."""
__version__ = "2.0.0"

from .core import SCENES, SCHEMA, analyze, default_thresholds, finalize

__all__ = ["__version__", "analyze", "default_thresholds", "finalize", "SCHEMA", "SCENES"]
