import asyncio
import base64
from types import SimpleNamespace as NS

import pytest

from weixin_media import _resolve_key, _validate_document, MediaValidationError, install_weixin_document_validation

KEY = bytes(range(16))


@pytest.mark.parametrize('scope,field,encoded', [
    ('media', 'aes_key', base64.b64encode(KEY).decode()),
    ('media', 'aes_key', base64.b64encode(KEY.hex().encode()).decode()),
    ('file_item', 'aeskey', KEY.hex()),
    ('file_item', 'aes_key', base64.b64encode(KEY).decode()),
    ('media', 'aeskey', KEY.hex()),
])
def test_key_formats(scope, field, encoded):
    file_item = {'media': {}}
    (file_item['media'] if scope == 'media' else file_item)[field] = encoded
    result, source = _resolve_key(file_item, file_item['media'])
    assert base64.b64decode(result) == KEY
    assert source == scope + '.' + field


def test_conflicting_and_malformed_keys_rejected():
    with pytest.raises(MediaValidationError, match='conflicting_aes_keys'):
        _resolve_key({'aeskey': KEY.hex()}, {'aes_key': bytes(range(16,32)).hex()})
    with pytest.raises(MediaValidationError, match='invalid_aes_key'):
        _resolve_key({'aeskey': 'not-a-key'}, {})


@pytest.mark.parametrize('data,name,metadata,reason', [
    (b'abc\0', 'a.md', {}, 'text_contains_nul'),
    (b'\xff', 'a.md', {}, 'text_decode_failed'),
    (b'abc', 'a.md', {'len':'4'}, 'plaintext_length_mismatch'),
    (b'abc', 'a.pdf', {}, 'pdf_signature_mismatch'),
    (b'abc', 'a.docx', {}, 'office_signature_mismatch'),
])
def test_invalid_documents(data, name, metadata, reason):
    with pytest.raises(MediaValidationError, match=reason):
        _validate_document(data, name, metadata)


def test_actual_weixin_download_decrypts_all_six_and_never_caches_ciphertext(monkeypatch, caplog):
    from gateway.platforms import weixin as w
    # Restore class after test; installer patches it globally.
    monkeypatch.setattr(w.WeixinAdapter, '_profile_document_validation_installed', False, raising=False)
    monkeypatch.setattr(w.WeixinAdapter, '_download_file', w.WeixinAdapter._download_file)
    assert install_weixin_document_validation()
    cached = []
    monkeypatch.setattr(w, 'cache_document_from_bytes', lambda data, name: cached.append(data) or '/tmp/test.md')
    adapter = object.__new__(w.WeixinAdapter)
    adapter._poll_session = None; adapter._cdn_base_url = 'https://example.invalid'
    plaintexts = [('# 中文文件 ' + str(i)).encode() for i in range(6)]
    ciphertext = b''
    async def download(*args, **kwargs): return ciphertext
    monkeypatch.setattr(w, '_download_bytes', download)
    async def run():
        nonlocal ciphertext
        for plain in plaintexts:
            ciphertext = w._aes128_ecb_encrypt(plain, KEY)
            item = {'file_item': {'file_name':'test.md', 'len':str(len(plain)), 'aeskey':KEY.hex(),
                                 'media': {'encrypt_query_param':'hidden-query', 'encrypt_type':1}}}
            assert (await adapter._download_file(item))[0]
        assert cached == plaintexts
        item['file_item'].pop('aeskey')
        assert (await adapter._download_file(item))[0] is None
        assert len(cached) == 6
        item['file_item']['aeskey'] = bytes(range(16,32)).hex()
        assert (await adapter._download_file(item))[0] is None
        assert len(cached) == 6
        assert KEY.hex() not in caplog.text
        assert 'hidden-query' not in caplog.text
        assert 'encrypted_file_missing_key' in caplog.text
    asyncio.run(run())


def test_receipt_diagnostics_cover_pre_download_drops_without_secrets(monkeypatch, caplog):
    from gateway.platforms import weixin as w
    from weixin_media import install_weixin_receipt_diagnostics
    import logging
    caplog.set_level(logging.INFO)
    cls = w.WeixinAdapter
    monkeypatch.setattr(cls, '_profile_receipt_diagnostics_installed', False, raising=False)
    monkeypatch.setattr(cls, '_process_message', cls._process_message)
    monkeypatch.setattr(cls, '_collect_media', cls._collect_media)
    assert install_weixin_receipt_diagnostics()
    a = object.__new__(cls)
    a._poll_session = object(); a._account_id = 'test-bot'
    a._dedup = w.MessageDeduplicator(); a._is_dm_intake_allowed = lambda sender: False
    a._group_policy = 'disabled'
    message = {'message_id':'unique-msg', 'from_user_id':'test-sender', 'to_user_id':'test-bot',
               'context_token':'PRIVATE-CONTEXT', 'item_list':[{'type':w.ITEM_FILE, 'file_item':{
                   'file_name':'private-filename.md', 'aeskey':KEY.hex(),
                   'media':{'encrypt_query_param':'PRIVATE-CDN'}}}]}
    async def run():
        await a._process_message(message)
        assert a._dedup.contains('unique-msg')  # core behavior unchanged
        assert 'gate=dm_policy_denied' in caplog.text
        await a._process_message(message)
        assert 'gate=duplicate_message_id' in caplog.text
        assert 'attempted=0 saved=0' in caplog.text
        for private in ['unique-msg', 'test-sender', 'PRIVATE-CONTEXT', 'PRIVATE-CDN', KEY.hex(), 'private-filename.md']:
            assert private not in caplog.text
    asyncio.run(run())
