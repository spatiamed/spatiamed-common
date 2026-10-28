"""Lab-system (Bahmni OpenELIS) client: HTTP servlet calls + read-only lab DB.

The lab system has no supported write API. Sample collection goes through the
internal ``/openelis/ajaxQueryXML`` servlet, which serves GET only:

* ``provider=SampleTypeTestsForSampleProvider&sampleId=`` returns the sample's
  current (non-cancelled) tests, wrapped as
  ``<fieldmessage><formfield><samples><sample><sampleType/><test/>...``;
* ``provider=TestUpdateWithAccessionNumberProvider`` sets the accession number
  and collection date, **cancels every test missing from ``typeAndTestIds``**,
  and answers HTTP 204 with an empty body. A malformed list is swallowed (the
  lab system answers 200 with an empty body), so only a 204 counts as "sent",
  and even a 204 must be verified against the lab database by the caller.

Rules this module enforces:

* every request carries the proxy guard header ``X-SM-Lab-Key``;
* only the two providers above can be called, never a caller-chosen one;
* the collect call is sent at most once per invocation (no re-login retry, no
  transport retry) and refuses, before any I/O, a test list that could cancel
  something: an empty list, a type without tests, a list read for another
  sample, or a type list that disagrees with the test map;
* XML is parsed with ``defusedxml`` (no DTDs, no entities).

Login: ``POST /openelis/ValidateLogin.do`` with the form fields ``loginName`` and
``password`` (the field names the Bahmni lab-feed authenticator posts), keeping
the ``JSESSIONID`` cookie. The lab system forwards server-side after both a good
and a bad login, so the status code alone does not tell them apart: success is a
200 (or a redirect away from the login page) that sets ``JSESSIONID`` and does
not render the login form.

Read-only SQL half: no lab endpoint maps an encounter to its samples, and the
collect call's success is only visible in the lab database. ``OpenElisSql`` runs
only the fixed, parameterised queries in ``LAB_QUERIES`` (never caller SQL);
``AsyncpgLabExecutor`` imports asyncpg lazily (extra ``sm-common[lab]``), runs
every query in a read-only transaction, and refuses a connection whose
``statement_timeout`` is off or above 5 s.

``collect()`` verifies in the DB that the accession was recorded, that an
accession event for THIS sample was published after a watermark, and that no
test was cancelled by the send (including one the lab feed added after our
pre-read). See ``OpenElisClient.collect``.

The library reads no environment variables and ships no default identifiers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as _safe_fromstring

from sm_common.integrations.adapters.bahmni_http import _cert_verification_failed
from sm_common.integrations.exceptions import (
    AuthError,
    ConfigError,
    ConflictError,
    HmsAdapterError,
    NotFound,
    TransientError,
    VendorRejected,
)

log = logging.getLogger(__name__)

GUARD_HEADER = "X-SM-Lab-Key"
_LOGIN_PATH = "/openelis/ValidateLogin.do"
_AJAX_PATH = "/openelis/ajaxQueryXML"
_COOKIE = "JSESSIONID"

PROVIDER_SAMPLE_TESTS = "SampleTypeTestsForSampleProvider"
PROVIDER_COLLECT = "TestUpdateWithAccessionNumberProvider"
_READ_PROVIDERS = frozenset({PROVIDER_SAMPLE_TESTS})
_WRITE_PROVIDERS = frozenset({PROVIDER_COLLECT})

# fullmatch, never match + "$": "$" also matches before a trailing newline.
_PREFIX_RE = re.compile(r"[A-Z]{1,3}")
_ACCESSION_RE = re.compile(r"[A-Z0-9]+")
# Defence in depth only: unreachable while the prefix is letters-only.
_LAB_DATENUM_RE = re.compile(r"\d{8}-")
_ACCESSION_MAX = 13
_LOGIN_MARKERS = ("loginpage", "validatelogin", "login.do")


def _is_login_location(location: str | None) -> bool:
    return bool(location) and any(m in (location or "").lower() for m in _LOGIN_MARKERS)


def _renders_login_form(resp: httpx.Response) -> bool:
    text = resp.text.lower()
    return 'name="loginname"' in text or "name='loginname'" in text


def _check_tls_opt_in(
    verify_tls: bool, allow_insecure_tls: bool, ca_bundle_pem: str | None
) -> None:
    if not verify_tls and not ca_bundle_pem and not allow_insecure_tls:
        raise ConfigError(
            "openelis: verify_tls=False requires allow_insecure_tls=True (explicit opt-in)"
        )


def _query(provider: str, params: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted provider plus params; a params ``provider`` key is refused."""
    if "provider" in params:
        raise ConfigError("openelis: params must not carry a provider key")
    return {**params, "provider": provider}


