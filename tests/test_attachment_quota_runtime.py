"""Exercise deployed quota parsing, attachment capture and analysis without network."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

class AttachmentQuotaTest(unittest.TestCase):
    def test_200_files_and_session_isolation(self):
        source = Path(os.environ.get('QUOTA_PLUGIN_PATH', str(Path(__file__).parents[1] / '__init__.py')))
        spec = importlib.util.spec_from_file_location('quota_test_plugin', source, submodule_search_locations=[str(source.parent)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'HERMES_HOME': tmp,
            plugin.ATTACHMENT_ROOTS_ENV: tmp + '/cache/documents',
            'PROFILE_RAG_MCP_ATTACHMENT_SHARED_ROOT': tmp + '/cache/documents',
            plugin.ATTACHMENT_BATCH_MAX_FILES_ENV: '20',
            plugin.ATTACHMENT_SESSION_MAX_FILES_ENV: '200',
        }):
            cache = Path(tmp) / 'cache/documents'; cache.mkdir(parents=True)
            home = Path(tmp) / 'profiles/employee-smoke'; home.mkdir(parents=True)
            plugin._profile_name = lambda: 'employee-smoke'
            plugin._current_profile_home = lambda: home
            self.assertEqual(plugin._attachment_session_max_files(), 200)
            source = SimpleNamespace(platform='weixin', user_id='smoke', profile='employee-smoke')
            paths = []
            for batch in range(10):
                files = []
                for i in range(batch * 20, (batch + 1) * 20):
                    f = cache / f'doc_{i:012x}_check{i}.md'
                    f.write_text('quota smoke success'); files.append(str(f))
                paths.extend(files)
                plugin._capture_pending_attachments(SimpleNamespace(media_urls=files), source)
                plugin._on_pre_llm_call(session_id='smoke', platform='weixin', sender_id='smoke')
            self.assertEqual(len(plugin._load_session_grants('smoke')), 200)
            extra = cache / 'doc_abcdefabcdef_extra.md'; extra.write_text('extra')
            plugin._capture_pending_attachments(SimpleNamespace(media_urls=[str(extra)]), source)
            plugin._on_pre_llm_call(session_id='smoke', platform='weixin', sender_id='smoke')
            self.assertEqual(len(plugin._load_session_grants('smoke')), 200)
            handler = plugin._analyze_attachment_handler(plugin.DEFAULT_ENDPOINT, 5)
            result = json.loads(handler({'attachment_path': paths[0]}, session_id='smoke'))
            self.assertEqual(result.get('text'), 'quota smoke success')
            result = json.loads(handler({'attachment_path': paths[0]}, session_id='other'))
            self.assertEqual(result.get('error_type'), 'attachment_missing')

if __name__ == '__main__':
    unittest.main()
