import re

DLT_TEMPLATE_PATTERN = re.compile(r"^\d{10,19}$")


def validate_dlt_template_id(template_id: str) -> bool:
    """Validate that a template ID matches TRAI DLT format."""
    return bool(DLT_TEMPLATE_PATTERN.match(template_id))
