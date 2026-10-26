"""Value types for lab results held in the hospital record (Bahmni/OpenMRS).

Used by ``sm_common.integrations.adapters.bahmni_lab_results``. Import by module
path; these types are deliberately not re-exported from the package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

# numeric -> Decimal; text -> str; coded -> the answer concept uuid (str).
type LabResultValue = Decimal | str

type LabTestDatatype = Literal["Numeric", "Text", "Coded", "Boolean", "Other"]


@dataclass(frozen=True)
class LabResultRow:
    """One row of Bahmni's ``labOrderResults``: one test of one lab order.

    A panel order yields one row per member test, each carrying the panel's uuid
    and name. ``status`` is ``"pending"`` for an order with no result obs yet
    (Bahmni returns those with ``result: null`` and no result time); a pending row
    is never a blank result.

    ``result`` is the value as Bahmni renders it (numbers become their decimal
    text; a coded result is the answer's name). ``result_uuid`` is Bahmni's
    ``resultUuid``: the answer concept uuid for a coded result, else ``None``.
    """

    order_uuid: str
    accession_uuid: str | None
    test_uuid: str
    test_name: str
    panel_uuid: str | None
    panel_name: str | None
    unit: str | None
    status: Literal["pending", "resulted"]
    result: str | None
    result_uuid: str | None
    min_normal: Decimal | None
    max_normal: Decimal | None
    abnormal: bool | None
    referred_out: bool
    notes: str | None
    provider: str | None
    result_at: datetime | None  # aware UTC
    uploaded_file_name: str | None


@dataclass(frozen=True, kw_only=True)
class LabResultWrite:
    """One test result to write into the record.

    ``order_uuid`` is the lab order the result answers; for a member of a panel it
    is the **panel** order (Bahmni joins results to orders on that uuid).
    """

    order_uuid: str
    test_concept_uuid: str
    panel_concept_uuid: str | None
    value_kind: Literal["numeric", "text", "coded"]
    value: LabResultValue
    min_normal: Decimal | None = None
    max_normal: Decimal | None = None
    abnormal: bool = False
    notes: str | None = None
    referred_out: bool = False
    result_at: datetime  # aware


@dataclass(frozen=True)
class LabResultWriteOutcome:
    """What a result write produced.

    ``encounter_uuid`` is NOT unique per write: Bahmni folds writes of the same
    encounter type, visit and provider within its session window into one
    encounter. ``obs_uuids_by_order`` holds the top-level group obs uuids per order.
    ``recovered`` is True when nothing was posted because the record already held
    this write (an earlier attempt landed unacknowledged) and the groups were read
    back from the encounter the caller named.
    """

    encounter_uuid: str
    obs_uuids_by_order: Mapping[str, tuple[str, ...]]
    recovered: bool = False


@dataclass(frozen=True)
class LabTestDefinition:
    """A lab test concept: its datatype, units (numeric) and answers (coded).

    ``answers`` is ``(answer concept uuid, display)`` in Bahmni's order.
    """

    concept_uuid: str
    datatype: LabTestDatatype
    units: str | None
    answers: tuple[tuple[str, str], ...]
