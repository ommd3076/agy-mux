"""Browser automation engine package."""

from core.engine import (
    BrowserEngine,
    CDPError,
    EngineError,
    TabSession,
    Target,
    TargetNotFoundError,
)
from core.browser_skills import (
    BoundedMetricQueue,
    BrowserSkills,
    ElementSnapshot,
    redact_headers,
)

__all__ = [
    "BoundedMetricQueue",
    "BrowserEngine",
    "BrowserSkills",
    "CDPError",
    "ElementSnapshot",
    "EngineError",
    "TabSession",
    "Target",
    "TargetNotFoundError",
    "redact_headers",
]