@dataclass(frozen=True)
class OpenElisConfig:
    base_url: str  # https://<host>; the paths are fixed
    username: str
    password: str
    guard_header_value: str  # sent as X-SM-Lab-Key on every call
    ro_dsn: str  # read-only lab DB DSN ("" until the SQL half needs it)
    accession_prefix: str  # 1-3 uppercase letters, never digits
    verify_tls: bool = True
    ca_bundle_pem: str | None = None
    timeout_s: float = field(default=30.0, repr=False)
    # verify_tls=False without a CA bundle needs this explicit opt-in (as BahmniHttp).
    allow_insecure_tls: bool = False

    def __post_init__(self) -> None:
        missing = [
            k
            for k in ("base_url", "username", "password", "guard_header_value")
            if not getattr(self, k)
        ]
        if missing:
            raise ConfigError(f"openelis config missing required keys: {', '.join(missing)}")
        if not isinstance(self.accession_prefix, str) or not _PREFIX_RE.fullmatch(
            self.accession_prefix
        ):
            raise ConfigError("openelis config: accession_prefix must be 1-3 uppercase letters")
        if not isinstance(self.verify_tls, bool):
            raise ConfigError("openelis config: verify_tls must be a boolean")
        if not isinstance(self.allow_insecure_tls, bool):
            raise ConfigError("openelis config: allow_insecure_tls must be a boolean")
        _check_tls_opt_in(self.verify_tls, self.allow_insecure_tls, self.ca_bundle_pem)

    def __repr__(self) -> str:  # never print secrets
        return (
            f"OpenElisConfig(base_url={self.base_url!r}, username={self.username!r}, "
            f"accession_prefix={self.accession_prefix!r}, verify_tls={self.verify_tls!r})"
        )

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any], creds: Mapping[str, Any]) -> OpenElisConfig:
        """Build from an integration's config + resolved secrets; unknown keys are ignored.

        Secrets (``username``, ``password``, ``guard_header_value``, ``ro_dsn``) come
        from ``creds``; everything else from ``cfg``.
        """
        cfg = cfg or {}
        creds = creds or {}
        verify_tls = cfg.get("verify_tls", True)
        if not isinstance(verify_tls, bool):
            raise ConfigError("openelis config: verify_tls must be a boolean")
        allow_insecure_tls = cfg.get("allow_insecure_tls", False)
        if not isinstance(allow_insecure_tls, bool):
            raise ConfigError("openelis config: allow_insecure_tls must be a boolean")
        return cls(
            base_url=str(cfg.get("base_url") or ""),
            username=str(creds.get("username") or ""),
            password=str(creds.get("password") or ""),
            guard_header_value=str(creds.get("guard_header_value") or ""),
            ro_dsn=str(creds.get("ro_dsn") or ""),
            accession_prefix=str(cfg.get("accession_prefix") or ""),
            verify_tls=verify_tls,
            ca_bundle_pem=cfg.get("ca_bundle_pem") or None,
            allow_insecure_tls=allow_insecure_tls,
        )


@dataclass(frozen=True)
class SampleTests:
    sample_id: int
    type_ids: tuple[int, ...]
    test_ids_by_type: Mapping[int, tuple[int, ...]]


