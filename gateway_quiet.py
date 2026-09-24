"""Keep native drain refusal notices in operator logs, across all Profiles."""
import logging
from functools import wraps

logger = logging.getLogger(__name__)

# Exact native control replies only: never filter ordinary model responses
# containing words such as 'gateway' or 'restart'.
DRAIN_NOTICES = frozenset(
    text
    for action in ("restarting", "shutting down")
    for text in (
        f"⏳ Gateway is {action} and is not accepting new work right now.",
        f"⏳ Gateway is {action} and is not accepting another turn right now.",
        f"⏳ Gateway {action} — queued for the next turn after it comes back.",
    )
)


def install_gateway_quiet():
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    original = BasePlatformAdapter._send_with_retry
    if getattr(original, "_employee_quiet_drain", False):
        return

    @wraps(original)
    async def send(self, chat_id, content, *args, **kwargs):
        if content in DRAIN_NOTICES:
            logger.info("Gateway drain notice suppressed; operator-only: %s", content)
            # Resolve this control notice so the delivery ledger won't replay it.
            # Native task queue / shutdown persistence remains responsible for work.
            return SendResult(success=True)
        return await original(self, chat_id, content, *args, **kwargs)

    send._employee_quiet_drain = True
    BasePlatformAdapter._send_with_retry = send
    logger.info("Quiet gateway drain notices installed for all Profile adapters")
