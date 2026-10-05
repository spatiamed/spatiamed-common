"""PHI-safe error tracking. See init_error_tracking."""

from sm_common.observability.init import init_error_tracking
from sm_common.observability.pii import redact_text
from sm_common.observability.scrub import scrub_breadcrumb, scrub_event

__all__ = ["init_error_tracking", "redact_text", "scrub_breadcrumb", "scrub_event"]
