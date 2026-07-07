from datetime import time

import pytz

IST = pytz.timezone("Asia/Kolkata")

# TCCCPR (Telecom Commercial Communications Customer Preference Regulations)
ALLOWED_START = time(9, 0)  # 9:00 AM IST
ALLOWED_END = time(21, 0)  # 9:00 PM IST
MAX_CALLS_PER_DAY = 1
MAX_CALLS_PER_WEEK = 3
