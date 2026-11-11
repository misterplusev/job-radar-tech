"""Keep credentials out of public execution receipts and failure alerts."""
import os
import re
from urllib.parse import quote


def safe_error(error: object) -> str:
    text = str(error)
    values = {value for name, value in os.environ.items()
              if len(value) >= 8 and any(word in name.upper()
                                        for word in ("TOKEN", "KEY", "SECRET", "WEBHOOK", "PASSWORD"))}
    for value in sorted(values, key=len, reverse=True):
        for representation in (value, quote(value, safe="")):
            text = text.replace(representation, "[redacted]")
    return re.sub(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s'\"<>]+",
                  "[redacted Discord webhook]", text, flags=re.IGNORECASE)
