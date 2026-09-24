"""Validate Weixin document downloads before admitting them to the attachment cache."""
from __future__ import annotations

import base64
import logging
from pathlib import Path
import re
from functools import wraps

logger = logging.getLogger(__name__)


class MediaValidationError(ValueError):
    """A stable, non-secret reason suitable for logs."""


def _key_bytes(value):
    if not isinstance(value, str):
        raise MediaValidationError('invalid_aes_key')
    value = value.strip()
    try:
        if re.fullmatch(r'[0-9a-fA-F]{32}', value):
            return bytes.fromhex(value)
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32 and re.fullmatch(rb'[0-9a-fA-F]{32}', decoded):
            return bytes.fromhex(decoded.decode('ascii'))
    except (ValueError, UnicodeError):
        pass
    raise MediaValidationError('invalid_aes_key')


def _resolve_key(file_item, media):
    # aeskey is the hex form already used by iLink image items; aes_key is
    # normally base64(raw bytes) or base64(hex). Accept either representation.
    candidates = [(f'{scope}.{field}', obj[field])
                  for scope, obj in [('media', media), ('file_item', file_item)]
                  for field in ('aes_key', 'aeskey') if obj.get(field)]
    keys = [_key_bytes(value) for _, value in candidates]
    if len(set(keys)) > 1:
        raise MediaValidationError('conflicting_aes_keys')
    return (base64.b64encode(keys[0]).decode('ascii') if keys else None,
            ','.join(name for name, _ in candidates) or 'none')


def _validate_document(data, filename, file_item):
    declared = file_item.get('len')
    if declared not in (None, ''):
        try:
            expected = int(declared)
        except (ValueError, TypeError):
            raise MediaValidationError('invalid_declared_length') from None
        if expected < 0 or len(data) != expected:
            raise MediaValidationError('plaintext_length_mismatch')
    suffix = Path(filename).suffix.lower()
    if suffix in {'.md', '.txt', '.csv', '.json', '.yaml', '.yml', '.log', '.xml', '.html'}:
        if b'\0' in data:
            raise MediaValidationError('text_contains_nul_or_ciphertext')
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                data.decode(encoding)
                return
            except UnicodeDecodeError:
                pass
        raise MediaValidationError('text_decode_failed')
    if suffix == '.pdf' and b'%PDF-' not in data[:1024]:
        raise MediaValidationError('pdf_signature_mismatch')
    if suffix in {'.docx', '.pptx', '.xlsx'} and not data.startswith(b'PK\x03\x04'):
        raise MediaValidationError('office_signature_mismatch')


def install_weixin_document_validation():
    from gateway.platforms import weixin
    adapter_class = weixin.WeixinAdapter
    if getattr(adapter_class, '_profile_document_validation_installed', False):
        return False
    original = adapter_class._download_file

    @wraps(original)
    async def download(adapter, item):
        file_item = item.get('file_item') or {}
        media = file_item.get('media') or {}
        filename = str(file_item.get('file_name') or 'document.bin')
        mime = weixin._mime_from_filename(filename)
        key_source = 'unresolved'
        try:
            key, key_source = _resolve_key(file_item, media)
            if not key and (media.get('encrypt_query_param') or str(media.get('encrypt_type') or '0') != '0'):
                raise MediaValidationError('encrypted_file_missing_key')
            data = await weixin._download_and_decrypt_media(
                adapter._poll_session, cdn_base_url=adapter._cdn_base_url,
                encrypted_query_param=media.get('encrypt_query_param'),
                aes_key_b64=key, full_url=media.get('full_url'), timeout_seconds=60.0,
            )
            _validate_document(data, filename, file_item)
            path = weixin.cache_document_from_bytes(data, filename)
            logger.info('weixin document validated key_source=%s bytes=%d suffix=%s',
                        key_source, len(data), Path(filename).suffix.lower()[:12])
            return path, mime
        except Exception as exc:
            # Never log the payload, key, CDN URL, or arbitrary exception message.
            reason = str(exc) if isinstance(exc, MediaValidationError) else type(exc).__name__
            logger.warning('weixin document rejected reason=%s key_source=%s suffix=%s',
                           reason, key_source, Path(filename).suffix.lower()[:12])
            return None, mime

    adapter_class._download_file = download
    adapter_class._profile_document_validation_installed = True
    logger.info('Weixin document decryption and validation installed')
    return True


