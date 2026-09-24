"""Require an explicit iLink acknowledgement for employee text delivery."""
import asyncio
import hashlib
import logging
import time

logger = logging.getLogger(__name__)


def response_state(response):
    if not isinstance(response, dict):
        return 'unknown'
    # iLink's successful POST may return an empty object (optional codes).
    if response == {}:
        return 'accepted_unconfirmed'
    if response.get('error') or response.get('success') is False:
        return 'rejected'
    codes = [response[k] for k in ('ret', 'errcode') if k in response]
    base = response.get('base_resp')
    if isinstance(base, dict):
        codes.extend(base[k] for k in ('ret', 'errcode') if k in base)
    if not codes and type(response.get('message_id')) is int and response['message_id'] > 0:
        return 'accepted_unconfirmed'
    if not codes or any(type(c) is not int for c in codes):
        return 'unknown'
    return 'accepted' if all(c == 0 for c in codes) else 'rejected'


def install_weixin_delivery():
    from gateway.platforms import weixin as w
    cls = w.WeixinAdapter
    if getattr(cls, '_strict_delivery_installed', False):
        return

    async def send_locked(self, *, chat_id, chunk, context_token, client_id):
        # Keep the native adapter-wide send gate. Do not retry ambiguous ACKs
        # or strip a context token: neither establishes delivery to this peer.
        started = time.monotonic()
        peer = hashlib.sha256(chat_id.encode()).hexdigest()[:16]
        account = hashlib.sha256(self._account_id.encode()).hexdigest()[:16]
        profile = getattr(self, '_owner_profile', None) or getattr(self, '_profile', None)
        if self._rate_limit_cooldown_remaining() > 0:
            raise self._rate_limit_error()
        try:
            response = await w._send_message(
                self._send_session, base_url=self._base_url, token=self._token,
                to=chat_id, text=chunk, context_token=context_token, client_id=client_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('weixin delivery profile=%s peer=%s account=%s trace=%s state=unknown exception=%s',
                           profile, peer, account, client_id, type(exc).__name__)
            # Native BasePlatformAdapter avoids resending timeout results.
            raise TimeoutError('Weixin acknowledgement timed out or unavailable; receipt unconfirmed') from None
        state = response_state(response)
        logger.info('weixin acknowledgement shape trace=%s type=%s fields=%s', client_id,
                    type(response).__name__,
                    {str(k)[:40]: type(v).__name__ for k, v in response.items()}
                    if isinstance(response, dict) else {})
        codes = {k: response[k] for k in ('ret', 'errcode')
                 if isinstance(response, dict) and type(response.get(k)) is int}
        logger.info('weixin delivery profile=%s peer=%s account=%s trace=%s context_present=%s state=%s codes=%s duration_ms=%d',
                    profile, peer, account, client_id, bool(context_token), state, codes,
                    round((time.monotonic()-started)*1000))
        if state == 'unknown':
            raise TimeoutError('Weixin acknowledgement timed out: response missing valid result codes; receipt unconfirmed')
        if state == 'rejected':
            if w.RATE_LIMIT_ERRCODE in codes.values():
                self._record_rate_limit_event()
            raise RuntimeError('Weixin API rejected delivery: ' + str(codes))
        self._reset_rate_limit_circuit()

    cls._send_text_chunk_locked = send_locked
    cls._strict_delivery_installed = True
    logger.info('Weixin strict delivery acknowledgement and diagnostics installed')