class OpenElisHttp:
    """Session + guard header + error mapping for the lab system's servlet."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        guard_header_value: str,
        *,
        ca_bundle_pem: str | None = None,
        verify_tls: bool = True,
        allow_insecure_tls: bool = False,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not guard_header_value:
            raise ConfigError("openelis: guard_header_value is required")
        _check_tls_opt_in(verify_tls, allow_insecure_tls, ca_bundle_pem)
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cadata=ca_bundle_pem) if ca_bundle_pem else verify_tls
        )
        self._username = username
        self._password = password
        self._guard = {GUARD_HEADER: guard_header_value}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=verify,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )
        self._cookie: str | None = None
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kw)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            if _cert_verification_failed(exc):
                raise ConfigError("openelis: TLS certificate verification failed") from exc
            raise TransientError(f"openelis: transport error: {type(exc).__name__}") from exc
        except (httpx.UnsupportedProtocol, httpx.ProxyError) as exc:
            raise ConfigError(
                f"openelis: unusable connection config: {type(exc).__name__}"
            ) from exc

    async def _login(self, stale: str | None) -> str:
        """Establish a session; skip if another task already replaced the stale cookie."""
        async with self._login_lock:
            if self._cookie is not None and self._cookie != stale:
                return self._cookie
            self._client.cookies.clear()
            resp = await self._send(
                "POST",
                _LOGIN_PATH,
                data={"loginName": self._username, "password": self._password},
                headers=self._guard,
            )
            status = resp.status_code
            self._client.cookies.clear()
            if status in (401, 403):
                raise AuthError(f"openelis: login HTTP {status}")
            if status >= 500 or status == 429:
                raise TransientError(f"openelis: login HTTP {status}")
            if status == 200:
                ok = not _renders_login_form(resp)
            elif status in (301, 302, 303, 307, 308):
                ok = not _is_login_location(resp.headers.get("location"))
            else:
                ok = False
            cookie = resp.cookies.get(_COOKIE)
            if not ok or not cookie:
                raise AuthError("openelis: login failed")
            self._cookie = cookie
            return cookie

    def _headers(self, cookie: str) -> dict[str, str]:
        return {**self._guard, "Cookie": f"{_COOKIE}={cookie}"}

    @staticmethod
    def _wants_login(resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        return 300 <= resp.status_code < 400 and _is_login_location(resp.headers.get("location"))

    async def provider_get(self, provider: str, params: Mapping[str, str]) -> httpx.Response:
        """GET a READ provider; re-login once on 401 / redirect-to-login; map errors."""
        if provider not in _READ_PROVIDERS:
            raise ConfigError(f"openelis: provider {provider!r} is not an allowed read provider")
        query = _query(provider, params)
        cookie = self._cookie or await self._login(None)
        relogged = False
        while True:
            resp = await self._send("GET", _AJAX_PATH, params=query, headers=self._headers(cookie))
            if self._wants_login(resp):
                if relogged:
                    raise AuthError("openelis: session refused after re-login")
                cookie = await self._login(cookie)
                relogged = True
                continue
            break
        status = resp.status_code
        if status == 403:
            raise AuthError("openelis: HTTP 403 forbidden")
        if status >= 500 or status == 429:
            raise TransientError(f"openelis: HTTP {status}")
        if status == 404:
            raise NotFound("openelis: query servlet not found")
        if status == 200:
            return resp
        raise VendorRejected(f"openelis: unexpected HTTP {status}")

    async def ensure_session(self) -> None:
        """Log in now if there is no session yet (so a login failure is not a send failure)."""
        if self._cookie is None:
            await self._login(None)

    async def provider_send_once(self, provider: str, params: Mapping[str, str]) -> httpx.Response:
        """GET a WRITE provider exactly once. No re-login, no retry, no status mapping.

        A 401/403 raises ``AuthError`` (the request was refused before it ran);
        a transport failure raises ``TransientError`` and the caller decides.
        """
        if provider not in _WRITE_PROVIDERS:
            raise ConfigError(f"openelis: provider {provider!r} is not an allowed write provider")
        query = _query(provider, params)
        cookie = self._cookie or await self._login(None)
        resp = await self._send("GET", _AJAX_PATH, params=query, headers=self._headers(cookie))
        if resp.status_code in (401, 403):
            raise AuthError(f"openelis: write provider HTTP {resp.status_code}")
        return resp


def _parse_xml(text: str) -> Element:
    try:
        root = _safe_fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError) as exc:
        raise VendorRejected(f"openelis: unreadable XML ({type(exc).__name__})") from exc
    if not isinstance(root, Element):
        raise VendorRejected("openelis: unreadable XML")
    return root


def _int(text: str | None, what: str) -> int:
    try:
        return int((text or "").strip())
    except ValueError as exc:
        raise VendorRejected(f"openelis: non-numeric {what}") from exc


def parse_sample_tests(sample_id: int, text: str) -> SampleTests:
    """Parse a ``SampleTypeTestsForSampleProvider`` response."""
    root = _parse_xml(text)
    samples = root.find("./formfield/samples") if root.tag == "fieldmessage" else None
    if samples is None:
        raise VendorRejected("openelis: sample tests response has no samples element")
    type_ids: list[int] = []
    tests_by_type: dict[int, tuple[int, ...]] = {}
    for sample in samples.findall("sample"):
        type_id = _int(sample.findtext("sampleType"), "sample type")
        if type_id in tests_by_type:
            # The collect call keys its list by type; two items of one type are ambiguous.
            raise VendorRejected("openelis: sample lists one sample type twice")
        type_ids.append(type_id)
        tests_by_type[type_id] = tuple(_int(t.text, "test id") for t in sample.findall("test"))
    return SampleTests(
        sample_id=sample_id, type_ids=tuple(type_ids), test_ids_by_type=tests_by_type
    )


def check_accession(accession: str, prefix: str) -> None:
    """Refuse an accession number that is not one we issue (``<prefix><digits>``)."""
    if (
        not isinstance(accession, str)
        or not accession.startswith(prefix)
        or len(accession) > _ACCESSION_MAX
        or not _ACCESSION_RE.fullmatch(accession)
        or _LAB_DATENUM_RE.match(accession)
    ):
        raise VendorRejected("openelis: accession number is not one of ours")


def collect_params(
    sample_id: int, accession: str, collected_on: date, tests: SampleTests, prefix: str
) -> dict[str, str]:
    """Build the collect call's query, refusing anything that could cancel a test."""
    if tests.sample_id != sample_id:
        raise VendorRejected("openelis: test list was read for another sample")
    if not tests.type_ids:
        raise VendorRejected("openelis: refusing to collect with an empty test list")
    if set(tests.type_ids) != set(tests.test_ids_by_type) or len(set(tests.type_ids)) != len(
        tests.type_ids
    ):
        raise VendorRejected("openelis: test list types disagree with the test map")
    if any(not tests.test_ids_by_type[t] for t in tests.type_ids):
        raise VendorRejected("openelis: refusing to collect a sample type with no tests")
    check_accession(accession, prefix)
    type_and_tests = {
        "tests": {
            str(i): ",".join(str(x) for x in tests.test_ids_by_type[t])
            for i, t in enumerate(tests.type_ids)
        },
        "types": {str(i): str(t) for i, t in enumerate(tests.type_ids)},
    }
    return {
        "accessionNumber": accession,
        "sampleId": str(sample_id),
        "collectionDate": collected_on.strftime("%d/%m/%Y"),
        "typeAndTestIds": json.dumps(type_and_tests, separators=(",", ":")),
    }


