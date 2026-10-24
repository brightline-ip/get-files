import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography import x509

from corpus.tls_material import create_material


class TLSMaterialTests(unittest.TestCase):
    def test_ca_subject_key_identifier_matches_its_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_material(root)
            ca=x509.load_pem_x509_certificate((root/'ca.pem').read_bytes())
            actual=ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            expected=x509.SubjectKeyIdentifier.from_public_key(ca.public_key())
            self.assertEqual(actual.digest,expected.digest)

    def test_leaf_identifiers_bind_each_leaf_to_the_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_material(root)
            ca=x509.load_pem_x509_certificate((root/'ca.pem').read_bytes())
            ca_key_id=ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
            for name in ['server.pem','n8n-client.pem']:
                with self.subTest(name=name):
                    leaf=x509.load_pem_x509_certificate((root/name).read_bytes())
                    subject=leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
                    authority=leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
                    self.assertEqual(subject.digest,x509.SubjectKeyIdentifier.from_public_key(leaf.public_key()).digest)
                    self.assertEqual(authority.key_identifier,ca_key_id)

    def test_spoke_defaults_to_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            config=create_material(Path(directory))
            self.assertEqual(config['host'],'127.0.0.1')
            self.assertEqual(json.loads((Path(directory)/'spoke.json').read_text(encoding='utf-8'))['host'],'127.0.0.1')
        source=(Path(__file__).resolve().parents[1]/'corpus'/'spoke.py').read_text(encoding='utf-8')
        self.assertNotIn('0.0.0.0',source)

    @unittest.skipUnless(sys.platform=='win32','icacls is Windows-only')
    def test_private_keys_are_restricted_to_current_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_material(root)
            for name in ['ca-key.pem','server-key.pem','n8n-client-key.pem']:
                listing=subprocess.run(['icacls.exe',str(root/name)],capture_output=True,text=True,timeout=30).stdout
                for broad in ['Everyone',r'BUILTIN\Users','Authenticated Users']:
                    self.assertNotIn(broad,listing,name)
                self.assertIn(os.environ['USERNAME'],listing,name)


if __name__=='__main__':unittest.main()
