import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from asana_export_kit.core import APIError, Archive, Client, Exporter, NoRedirect, encode, retry_delay
from asana_export_kit.output import cell, manifest, normalize_connector, verify, write_archive
from asana_export_kit.cli import main
from asana_export_kit.downloads import destination, download_attachment


class FakeClient:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        return self.handler(path, params)


class Response(io.BytesIO):
    pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_pages_and_resume_without_network(self):
        def handler(path, q):
            if q.get("offset") == "next":
                return {"data": [{"gid": "2"}], "next_page": None}
            return {"data": [{"gid": "1"}], "next_page": {"offset": "next", "uri": "https://evil.invalid"}}
        client = FakeClient(handler)
        a = Archive(self.root, client)
        self.assertEqual(len(a.operation("/tasks", many=True)), 2)
        self.assertEqual(len(client.calls), 2)
        a = Archive(self.root, FakeClient(lambda *_: self.fail("network on cached operation")))
        self.assertEqual(a.operation("/tasks", many=True)[1]["gid"], "2")

    def test_interrupted_collection_restarts_without_stale_offset(self):
        def handler(path, q):
            if q.get("offset"):
                raise APIError(503)
            return {"data": [{"gid": "1"}], "next_page": {"offset": "expired"}}
        a = Archive(self.root, FakeClient(handler))
        self.assertIsNone(a.operation("/tasks", many=True))
        self.assertFalse(list((self.root / "operations").glob("*")))
        client = FakeClient(lambda p, q: {"data": [{"gid": "1"}, {"gid": "2"}], "next_page": None})
        self.assertEqual(len(Archive(self.root, client).operation("/tasks", many=True)), 2)
        self.assertNotIn("offset", client.calls[0][1])

    def test_repeated_cursor_fails(self):
        c = FakeClient(lambda *_: {"data": [], "next_page": {"offset": "same"}})
        with self.assertRaisesRegex(ValueError, "repeated"):
            Archive(self.root, c).operation("/tasks", many=True)

    def test_authentication_failure_aborts(self):
        def fail(*_):
            raise APIError(401)
        with self.assertRaises(APIError):
            Archive(self.root, FakeClient(fail)).operation("/users/me")

    def test_denial_is_not_cached_as_empty(self):
        def fail(*_):
            raise APIError(402, {"error": "premium"})
        a = Archive(self.root, FakeClient(fail))
        self.assertIsNone(a.operation("/portfolios", many=True))
        self.assertEqual(a.errors[0]["status"], 402)
        self.assertFalse((self.root / "operations").exists())

    def test_corrupt_cache_rejected(self):
        a = Archive(self.root, FakeClient(lambda *_: {"data": {"gid": "1"}}))
        a.operation("/tasks/1")
        path = next((self.root / "operations").glob("*"))
        record = json.loads(path.read_text())
        record["data"]["gid"] = "2"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "Corrupt"):
            a.operation("/tasks/1")

    def test_retry_after_and_read_only_transport(self):
        class Opener:
            def __init__(self):
                self.calls = []
            def open(self, req, timeout):
                self.calls.append(req)
                if len(self.calls) == 1:
                    raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "7"}, io.BytesIO(b'{}'))
                return Response(b'{"data": {"gid":"1"}}')
        opener, sleeps = Opener(), []
        client = Client("fake-token", opener=opener, sleep=sleeps.append)
        self.assertEqual(client.get("/users/me")["data"]["gid"], "1")
        self.assertIn(7, sleeps)
        self.assertTrue(all(r.get_method() == "GET" for r in opener.calls))
        self.assertTrue(all(r.full_url.startswith("https://app.asana.com/api/1.0/") for r in opener.calls))
        self.assertNotIn("fake-token", opener.calls[0].full_url)

    def test_api_redirects_and_absolute_paths_rejected(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        with self.assertRaises(ValueError):
            Client("fake").get("https://evil.invalid")
        with self.assertRaises(ValueError):
            Client("fake").get("/tasks/../../secrets")

    def test_retry_delay(self):
        self.assertEqual(retry_delay("5", 0), 5)
        self.assertEqual(retry_delay("garbage", 2), 4)

    def test_attachment_rejects_private_network_and_http(self):
        for url in ["http://example.com/x", "https://user:pass@example.com/x", "https://example.com:8080/x"]:
            with self.assertRaises(ValueError):
                destination(url)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                destination("https://example.com/file")

    def test_attachment_redirect_public_ip_pinned_no_credentials(self):
        connections = []
        class Conn:
            def __init__(self, host, ip):
                self.host, self.ip, self.headers = host, ip, {}
                connections.append(self)
            def request(self, method, path, headers):
                self.headers = headers
            def getresponse(self):
                response = Response(b"abc")
                response.status = 302 if len(connections) == 1 else 200
                response.getheader = lambda name: {"Location": "https://files.example.com/data", "Content-Length": "3"}.get(name)
                return response
            def close(self):
                pass
        answers = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch("socket.getaddrinfo", return_value=answers), patch("asana_export_kit.downloads.PinnedHTTPS", Conn):
            relative = download_attachment(self.root, {"gid": "1", "name": "../../evil", "download_url": "https://example.com/x?sig=secret"})
        self.assertEqual(relative, "attachments/1.bin")
        self.assertEqual((self.root / relative).read_bytes(), b"abc")
        self.assertEqual(len(connections), 2)
        self.assertTrue(all("Authorization" not in c.headers for c in connections))
        self.assertTrue(all(c.ip == "8.8.8.8" for c in connections))

    def test_csv_formula_and_long_gid_safety(self):
        for value in ["=1+1", "  +CMD", "-2", "@SUM(A1)", "\tfoo", "12345678901234567"]:
            self.assertTrue(cell(value).startswith("'"))
        self.assertEqual(cell("plain"), "plain")

    def test_manifest_tamper_missing_extra(self):
        (self.root / "data.json").write_text("original")
        manifest(self.root)
        self.assertEqual(verify(self.root), [])
        (self.root / "data.json").write_text("changed")
        self.assertTrue(any("Changed" in x for x in verify(self.root)))
        (self.root / "data.json").unlink()
        self.assertTrue(any("Missing" in x for x in verify(self.root)))
        (self.root / "extra").write_text("new")
        self.assertTrue(any("Unlisted" in x for x in verify(self.root)))

    def test_manifest_path_traversal_rejected(self):
        (self.root / "manifest.json").write_text(json.dumps({"files": [{"path": "../outside", "bytes": 0, "sha256": "x"}]}))
        self.assertIn("Unsafe", verify(self.root)[0])

    def test_connector_reconciliation_caps_and_exact_ids(self):
        def rec(key, data):
            return {"key": key, "result": {"structuredContent": {"data": data}}}
        task = {"gid": "12345678901234567", "name": "=danger", "notes": "original", "completed": True,
            "num_subtasks": 3, "subtasks": [{"gid": "2"}], "comments": []}
        records = [rec("projects/1", {"gid": "1", "name": "P", "sections": [], "task_counts": {"num_tasks": 1}}),
            rec("project-tasks/1", [task]), rec("tasks/12345678901234567", task)]
        export = normalize_connector(records)
        self.assertTrue(export["coverage"]["project_reconciliation"][0]["matches"])
        self.assertEqual(len(export["coverage"]["subtask_count_mismatches"]), 1)
        write_archive(self.root, export)
        self.assertEqual(verify(self.root), [])
        self.assertEqual(json.loads((self.root / "export.json").read_text())["objects"]["tasks"][task["gid"]]["notes"], "original")
        self.assertIn("'=danger", (self.root / "tables/tasks.csv").read_text(encoding="utf-8-sig"))

    def test_cli_partial_exit_and_zip(self):
        capture = self.root / "capture.json"
        capture.write_text("[]")
        output = self.root / "out"
        self.assertEqual(main(["normalize-connector", "--capture", str(capture), "--out", str(output)]), 2)
        self.assertEqual(main(["verify", str(output)]), 0)
        self.assertEqual(main(["zip", str(output), str(self.root / "archive.zip")]), 0)
        self.assertEqual(main(["zip", str(output), str(output / "bad.zip")]), 1)

    def test_graph_archive_completed_archived_orphan_recursive_cycles(self):
        def handler(path, q):
            if path == "/users/me":
                return {"data": {"gid": "1", "name": "Synthetic"}}
            if path == "/workspaces":
                return {"data": [{"gid": "2"}]}
            if path == "/workspaces/2":
                return {"data": {"gid": "2", "name": "Test"}}
            if path == "/workspaces/2/users":
                return {"data": [{"gid": "1"}]}
            if path == "/users/1":
                return {"data": {"gid": "1", "name": "Synthetic"}}
            if path == "/workspaces/2/projects":
                return {"data": [{"gid": "3" if q["archived"] == "false" else "4"}]}
            if path in ("/projects/3", "/projects/4"):
                return {"data": {"gid": path.rsplit("/", 1)[1], "name": "Project"}}
            if path in ("/projects/3/tasks", "/projects/4/tasks"):
                self.assertIn("completed_since", q)
                return {"data": [{"gid": "5"}]}
            if path == "/tasks":
                return {"data": [{"gid": "7"}]}
            if path == "/tasks/5/subtasks":
                return {"data": [{"gid": "6"}]}
            if path == "/tasks/6/dependencies":
                return {"data": [{"gid": "5"}]}
            if path in ("/tasks/5", "/tasks/6", "/tasks/7"):
                return {"data": {"gid": path.rsplit("/", 1)[1], "name": "Task", "completed": path.endswith("5")}}
            if path == "/users/1/user_task_list":
                return {"data": {"gid": "8"}}
            if path == "/portfolios":
                raise APIError(402)
            return {"data": [], "next_page": None}
        client = FakeClient(handler)
        export = Exporter(Archive(self.root, client)).run()
        self.assertEqual(set(export["objects"]["projects"]), {"3", "4"})
        self.assertEqual(set(export["objects"]["tasks"]), {"5", "6", "7"})
        self.assertTrue(export["objects"]["tasks"]["5"]["completed"])
        self.assertEqual(export["coverage"]["errors"][0]["status"], 402)
        self.assertEqual(sum(p == "/tasks/5" and not q for p, q in client.calls), 1)
        resumed = Exporter(Archive(self.root, FakeClient(handler))).run()
        self.assertEqual(set(resumed["objects"]["tasks"]), {"5", "6", "7"})

    def test_resume_different_principal_refused(self):
        client = FakeClient(lambda p, q: {"data": {"gid": "1"}} if p == "/users/me" else {"data": []})
        Exporter(Archive(self.root, client)).run()
        other = FakeClient(lambda *_: {"data": {"gid": "2"}})
        with self.assertRaisesRegex(ValueError, "principal"):
            Exporter(Archive(self.root, other)).run()


if __name__ == "__main__":
    unittest.main()