# ─── read-only lab DB (SQL half) ─────────────────────────────────────────────

# The ONLY SQL this library sends to the lab database. Fixed text, parameters
# only ($n), no string building, no caller SQL. Ids in the lab schema are
# numeric(10,0); they are cast to bigint so drivers return ints.
LAB_QUERIES: Mapping[str, str] = {
    # The "Test Canceled" analysis status, as the lab system's status util maps it.
    "canceled_status_id": (
        "SELECT st.id::bigint AS id FROM clinlims.status_of_sample st"
        " WHERE st.status_type = 'ANALYSIS' AND st.name = 'Test Canceled'"
    ),
    "sample_by_id": (
        "SELECT s.id::bigint AS id, s.uuid AS uuid, s.accession_number AS accession_number,"
        " s.collection_date AS collection_date"
        " FROM clinlims.sample s WHERE s.id = $1::bigint"
    ),
    "event_watermark": (
        "SELECT coalesce(max(e.id), 0)::bigint AS max_id FROM clinlims.event_records e"
    ),
    # An accession event published after the watermark for THIS sample: the
    # event content ends with /ws/rest/accession/<sample uuid> (the sample uuid
    # is the encounter uuid). Suffix compare, not a pattern, so no wildcards.
    "event_for_sample_after": (
        "SELECT e.id::bigint AS id FROM clinlims.event_records e"
        " WHERE e.id > $1::bigint"
        " AND (right(coalesce(e.object, ''), length($2::text)) = $2::text"
        " OR right(coalesce(e.uri, ''), length($2::text)) = $2::text)"
        " ORDER BY e.id DESC LIMIT 1"
    ),
    # Every analysis of the sample (any status), taken before the pre-read.
    "analysis_snapshot": (
        "SELECT a.id::bigint AS id, a.status_id::bigint AS status_id"
        " FROM clinlims.analysis a JOIN clinlims.sample_item si ON si.id = a.sampitem_id"
        " WHERE si.samp_id = $1::bigint"
    ),
    "canceled_analyses": (
        "SELECT a.id::bigint AS id"
        " FROM clinlims.analysis a JOIN clinlims.sample_item si ON si.id = a.sampitem_id"
        " WHERE si.samp_id = $1::bigint AND a.status_id = $2::bigint"
    ),
    # One row per (sample, sample type); a sample's uuid is its encounter uuid.
    "pending_samples": (
        "SELECT s.id::bigint AS sample_id, s.uuid AS encounter_uuid,"
        " t.description AS sample_type, s.accession_number AS accession_number,"
        " s.collection_date AS collection_date,"
        " array_agg(coalesce(te.name, te.description) ORDER BY a.id) AS test_names"
        " FROM clinlims.sample s"
        " JOIN clinlims.sample_item si ON si.samp_id = s.id"
        " JOIN clinlims.type_of_sample t ON t.id = si.typeosamp_id"
        " JOIN clinlims.analysis a ON a.sampitem_id = si.id"
        " JOIN clinlims.test te ON te.id = a.test_id"
        " WHERE s.uuid = ANY($1::text[]) AND a.status_id <> $2::bigint"
        " GROUP BY s.id, s.uuid, t.id, t.description, s.accession_number, s.collection_date"
        " ORDER BY s.uuid, s.id, t.description"
    ),
}
_ALLOWED_SQL = frozenset(LAB_QUERIES.values())
_SHOW_TIMEOUT = "SHOW statement_timeout"
MAX_STATEMENT_TIMEOUT_S = 5.0
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MAX_PENDING_UUIDS = 200
_EVENT_SUFFIX = "/ws/rest/accession/"
_DSN_SCHEMES = frozenset({"postgres", "postgresql"})
_DSN_QUERY_KEYS = frozenset({"sslmode"})
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|s|min|h|d)?")
_DURATION_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


