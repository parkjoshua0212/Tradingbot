import json
import logging
import os
import urllib.request

import data  # noqa: F401  (loads .env on import)

log = logging.getLogger(__name__)


def send_alert(text: str) -> None:
    """Post to DISCORD_WEBHOOK_URL. Never raises."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; alert skipped")
        return
    try:
        req = urllib.request.Request(
            url,
            json.dumps({"content": text[:2000]}).encode(),
            # Discord rejects urllib's default User-Agent (403)
            {"Content-Type": "application/json", "User-Agent": "mundo-trade-bot"},
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:  # alerts must never crash the bot
        log.warning("Discord alert failed: %s", e)
