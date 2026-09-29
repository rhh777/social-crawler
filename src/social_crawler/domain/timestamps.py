"""Normalize platform timestamps without inventing missing source times."""

import math
from datetime import datetime


def unix_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
            stamp = date.timestamp()
        except ValueError:
            return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    if stamp >= 100_000_000_000:
        stamp /= 1000
    return stamp if stamp < 32_503_680_000 else None
