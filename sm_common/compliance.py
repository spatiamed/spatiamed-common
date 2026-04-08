from datetime import datetime, time

import pytz

IST = pytz.timezone("Asia/Kolkata")

TCCCPR_RULES = {
    "allowed_start": time(9, 0),    # 9:00 AM IST
    "allowed_end": time(21, 0),     # 9:00 PM IST
    "max_calls_per_day": 1,
    "max_calls_per_week": 3,
}


def is_within_allowed_hours(now: datetime | None = None) -> bool:
    """Check if current IST time is within TCCCPR allowed calling hours (9AM-9PM)."""
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = IST.localize(now)
    else:
        now = now.astimezone(IST)

    current_time = now.time()
    return TCCCPR_RULES["allowed_start"] <= current_time <= TCCCPR_RULES["allowed_end"]
