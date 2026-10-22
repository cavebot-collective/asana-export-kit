import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch
from asana_export_kit.oauth import OAuthSession, callback_code, pkce, login
from asana_export_kit.core import hash_file, Archive, Exporter
from asana_export_kit.benchmarks import synthetic, run_benchmark, profile_archive
from asana_export_kit.consistency import compare_archives
from asana_export_kit.output import write_archive, manifest, verify
from asana_export_kit.downloads import download_attachment


class Extensions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pkce_rfc7636_vector(self):
        self.assertEqual(pkce('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),
            'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')

    def test_callback_rejects_csrf_duplicate_and_wrong_path(self):
        self.assertEqual(callback_code('/callback?state=match&code=secret', 'match'), 'secret')
        for url in ['/callback?state=other&code=x', '/callback?state=match&code=x&code=y',
                '/callback?state=match&state=match&code=x', '/elsewhere?state=match&code=x']:
            with self.assertRaises(ValueError):
                callback_code(url, 'match')
        with self.assertRaises(PermissionError):
            callback_code('/callback?state=match&error=access_denied', 'match')

    def test_oauth_refresh_and_rotation_in_memory(self):
        calls, clock = [], [1000]
        class Opener:
            def open(self, req, timeout):
                calls.append(req)
                return io.BytesIO(json.dumps({'access_token': 'access' + str(len(calls)),
                    'refresh_token': 'refresh' + str(len(calls)), 'expires_in': 3600}).encode())
        session = OAuthSession('id', 'secret', 'http://127.0.0.1:8765/callback', opener=Opener(), clock=lambda: clock[0])
        session.exchange({'grant_type': 'authorization_code', 'code': 'code', 'code_verifier': 'verifier'})
        self.assertEqual(session.token(), 'access1')
        clock[0] += 3550
        self.assertEqual(session.token(), 'access2')
        second = urllib.parse.parse_qs(calls[-1].data.decode())
        self.assertEqual(second['refresh_token'], ['refresh1'])
        self.assertEqual(session.refresh_token, 'refresh2')
        self.assertTrue(all(r.full_url == 'https://app.asana.com/-/oauth_token' for r in calls))
        self.assertNotIn('secret', calls[0].full_url)

    def test_oauth_bad_response_and_scopes_fail_closed(self):
        class Opener:
            def open(self, req, timeout):
                return io.BytesIO(b'{"error":"invalid"}')
        session = OAuthSession('id', 'secret', 'http://127.0.0.1:8765/callback', opener=Opener())
        with self.assertRaises(ValueError):
            session.exchange({})
        with self.assertRaises(ValueError):
            login('id', 'secret', 'tasks:write')
        with self.assertRaises(ValueError):
            login('id', 'secret', 'tasks:read', 'http://0.0.0.0:8765/callback')

    def test_manifest_hashing_does_not_read_whole_files(self):
        p = self.root / 'large.bin'
        with open(p, 'wb') as f:
            f.truncate(16 * 1024 * 1024)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file read')):
            size, _ = hash_file(p)
            self.assertEqual(size, 16 * 1024 * 1024)
            manifest(self.root)
            self.assertEqual(verify(self.root), [])

    def test_attachment_default_unlimited_and_explicit_limit(self):
        # A streaming fake crosses 1 GiB without allocating it or writing to disk.
        chunk = b'x' * (1024 * 1024)
        total = 1025 * len(chunk)
        class Response:
            status = 200
            def __init__(self): self.remaining = total
            def getheader(self, name): return str(total) if name == 'Content-Length' else None
            def read(self, size):
                if self.remaining == 0: return b''
                self.remaining -= len(chunk)
                return chunk
        class Conn:
            def __init__(self, *_): pass
            def request(self, *_, **kw): pass
            def getresponse(self): return Response()
            def close(self): pass
        class Sink:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def write(self, data): return len(data)
        a = {'gid': '1', 'download_url': 'https://example.com/file'}
        with patch('asana_export_kit.downloads.destination', return_value=(urllib.parse.urlsplit(a['download_url']), '8.8.8.8')), \
             patch('asana_export_kit.downloads.PinnedHTTPS', Conn), patch('builtins.open', return_value=Sink()), \
             patch('asana_export_kit.downloads.os.replace'):
            self.assertEqual(download_attachment(self.root, a), 'attachments/1.bin')
            with self.assertRaisesRegex(ValueError, 'size limit'):
                download_attachment(self.root, a, max_bytes=1024**3)

    def test_unlimited_unknown_length_still_enforces_explicit_ceiling(self):
        class Response:
            status = 200
            def getheader(self, name): return None
            def read(self, size): return b'12345'
        class Conn:
            def __init__(self, *_): pass
            def request(self, *_, **kw): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch('asana_export_kit.downloads.destination', return_value=(urllib.parse.urlsplit('https://example.com/x'), '8.8.8.8')), \
             patch('asana_export_kit.downloads.PinnedHTTPS', Conn):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                download_attachment(self.root, {'gid': '1', 'download_url': 'https://example.com/x'}, max_bytes=4)
        self.assertFalse((self.root / 'attachments/1.part').exists())

    def test_comparison_labels_observations_and_missing_resources(self):
        a, b = self.root / 'a', self.root / 'b'
        export = synthetic(2, 1)
        write_archive(a, export)
        write_archive(b, export)
        r = compare_archives(a, b)
        self.assertTrue(r['observed_equal'])
        self.assertFalse(r['transactional_snapshot_proven'])
        export['objects']['tasks'].pop('1000000000000000')
        write_archive(b, export)
        r = compare_archives(a, b)
        self.assertFalse(r['observed_equal'])
        self.assertEqual(len(r['differences']['no_longer_observed']), 1)

    def test_comparison_ignores_signed_url_rotation_but_compares_bytes(self):
        a, b = self.root / 'a', self.root / 'b'
        export = synthetic(0, 0)
        export['objects']['attachments'] = {'3': {'gid': '3', 'download_url': 'https://example.com/?sig=a', 'local_file': 'attachments/3.bin'}}
        for root in (a, b):
            (root / 'attachments').mkdir(parents=True)
            (root / 'attachments/3.bin').write_bytes(b'abc')
        write_archive(a, export)
        export['objects']['attachments']['3']['download_url'] = 'https://example.com/?sig=b'
        write_archive(b, export)
        self.assertTrue(compare_archives(a, b)['observed_equal'])
        (b / 'attachments/3.bin').write_bytes(b'def')
        manifest(b)
        self.assertFalse(compare_archives(a, b)['observed_equal'])

    def test_benchmark_and_profile(self):
        r = run_benchmark(self.root / 'bench', tasks=10, comments=3, attachment_bytes=4096)
        self.assertEqual(r['shape']['tasks'], 10)
        self.assertEqual(r['large_file']['bytes_read'], 4096)
        self.assertEqual(r['integrity_errors'], [])
        p = profile_archive(self.root / 'bench/archive')
        self.assertEqual(p['counts']['stories'], 30)
        self.assertGreater(p['task_note_bytes']['median'], 0)

    def test_extended_templates_and_memberships_are_hydrated(self):
        calls = []
        class Client:
            def get(self, path, q):
                calls.append((path, q))
                if path == '/users/me': return {'data': {'gid': '1'}}
                if path == '/workspaces': return {'data': [{'gid': '2'}]}
                if path == '/workspaces/2': return {'data': {'gid': '2'}}
                if path == '/project_templates': return {'data': [{'gid': '3'}]}
                if path == '/project_templates/3': return {'data': {'gid': '3', 'name': 'template'}}
                if path == '/projects/4': return {'data': {'gid': '4'}}
                if path == '/task_templates': return {'data': [{'gid': '5'}]}
                if path == '/task_templates/5': return {'data': {'gid': '5', 'name': 'task template'}}
                if path == '/memberships': return {'data': [{'gid': '6'}]}
                if path == '/memberships/6': return {'data': {'gid': '6', 'access_level': 'editor'}}
                return {'data': []}
        result = Exporter(Archive(self.root, Client()), extended=True).run(projects=['4'])
        self.assertEqual(result['objects']['project_templates']['3']['name'], 'template')
        self.assertEqual(result['objects']['memberships']['6']['access_level'], 'editor')
        self.assertTrue(any(p == '/task_templates/5' for p, q in calls))


if __name__ == '__main__':
    unittest.main()
