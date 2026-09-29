import base64
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('wecom_configuration', Path(__file__).parents[1]/'scripts/configure_wecom.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def values(self):
        return dict(corp_id='wwtest', agent_id='10001', corp_secret='testsecret',
                    token='Token123', encoding_aes_key=base64.b64encode(bytes(32)).decode().rstrip('='))

    def test_blank_form_fails_without_echoing_secrets(self):
        values = self.values(); values['agent_id']=''
        with self.assertRaises(ValueError) as caught: module.validate(values)
        self.assertIn('agent_id',str(caught.exception))
        self.assertNotIn(values['corp_secret'],str(caught.exception))

    def test_merge_is_scoped_and_loopback_only(self):
        original={'platforms':{'telegram':{'enabled':True}},'gateway':{'profile_routes':[]}}
        config=module.merge_config(original,module.validate(self.values()))
        self.assertNotIn('wecom_callback',original['platforms'])
        self.assertEqual(config['platforms']['telegram'],{'enabled':True})
        self.assertEqual(config['platforms']['wecom_callback']['extra']['host'],'127.0.0.1')
        self.assertEqual(config['wecom_binding'],{'enabled':True,'corp_id':'wwtest'})

    def test_invalid_aes_and_foreign_bindings_rejected(self):
        values=self.values();values['encoding_aes_key']='wrong'
        with self.assertRaises(ValueError):module.validate(values)
        original={'gateway':{'profile_routes':[{'platform':'wecom_callback','chat_id':'other:employee'}]}}
        with self.assertRaises(ValueError):module.merge_config(original,self.values())
