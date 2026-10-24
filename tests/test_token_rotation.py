import base64
import tempfile
import unittest
from pathlib import Path

from corpus.token_rotation import rotate_local_token


class TokenRotationTests(unittest.TestCase):
    def test_rotation_replaces_existing_token_with_fresh_encoded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'service-token.txt';target.write_text('old-fixture-token')
            rotate_local_token(root)
            encoded=target.read_bytes()
            self.assertNotEqual(encoded,b'old-fixture-token')
            self.assertEqual(len(base64.b64decode(encoded,validate=True)),48)
            self.assertFalse(list(root.glob('token-pending-*')))

    def test_rotation_requires_an_existing_regular_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'Existing local token file is required'):
                rotate_local_token(directory)


if __name__=='__main__':unittest.main()