class LabSqlExecutor(Protocol):
    """Runs one of the fixed lab queries; returns rows as mappings."""

    async def fetch(self, sql: str, *args: Any) -> Sequence[Mapping[str, Any]]: ...

    async def aclose(self) -> None: ...


class OpenElisSql:
    """The query allowlist: callers name a fixed query, never pass SQL."""

    def __init__(self, executor: LabSqlExecutor) -> None:
        self._executor = executor

    async def run(self, name: str, *args: Any) -> list[Mapping[str, Any]]:
        sql = LAB_QUERIES.get(name)
        if sql is None:
            raise ConfigError("openelis db: query is not in the fixed allowlist")
        return list(await self._executor.fetch(sql, *args))

    async def aclose(self) -> None:
        await self._executor.aclose()


def parse_pg_duration(value: str) -> float:
    """Seconds from a Postgres duration setting (``SHOW`` output; bare number = ms)."""
    m = _DURATION_RE.fullmatch((value or "").strip())
    if not m:
        raise ConfigError("openelis db: unreadable statement_timeout")
    unit = m.group(2) or "ms"
    return float(m.group(1)) * _DURATION_UNITS[unit]


def validate_ro_dsn(dsn: str) -> None:
    """Require a complete DSN, so the driver never falls back to PG* env or files."""
    if not dsn:
        raise ConfigError("openelis db: ro_dsn is required for the lab DB")
    try:
        parts = urlsplit(dsn)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise ConfigError("openelis db: ro_dsn is not a valid URL") from exc
    query_keys = {k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if (
        parts.scheme not in _DSN_SCHEMES
        or not parts.username
        or not parts.password
        or not parts.hostname
        or not parts.path.strip("/")
        or "/" in parts.path.strip("/")
        or not query_keys <= _DSN_QUERY_KEYS
        or parts.fragment
    ):
        # Never echo the DSN: it carries the password.
        raise ConfigError(
            "openelis db: ro_dsn must be postgresql://user:password@host[:port]/db"
            " (only sslmode may be set as a query parameter)"
        )


ConnectFn = Callable[..., Awaitable[Any]]


async def _asyncpg_connect(dsn: str, *, timeout: float) -> Any:
    try:
        # Lazy: the 'lab' extra; never imported at module import. No stubs ship.
        import asyncpg  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ConfigError("openelis db: install the 'lab' extra (asyncpg)") from exc
    return await asyncpg.connect(dsn, timeout=timeout)


def _map_db_error(exc: BaseException) -> HmsAdapterError | None:
    """Map a driver/connection error; ``None`` for anything else (re-raised unchanged).

    A programming error (say a ``KeyError`` on a renamed column alias) must not
    become a retryable ``TransientError``.
    """
    if isinstance(exc, HmsAdapterError):
        return exc
    name = type(exc).__name__
    if isinstance(exc, (OSError, TimeoutError, asyncio.TimeoutError)):
        return TransientError(f"openelis db: connection error: {name}")
    apg = sys.modules.get("asyncpg")
    if apg is not None:
        errs = apg.exceptions
        if isinstance(exc, (errs.QueryCanceledError, errs.InterfaceError)):
            return TransientError(f"openelis db: {name}")
        if isinstance(exc, errs.ConnectionDoesNotExistError):
            return TransientError(f"openelis db: {name}")
        if isinstance(
            exc,
            (
                errs.InsufficientPrivilegeError,
                errs.InvalidAuthorizationSpecificationError,
                errs.InvalidPasswordError,
                errs.UndefinedTableError,
                errs.UndefinedColumnError,
                errs.InvalidCatalogNameError,
            ),
        ):
            return ConfigError(f"openelis db: {name}")
        if isinstance(exc, apg.PostgresError):
            return VendorRejected(f"openelis db: {name}")
    return None


class AsyncpgLabExecutor:
    """Read-only lab DB access over one asyncpg connection (opened lazily).

    * runs only SQL texts from ``LAB_QUERIES`` (defence in depth behind ``OpenElisSql``);
    * every query runs in a ``READ ONLY`` transaction;
    * on connect, ``SHOW statement_timeout`` must be set and at most 5 s (the
      read-only role carries it), else ``ConfigError`` and the connection closes.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect: ConnectFn | None = None,
        connect_timeout_s: float = 10.0,
        max_statement_timeout_s: float = MAX_STATEMENT_TIMEOUT_S,
    ) -> None:
        validate_ro_dsn(dsn)
        self._dsn = dsn
        self._connect = connect or _asyncpg_connect
        self._connect_timeout_s = connect_timeout_s
        self._max_timeout_s = max_statement_timeout_s
        self._conn: Any = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:  # never print the DSN
        return "AsyncpgLabExecutor(<dsn hidden>)"

    async def _ensure(self) -> Any:
        if self._conn is not None and not self._conn.is_closed():
            return self._conn
        conn = await self._connect(self._dsn, timeout=self._connect_timeout_s)
        try:
            secs = parse_pg_duration(str(await conn.fetchval(_SHOW_TIMEOUT)))
            if secs <= 0 or secs > self._max_timeout_s:
                raise ConfigError(
                    "openelis db: the read-only role's statement_timeout must be set"
                    f" and at most {self._max_timeout_s:g}s"
                )
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        return conn

    async def fetch(self, sql: str, *args: Any) -> Sequence[Mapping[str, Any]]:
        if sql not in _ALLOWED_SQL:
            raise ConfigError("openelis db: query is not in the fixed allowlist")
        async with self._lock:
            try:
                conn = await self._ensure()
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(sql, *args)
            except Exception as exc:
                mapped = _map_db_error(exc)
                if isinstance(mapped, TransientError) and self._conn is not None:
                    await self._drop()
                if mapped is None or mapped is exc:
                    raise
                raise mapped from exc
        return [dict(r) for r in rows]

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - best effort on a broken connection
            log.debug("openelis db: close after error failed", exc_info=True)

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._drop()


@dataclass(frozen=True)
class PendingSample:
    sample_id: int
    encounter_uuid: str
    sample_type: str
    test_names: tuple[str, ...]
    accession_number: str | None
    collection_date: date | None


@dataclass(frozen=True)
class CollectResult:
    sample_id: int
    accession_number: str
    collection_date: date
    event_id: int
    already_recorded: bool


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise VendorRejected("openelis db: unreadable collection date")


def _as_int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise VendorRejected(f"openelis db: non-numeric {what}") from exc


class _CollectOutcomeUnknown(VendorRejected):  # noqa: N818
    """The collect call may have run: the answer was lost or was a 5xx."""


def _pairs(tests: SampleTests) -> set[tuple[int, int]]:
    return {(t, x) for t in tests.type_ids for x in tests.test_ids_by_type.get(t, ())}


class OpenElisClient:
    """Lab-system client: servlet calls (HTTP) + the read-only lab DB (SQL).

    ``sql`` injects a ``LabSqlExecutor`` (tests, or a caller-owned pool); by
    default an ``AsyncpgLabExecutor`` over ``config.ro_dsn`` is built on first use.
    ``sleep``/``clock`` drive the post-collect verify polling.
    """

    def __init__(
        self,
        config: OpenElisConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sql: LabSqlExecutor | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        verify_timeout_s: float = 10.0,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.config = config
        self._sql = OpenElisSql(sql) if sql is not None else None
        self._sleep = sleep
        self._clock = clock
        self._verify_timeout_s = verify_timeout_s
        self._poll_interval_s = poll_interval_s
        self._canceled_status: int | None = None
        self._http = OpenElisHttp(
            config.base_url,
            config.username,
            config.password,
            config.guard_header_value,
            ca_bundle_pem=config.ca_bundle_pem,
            verify_tls=config.verify_tls,
            allow_insecure_tls=config.allow_insecure_tls,
            timeout_s=config.timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._sql is not None:
            await self._sql.aclose()

    def _db(self) -> OpenElisSql:
        if self._sql is None:
            # ConfigError here (no/incomplete ro_dsn) happens before any I/O.
            self._sql = OpenElisSql(AsyncpgLabExecutor(self.config.ro_dsn))
        return self._sql

    async def sample_tests(self, sample_id: int) -> SampleTests:
        resp = await self._http.provider_get(PROVIDER_SAMPLE_TESTS, {"sampleId": str(sample_id)})
        return parse_sample_tests(sample_id, resp.text)

    async def _send_collect(
        self, sample_id: int, accession: str, collected_on: date, tests: SampleTests
    ) -> None:
        """Send the destructive collect call once with exactly ``tests``.

        HTTP 204 returns; anything else raises ``VendorRejected``. A transport
        failure during the send (``lab_collect_unconfirmed``) and an HTTP 5xx
        (``lab_collect_http_5xx``: a gateway timeout, or an error after the
        commit) leave the outcome unknown; they are raised as the private
        ``_CollectOutcomeUnknown`` subclass, which ``collect`` verifies in the DB.
        The login runs first, so a login failure raises ``AuthError``/
        ``TransientError`` with nothing sent. Never retried here.
        """
        params = collect_params(
            sample_id, accession, collected_on, tests, self.config.accession_prefix
        )
        # Log in before the send: a login failure here means nothing was sent.
        await self._http.ensure_session()
        try:
            resp = await self._http.provider_send_once(PROVIDER_COLLECT, params)
        except TransientError as exc:
            log.error("openelis collect outcome unknown for sample %s: %s", sample_id, exc)
            raise _CollectOutcomeUnknown("lab_collect_unconfirmed") from exc
        if resp.status_code != 204:
            log.error(
                "openelis collect for sample %s answered HTTP %s", sample_id, resp.status_code
            )
            code = f"lab_collect_http_{resp.status_code}"
            if resp.status_code >= 500:
                raise _CollectOutcomeUnknown(code)
            raise VendorRejected(code)

    # ─── SQL half ───────────────────────────────────────────────────────────

    async def _canceled_status_id(self) -> int:
        """The "Test Canceled" analysis status id, resolved once per client."""
        if self._canceled_status is None:
            rows = await self._db().run("canceled_status_id")
            if len(rows) != 1:
                raise ConfigError(
                    "openelis db: expected exactly one 'Test Canceled' analysis status,"
                    f" found {len(rows)}"
                )
            self._canceled_status = _as_int(rows[0]["id"], "status id")
        return self._canceled_status

    async def pending_samples(self, encounter_uuids: Sequence[str]) -> list[PendingSample]:
        """Lab samples of these encounters, one per (encounter, sample type).

        Cancelled tests are left out. ``accession_number is None`` means not
        collected yet; a collected sample is returned with its accession.
        """
        uuids = list(encounter_uuids)
        if not uuids:
            return []
        if len(uuids) > _MAX_PENDING_UUIDS or not all(
            isinstance(u, str) and _UUID_RE.fullmatch(u) for u in uuids
        ):
            raise VendorRejected("openelis: encounter uuids must be 1-200 uuids")
        db = self._db()
        canceled = await self._canceled_status_id()
        rows = await db.run("pending_samples", uuids, canceled)
        return [
            PendingSample(
                sample_id=_as_int(r["sample_id"], "sample id"),
                encounter_uuid=str(r["encounter_uuid"]),
                sample_type=str(r["sample_type"]),
                test_names=tuple(str(n) for n in (r["test_names"] or ()) if n is not None),
                accession_number=r["accession_number"] or None,
                collection_date=_as_date(r["collection_date"]),
            )
            for r in rows
        ]

    async def _sample_row(self, sample_id: int) -> Mapping[str, Any]:
        rows = await self._db().run("sample_by_id", sample_id)
        if not rows:
            raise NotFound(f"openelis: lab sample {sample_id} not found")
        return rows[0]

    async def _event_after(self, watermark: int, sample_uuid: str) -> int | None:
        rows = await self._db().run(
            "event_for_sample_after", watermark, _EVENT_SUFFIX + sample_uuid
        )
        return _as_int(rows[0]["id"], "event id") if rows else None

    async def collect(self, sample_id: int, accession: str, collected_on: date) -> CollectResult:
        """Record a sample collection in the lab system, then verify it in the lab DB.

        1. Read the sample. Already carrying ``accession`` → ``already_recorded``
           (no call). Another accession → ``ConflictError("accession_conflict")``.
        2. Watermark ``max(event_records.id)``; snapshot every analysis
           (id, status) of the sample; then read the current tests.
        3. Send the collect call once with exactly that test list.
        4. Poll (``verify_timeout_s``) for our accession on the sample and an
           accession event for THIS sample after the watermark.
        5. Raise ``VendorRejected``: ``lab_not_recorded`` (accession absent),
           ``lab_tests_changed`` (a pre-read test is gone, or an analysis is now
           cancelled that was not cancelled in the snapshot, e.g. one the lab feed
           added after the pre-read; logged at error level with ids only), or
           ``lab_event_missing`` (OpenMRS will not hear of it).

        A send whose outcome is unknown (the answer was lost, or HTTP 5xx) is
        verified the same way; another non-204 (``lab_collect_http_<n>``) is
        raised as is. A ``TransientError`` while verifying, after the send, raises
        ``lab_collect_unverified``.

        Caller contract: every error raised after the send (``lab_not_recorded``
        aside) means "may be recorded". ``lab_tests_changed`` cannot be re-derived
        later (there is no snapshot on a retry), so the caller keeps its own
        marker from the first failure and never treats a later
        ``already_recorded=True`` as clean. The idempotent path (step 1) still
        requires an accession event (``lab_event_missing`` otherwise).

        Not concurrency-safe: the caller holds a claim across this call. Sibling
        samples (two sample types of one encounter) share the encounter uuid and
        hence the event suffix, so a sibling's concurrent collect can satisfy our
        event check, and on the idempotent path any sibling event counts; hold
        the claim per encounter, not only per sample.
        """
        if not isinstance(sample_id, int) or isinstance(sample_id, bool) or sample_id <= 0:
            raise VendorRejected("openelis: sample id must be a positive integer")
        check_accession(accession, self.config.accession_prefix)
        db = self._db()

        row = await self._sample_row(sample_id)
        current = row["accession_number"] or None
        sample_uuid = str(row["uuid"] or "")
        if not _UUID_RE.fullmatch(sample_uuid):
            raise VendorRejected("openelis: lab sample has no encounter uuid")
        if current == accession:
            event_id = await self._event_after(0, sample_uuid)
            if event_id is None:
                log.error(
                    "openelis sample %s carries our accession but has no accession event",
                    sample_id,
                )
                raise VendorRejected("lab_event_missing")
            return CollectResult(
                sample_id=sample_id,
                accession_number=accession,
                collection_date=_as_date(row["collection_date"]) or collected_on,
                event_id=event_id,
                already_recorded=True,
            )
        if current is not None:
            raise ConflictError("accession_conflict")

        canceled = await self._canceled_status_id()
        wm_rows = await db.run("event_watermark")
        watermark = _as_int(wm_rows[0]["max_id"], "event id") if wm_rows else 0
        snapshot = {
            _as_int(r["id"], "analysis id"): _as_int(r["status_id"], "status id")
            for r in await db.run("analysis_snapshot", sample_id)
        }
        pre = await self.sample_tests(sample_id)

        # An unknown outcome falls through: the call may have run; the DB tells.
        with contextlib.suppress(_CollectOutcomeUnknown):
            await self._send_collect(sample_id, accession, collected_on, pre)

        try:
            return await self._verify_collect(
                sample_id, accession, collected_on, sample_uuid, watermark, canceled, snapshot, pre
            )
        except TransientError as exc:
            log.error("openelis collect for sample %s sent but not verified: %s", sample_id, exc)
            raise VendorRejected("lab_collect_unverified") from exc

    async def _verify_collect(
        self,
        sample_id: int,
        accession: str,
        collected_on: date,
        sample_uuid: str,
        watermark: int,
        canceled: int,
        snapshot: Mapping[int, int],
        pre: SampleTests,
    ) -> CollectResult:
        """Steps 4-5 of ``collect``: runs after the send, so the outcome is open."""
        db = self._db()
        recorded, event_id, row = await self._poll_recorded(
            sample_id, accession, sample_uuid, watermark
        )
        if not recorded:
            log.error("openelis collect for sample %s not recorded in the lab DB", sample_id)
            raise VendorRejected("lab_not_recorded")

        post = await self.sample_tests(sample_id)
        missing = sorted(_pairs(pre) - _pairs(post))
        newly_canceled = sorted(
            aid
            for aid in (
                _as_int(r["id"], "analysis id")
                for r in await db.run("canceled_analyses", sample_id, canceled)
            )
            if snapshot.get(aid) != canceled
        )
        if missing or newly_canceled:
            log.error(
                "openelis collect for sample %s changed its tests: cancelled analysis ids %s,"
                " missing (type, test) ids %s",
                sample_id,
                newly_canceled,
                missing,
            )
            raise VendorRejected("lab_tests_changed")
        if event_id is None:
            log.error("openelis collect for sample %s published no accession event", sample_id)
            raise VendorRejected("lab_event_missing")
        return CollectResult(
            sample_id=sample_id,
            accession_number=accession,
            collection_date=_as_date(row["collection_date"]) or collected_on,
            event_id=event_id,
            already_recorded=False,
        )

    async def _poll_recorded(
        self, sample_id: int, accession: str, sample_uuid: str, watermark: int
    ) -> tuple[bool, int | None, Mapping[str, Any]]:
        """Poll until our accession and THIS sample's new event are both seen."""
        deadline = self._clock() + self._verify_timeout_s
        event_id: int | None = None
        while True:
            row = await self._sample_row(sample_id)
            recorded = row["accession_number"] == accession
            if recorded:
                event_id = await self._event_after(watermark, sample_uuid)
                if event_id is not None:
                    return True, event_id, row
            if self._clock() >= deadline:
                return recorded, event_id, row
            await self._sleep(self._poll_interval_s)
