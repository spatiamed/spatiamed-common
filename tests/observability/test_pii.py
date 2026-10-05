import pytest

from sm_common.observability.pii import redact_text


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        ("call 9876543210 now", "9876543210", "[PHONE]"),
        ("call +91 98765 43210 now", "98765 43210", "[PHONE]"),
        ("call +91-9876543210", "9876543210", "[PHONE]"),
        ("call 09876543210", "9876543210", "[PHONE]"),
        ("ksa +966512345678", "966512345678", "[PHONE]"),
        ("ksa 0512345678 local", "0512345678", "[PHONE]"),
        ("uk +447911123456", "447911123456", "[PHONE]"),
        ("mail ramesh.k+x@example.co.in ok", "ramesh.k+x@example.co.in", "[EMAIL]"),
        ("aadhaar 2345 6789 0123", "2345 6789 0123", "[AADHAAR]"),
        ("aadhaar 2345-6789-0123", "2345-6789-0123", "[AADHAAR]"),
        ("aadhaar 234567890123", "234567890123", "[AADHAAR]"),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl", "[TOKEN]"),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl",
            "eyJzdWIiOiIxMjMifQ",
            "[TOKEN]",
        ),
        ("GET /t/Abc_123-xyz/status", "Abc_123-xyz", "/t/[REDACTED]"),
        ("https://patient.example.com/t/Abc123?x=1", "Abc123", "/t/[REDACTED]"),
        ("GET https://api.exotel.com/v1/Calls?To=foo&Body=bar", "To=foo", "?[Filtered]"),
        ("GET /t/cap0123456789abcdef0123/status", "0123456789abcdef0123", "/t/[REDACTED]"),
        ("mail a1b2c3d4e5f6a7@example.org", "a1b2c3d4e5f6a7", "[EMAIL]"),
    ],
)
def test_redacts(raw: str, must_not_contain: str, must_contain: str) -> None:
    out = redact_text(raw)
    assert must_not_contain not in out
    assert must_contain in out


def test_aadhaar_starting_6_to_9_is_one_aadhaar_not_a_phone() -> None:
    # Ordering bug in the CareLoop original: phone ran first and ate 10 of the 12 digits.
    out = redact_text("id 987654321012 end")
    assert out == "id [AADHAAR] end"


@pytest.mark.parametrize(
    "safe",
    [
        "patient 3f2a9c1e-7b6d-4e8f-9a0b-123456789012 not found",  # UUID, all-digit last group
        "ts=1759740000000",  # epoch ms, 13 digits
        "ts=1759740000",  # epoch s, starts with 1
        "booking 12345678 failed",  # 8-digit id
        "token_number=42",
        "phone_hash=5d41402abc4b2a76b9719d911017c592",
        # sha256 / event_id containing a phone-shaped 10-digit run
        "phone_hash=ab6789012345cd6789012345ef0123456789abcdef0123456789abcdef012345",
        "event_id=a1b2c3d46789012345e6f7a8b9c0d1e2",
    ],
)
def test_identifiers_survive(safe: str) -> None:
    assert redact_text(safe) == safe
