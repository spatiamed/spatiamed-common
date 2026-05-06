from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
import respx
import httpx

from sm_common.integrations.adapters.generic_rest import GenericRestAdapter
from sm_common.integrations.exceptions import ConflictError, TransientError


MEDIXCEL_MAPPING = {
    "base_url": "https://api.medixcel.in",
    "auth_scheme": "api_key",
    "api_key_header": "X-Api-Key",
    "api_key": "medixcel-key",
    "list_appointments_path": "/v2/appointments",
    "cursor_field": "lastModifiedCursor",
    "appointment_fields": {
        "appointment_id": "apptId",
        "hms_version": "version",
        "doctor_external_id": "docId",
        "department_external_id": "deptId",
        "slot_start": "startTime",
        "slot_duration_min": "duration",
        "payer_type": "payerType",
        "status": "apptStatus",
        "mrn": "patientMrn",
    },
    "write_back_path": "/v2/appointments",
    "idempotency_field": "externalRef",
    "conflict_status_code": 422,
    "doctor_roster_path": "/v2/doctors",
    "patient_lookup_path": "/v2/patients",
    "patient_lookup_param": "mrn",
    "appointments_list_key": "data",
    "doctors_list_key": "doctors",
    "doctor_fields": {
        "external_doctor_id": "docId",
        "external_speciality_id": "specialityCode",
        "external_department_id": "deptCode",
        "speciality_label": "speciality",
        "department_label": "department",
        "consultation_duration_min": "consultDuration",
    },
}


class TestGenericRestAdapterFieldMapping:
    @respx.mock
    async def test_maps_vendor_fields_to_canonical(self):
        respx.get("https://api.medixcel.in/v2/appointments").mock(
            return_value=httpx.Response(200, json={
                "data": [{
                    "apptId": "APT-MX-001",
                    "version": 3,
                    "docId": "DR-MX-001",
                    "deptId": "DEPT-MX-001",
                    "startTime": "2026-05-07T11:00:00Z",
                    "duration": 20,
                    "payerType": "INSURANCE",
                    "apptStatus": "confirmed",
                    "patientMrn": "MRN-MX-001",
                }],
                "lastModifiedCursor": "2026-05-07T11:00:00Z",
            })
        )
        adapter = GenericRestAdapter(MEDIXCEL_MAPPING)
        apts, cursor = await adapter.list_appointments_modified_since("", date(2026, 5, 8))
        assert len(apts) == 1
        assert apts[0].appointment_id == "APT-MX-001"
        assert apts[0].doctor_external_id == "DR-MX-001"
        assert apts[0].slot_duration_min == 20
        assert cursor == "2026-05-07T11:00:00Z"

    @respx.mock
    async def test_conflict_on_custom_status_code(self):
        respx.post("https://api.medixcel.in/v2/appointments").mock(
            return_value=httpx.Response(422, json={"message": "slot taken"})
        )
        adapter = GenericRestAdapter(MEDIXCEL_MAPPING)
        with pytest.raises(ConflictError):
            await adapter.write_back_idempotent(uuid4(), {}, "idem-001")

    @respx.mock
    async def test_500_raises_transient(self):
        respx.get("https://api.medixcel.in/v2/appointments").mock(
            return_value=httpx.Response(500)
        )
        adapter = GenericRestAdapter(MEDIXCEL_MAPPING)
        with pytest.raises(TransientError):
            await adapter.list_appointments_modified_since("", date(2026, 5, 8))
