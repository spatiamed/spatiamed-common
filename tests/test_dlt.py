from sm_common.dlt import validate_dlt_template_id


def test_valid_10_digit():
    assert validate_dlt_template_id("1234567890") is True


def test_valid_19_digit():
    assert validate_dlt_template_id("1234567890123456789") is True


def test_valid_15_digit():
    assert validate_dlt_template_id("123456789012345") is True


def test_invalid_too_short():
    assert validate_dlt_template_id("123456789") is False


def test_invalid_too_long():
    assert validate_dlt_template_id("12345678901234567890") is False


def test_invalid_non_numeric():
    assert validate_dlt_template_id("12345abc90") is False


def test_invalid_empty():
    assert validate_dlt_template_id("") is False


def test_invalid_spaces():
    assert validate_dlt_template_id("123 456 7890") is False