# Correlate adapter entry with download results without logging protocol secrets.
from contextvars import ContextVar
import hashlib
import json

_receipt = ContextVar('weixin_document_receipt', default=None)


def _digest(value):
    return hashlib.sha256(str(value or '').encode()).hexdigest()[:16]


def install_weixin_receipt_diagnostics():
    from gateway.platforms import weixin as w
    cls = w.WeixinAdapter
    if getattr(cls, '_profile_receipt_diagnostics_installed', False):
        return False
    process_original = cls._process_message
    collect_original = cls._collect_media

    @wraps(process_original)
    async def process(adapter, message):
        items = message.get('item_list') or []
        files = []
        for item in items:
            candidates = [item, (item.get('ref_msg') or {}).get('message_item')]
            for candidate in candidates:
                if isinstance(candidate, dict) and isinstance(candidate.get('file_item'), dict):
                    f = candidate['file_item']; media = f.get('media') or {}
                    files.append({
                        'name_id': _digest(f.get('file_name')),
                        'suffix': re.sub(r'[^a-z0-9.]', '', Path(str(f.get('file_name') or '')).suffix.lower())[:12],
                        'key_fields': [scope + '.' + key for scope, obj in [('file_item', f), ('media', media)]
                                       for key in ('aes_key', 'aeskey') if obj.get(key)],
                    })
        state = {'id': _digest(str(getattr(adapter, '_account_id', '')) + ':' + str(message.get('message_id', ''))),
                 'attempted': 0, 'saved': 0}
        token = _receipt.set(state)
        sender = str(message.get('from_user_id') or '').strip()
        message_id = str(message.get('message_id') or '').strip()
        text = w._extract_text(items)
        dedup = adapter._dedup
        reason = 'eligible'
        if not sender: reason = 'missing_sender'
        elif sender == adapter._account_id: reason = 'self_message'
        elif message_id and dedup.contains(message_id): reason = 'duplicate_message_id'
        elif text and dedup.contains(f'content:{sender}:{hashlib.md5(text.encode()).hexdigest()}'):
            reason = 'duplicate_text'
        if reason == 'eligible':
            chat_type, chat_id = w._guess_chat_type(message, adapter._account_id)
            if chat_type == 'group':
                if adapter._group_policy in {'disabled', 'pairing'}:
                    reason = 'group_policy_denied'
                elif adapter._group_policy == 'allowlist' and chat_id not in adapter._group_allow_from:
                    reason = 'group_allowlist_denied'
            elif not adapter._is_dm_intake_allowed(sender):
                reason = 'dm_policy_denied'
        logger.info('weixin receipt entry trace=%s sender_id=%s gate=%s files=%s',
                    state['id'], _digest(sender), reason, json.dumps(files, separators=(',', ':')))
        try:
            return await process_original(adapter, message)
        except BaseException as exc:
            logger.warning('weixin receipt interrupted trace=%s exception=%s', state['id'], type(exc).__name__)
            raise
        finally:
            logger.info('weixin receipt exit trace=%s attempted=%d saved=%d',
                        state['id'], state['attempted'], state['saved'])
            _receipt.reset(token)

    @wraps(collect_original)
    async def collect(adapter, item, paths, types):
        state = _receipt.get()
        is_file = item.get('type') == w.ITEM_FILE
        before = len(paths)
        if state is not None and is_file:
            state['attempted'] += 1
        try:
            return await collect_original(adapter, item, paths, types)
        finally:
            if state is not None and is_file:
                saved = len(paths) - before
                state['saved'] += saved
                logger.info('weixin receipt download trace=%s name_id=%s saved=%d',
                            state['id'], _digest((item.get('file_item') or {}).get('file_name')), saved)

    cls._process_message = process
    cls._collect_media = collect
    cls._profile_receipt_diagnostics_installed = True
    logger.info('Weixin receipt diagnostics installed')
    return True
