class HmsAdapterError(Exception):
    """Base class for all HMS adapter errors."""


class ConflictError(HmsAdapterError):
    """Slot taken in HMS — do not retry, do not fall through to next write-back tier."""


class TransientError(HmsAdapterError):
    """Network error or 5xx — safe to retry or fall through to next tier."""


class AuthError(HmsAdapterError):
    """Credentials invalid or expired — surface to health check, do not retry indefinitely."""
