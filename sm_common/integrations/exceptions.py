class HmsAdapterError(Exception):
    """Base class for all HMS adapter errors."""


class ConflictError(HmsAdapterError):
    """Slot taken in HMS — do not retry, do not fall through to next write-back tier."""


class TransientError(HmsAdapterError):
    """Network error or 5xx — safe to retry or fall through to next tier."""


class AuthError(HmsAdapterError):
    """Credentials invalid or expired — surface to health check, do not retry indefinitely."""


class WriteNotSupported(HmsAdapterError):  # noqa: N818
    """The vendor has no route for this write (404/405 on create). Terminal — retrying changes nothing."""


class VendorRejected(HmsAdapterError):  # noqa: N818
    """The vendor refused the payload (validation error, even under HTTP 200). Terminal."""
