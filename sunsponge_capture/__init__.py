"""SunSponge Capture — rested-state website screenshot batch tool."""

from .capture import (
    RestedCaptureError,
    RestedCaptureManager,
    build_capture_plan,
)

__version__ = "0.1.0"
__all__ = [
    "RestedCaptureError",
    "RestedCaptureManager",
    "build_capture_plan",
    "__version__",
]
