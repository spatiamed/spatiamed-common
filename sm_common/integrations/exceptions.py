class HmsAdapterError(Exception):
    """Base class for all HMS adapter errors.

    ``landed=True`` marks a refusal raised AFTER the HMS already holds our
    appointment — a read-back missing a field we need, or more than one
    appointment already carrying our booking identifier. Retrying is still
    pointless, but a caller must not read it as "the write did not land".

    ``code`` is a stable machine-readable name for a refusal a caller handles
    specifically (``None`` for the general types).
    """

    code: str | None = None

    def __init__(self, *args: object, landed: bool = False) -> None:
        super().__init__(*args)
        self.landed = landed


class ConflictError(HmsAdapterError):
    """Slot taken in HMS — do not retry, do not fall through to next write-back tier."""


class TransientError(HmsAdapterError):
    """Network error or 5xx — safe to retry or fall through to next tier.

    ``status_code`` is the HTTP status when the error is an HTTP answer (``None`` for a
    transport failure).
    """

    def __init__(
        self, *args: object, landed: bool = False, status_code: int | None = None
    ) -> None:
        super().__init__(*args, landed=landed)
        self.status_code = status_code


class AuthError(HmsAdapterError):
    """Credentials invalid or expired — surface to health check, do not retry indefinitely."""


class WriteNotSupported(HmsAdapterError):  # noqa: N818
    """The vendor has no route for this write (404/405 on create). Terminal — retrying changes nothing."""


class VendorRejected(HmsAdapterError):  # noqa: N818
    """The vendor refused the payload (validation error, even under HTTP 200). Terminal."""


class SearchNotSupported(HmsAdapterError):  # noqa: N818
    """This vendor cannot run the requested patient search (e.g. name + DOB). Terminal."""


class ConfigError(HmsAdapterError):
    """The integration config is missing or inconsistent (e.g. no appointment service for a provider). Terminal."""


class NotFound(HmsAdapterError):  # noqa: N818
    """The HMS has no such record (HTTP 404). Terminal."""


class EncounterOutsideVisit(VendorRejected):  # noqa: N818
    """The encounter time is outside its visit's start/stop range. Terminal.

    Bahmni answers this with a bare HTTP 500 (an HTML page, no message), which would
    otherwise read as transient. Exactly the visit start is accepted; one second before
    it is not, so a clock skew between caller and server is enough to trigger it.
    Re-read the visit and resubmit with a time inside it.
    """

    code = "encounter_outside_visit"


class EncounterMatchAmbiguous(VendorRejected):  # noqa: N818
    """Bahmni could not pick the encounter to write into. Terminal.

    A ``bahmniencounter`` write without an ``encounterUuid`` is merged into the
    matching encounter of the same type, provider and visit; once two such encounters
    exist Bahmni answers a bare HTTP 500 ("More than one encounter matches the
    criteria") to every such write. Only a write that names its own encounter uuid
    gets through.
    """

    code = "encounter_match_ambiguous"


class DocumentFilingFailed(VendorRejected):  # noqa: N818
    """The file upload succeeded but attaching it to a document encounter failed.

    ``uploaded_url`` lets a caller resume with ``file_uploaded_document`` instead of
    re-uploading. ``landed`` is True when the encounter may already exist (5xx or a
    malformed success), so a resume must first check for a duplicate.
    """

    def __init__(
        self, message: str, *, uploaded_url: str, cause: str = "", landed: bool = False
    ) -> None:
        super().__init__(message, landed=landed)
        self.uploaded_url = uploaded_url
        self.cause = cause
