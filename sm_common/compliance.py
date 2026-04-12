from datetime import datetime, time

import pytz

IST = pytz.timezone("Asia/Kolkata")

# TCCCPR (Telecom Commercial Communications Customer Preference Regulations)
ALLOWED_START = time(9, 0)  # 9:00 AM IST
ALLOWED_END = time(21, 0)  # 9:00 PM IST
MAX_CALLS_PER_DAY = 1
MAX_CALLS_PER_WEEK = 3


def is_within_allowed_hours(now: datetime | None = None) -> bool:
    """Check if current IST time is within TCCCPR allowed calling hours (9AM-9PM)."""
    if now is None:
        now = datetime.now(IST)
    elif now.tzinfo is None:
        now = IST.localize(now)
    else:
        now = now.astimezone(IST)

    current_time = now.time()
    return ALLOWED_START <= current_time <= ALLOWED_END
