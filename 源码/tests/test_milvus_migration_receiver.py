import hashlib
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import milvus_migration_receiver as receiver


class MigrationReceiverTests(unittest.TestCase):
    def test_source_aliases_and_ipv6_mapping_are_refused(self):
        for address in ['127.0.0.1', '::ffff:127.0.0.1']:
            with self.subTest(address=address), patch.object(receiver.socket, 'getaddrinfo', return_value=[(None, None, None, None, (address, 0))]):
                with self.assertRaises(AssertionError):
                    receiver.reject_source_host('alias.example')
        with patch.object(receiver.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('192.0.2.20', 0))]):
            receiver.reject_source_host('target.example')

    def test_existing_collection_is_refused_without_mutation(self):
        class Target:
            def list_databases(self):
                return [receiver.DATABASE]
            def use_database(self, name):
                assert name == receiver.DATABASE
            def has_collection(self, name):
                return True
        with self.assertRaisesRegex(AssertionError, 'no overwrite'):
            receiver.require_absent(Target())

    def test_source_rest_endpoint_is_also_refused(self):
        import yaml
        cfg = {'milvus': {'grpc': {'address':'target.example'},
                          'rest': {'endpoint':'http://127.0.0.1:19534'},
                          'management': {'endpoint':'http://target.example:9091'}}}
        def resolve(host, port):
            address = '127.0.0.1' if host == '127.0.0.1' else '192.0.2.20'
            return [(None, None, None, None, (address, 0))]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'target.yaml'
            path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
            with patch.object(receiver.socket, 'getaddrinfo', side_effect=resolve):
                with self.assertRaisesRegex(AssertionError, 'source server'):
                    receiver.configuration(path)

    def test_manifest_detects_tampering_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / '校验资料').mkdir()
            data = b'checked-data'
            (root / 'data.bin').write_bytes(data)
            (root / '校验资料/文件校验清单.sha256').write_text(hashlib.sha256(data).hexdigest() + '  data.bin\n', encoding='utf-8')
            receiver.verify_files(root)
            (root / 'data.bin').write_bytes(b'tampered')
            with self.assertRaisesRegex(AssertionError, 'mismatch'):
                receiver.verify_files(root)
            for name in ['../data.bin', '/data.bin', 'C:/data.bin', '..\\data.bin']:
                with self.subTest(name=name), self.assertRaises(AssertionError):
                    receiver.relative_path(root, name)


if __name__ == '__main__':
    unittest.main()
