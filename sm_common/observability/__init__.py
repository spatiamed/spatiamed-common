"""PHI-safe error tracking. See init_error_tracking."""

from sm_common.observability.init import init_error_tracking
from sm_common.observability.pii import redact_text
from sm_common.observability.scrub import scrub_breadcrumb, scrub_event
from sm_common.observability.traces import (
    TRACES_SAMPLE_RATE_CEILING,
    parse_sample_rate,
    scrub_transaction,
    strip_sql_literals,
)

__all__ = [
    "TRACES_SAMPLE_RATE_CEILING",
    "init_error_tracking",
    "parse_sample_rate",
    "redact_text",
    "scrub_breadcrumb",
    "scrub_event",
    "scrub_transaction",
    "strip_sql_literals",
]
