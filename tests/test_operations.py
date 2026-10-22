import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from asana_export_kit.core import Archive, Exporter, encode, atomic, hash_file, digest
from asana_export_kit.output import write_archive, manifest
from asana_export_kit.purge import requests_for, save_plan, apply_plan
from asana_export_kit.schedule import watch
from asana_export_kit.storage import upload_archive


class Source:
    def __init__(self):
        self.task = {"gid": "12", "name": "Fixture", "parent": None, "num_subtasks": 0, "modified_at": "2026-01-01T00:00:00Z"}
        self.deleted, self.calls = [], []
        self.fail_delete = False
        self.denied = False
    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/users/me":
            return {"data": {"gid": "1"}}
        if path == "/tasks/12":
            return {"data": copy.deepcopy(self.task)}
        if path in [r["path"] for r in requests_for("12")]:
            return {"data": []}
        raise AssertionError(path)
    def delete_task(self, task):
        self.deleted.append(task)
        if self.fail_delete:
            raise OSError("ambiguous transport failure")


def fixture(root, source):
    archive = root / "archive"
    a = Archive(archive, source)
    for request in requests_for("12"):
        a.operation(request["path"], request["params"], request["many"])
    write_archive(archive, {"schema_version": 1, "source": "asana-rest", "principal": {"gid": "1"},
        "objects": {"tasks": {"12": copy.deepcopy(source.task)}}, "edges": [],
        "coverage": {"status": "partial", "errors": [], "limitations": []}})
    return archive


class Operations(unittest.TestCase):
    def test_purge_requires_exact_plan_and_acknowledgements(self):
        with tempfile.TemporaryDirectory() as d:
            root, source = Path(d), Source()
            archive = fixture(root, source)
            path = root / "plan.json"
            plan = save_plan(archive, ["12"], path)
            self.assertEqual(plan["blocked"], [])
            for confirmation, paused, accepted in [("wrong", True, True), (plan["plan_id"], False, True), (plan["plan_id"], True, False)]:
                with self.assertRaises(ValueError):
                    apply_plan(path, confirmation, source, paused, accepted)
            self.assertEqual(source.deleted, [])
            result = apply_plan(path, plan["plan_id"], source, True, True)
            self.assertEqual(result["items"], {"12": "deleted"})
            apply_plan(path, plan["plan_id"], source, True, True)
            self.assertEqual(source.deleted, ["12"])

    def test_purge_rejects_source_or_archive_changes(self):
        for target in ("source", "archive"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as d:
                root, source = Path(d), Source()
                archive = fixture(root, source)
                path = root / "plan.json"
                plan = save_plan(archive, ["12"], path)
                if target == "source":
                    source.task["name"] = "Changed after export"
                else:
                    (archive / "tasks.md").write_text("tampered")
                with self.assertRaises(ValueError):
                    apply_plan(path, plan["plan_id"], source, True, True)
                self.assertEqual(source.deleted, [])

    def test_purge_never_retries_an_uncertain_delete(self):
        with tempfile.TemporaryDirectory() as d:
            root, source = Path(d), Source()
            archive = fixture(root, source)
            path = root / "plan.json"
            plan = save_plan(archive, ["12"], path)
            source.fail_delete = True
            with self.assertRaises(OSError):
                apply_plan(path, plan["plan_id"], source, True, True)
            source.fail_delete = False
            with self.assertRaisesRegex(ValueError, "Unresolved"):
                apply_plan(path, plan["plan_id"], source, True, True)
            self.assertEqual(source.deleted, ["12"])

    def test_purge_blocks_wrong_identity_expiry_and_edited_plan(self):
        for condition in ("identity", "expiry", "tamper"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as d:
                root, source = Path(d), Source()
                archive = fixture(root, source); path = root / "plan.json"
                plan = save_plan(archive, ["12"], path)
                if condition == "identity":
                    original = source.get
                    source.get = lambda p, q=None: {"data": {"gid": "2"}} if p == "/users/me" else original(p, q)
                else:
                    plan["expires_at"] = "2000-01-01T00:00:00+00:00"
                    if condition == "expiry":
                        plan.pop("plan_id")
                        plan["plan_id"] = digest(encode(plan))
                    atomic(path, encode(plan))
                with self.assertRaises(ValueError):
                    apply_plan(path, plan["plan_id"], source, True, True)
                self.assertEqual(source.deleted, [])

    def test_purge_rechecks_after_batch_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            root, source = Path(d), Source()
            archive = fixture(root, source); path = root / "plan.json"
            plan = save_plan(archive, ["12"], path)
            original = source.get; count = [0]
            def changing(path, params=None):
                if path == "/tasks/12":
                    count[0] += 1
                    if count[0] == 3:
                        source.task["name"] = "Changed after preflight"
                return original(path, params)
            source.get = changing
            with self.assertRaises(ValueError):
                apply_plan(path, plan["plan_id"], source, True, True)
            self.assertEqual(source.deleted, [])

    def test_purge_blocks_attachment_metadata_without_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root, source = Path(d), Source()
            original = source.get
            source.get = lambda p, q=None: {"data": [{"gid": "77"}]} if p == "/attachments" else original(p, q)
            archive = fixture(root, source)
            plan = save_plan(archive, ["12"], root / "plan.json")
            self.assertIn("Attachment bytes missing", plan["blocked"][0]["reason"])

    def test_purge_blocks_missing_evidence_cascades_and_connector(self):
        for condition in ("evidence", "parent", "connector"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as d:
                root, source = Path(d), Source()
                if condition == "parent":
                    source.task["parent"] = {"gid": "99"}
                archive = fixture(root, source)
                if condition == "evidence":
                    next((archive / "operations").iterdir()).unlink()
                    manifest(archive)
                if condition == "connector":
                    file = archive / "export.json"
                    data = json.loads(file.read_text())
                    data["source"] = "asana-connector"
                    atomic(file, encode(data)); manifest(archive)
                    with self.assertRaises(ValueError):
                        save_plan(archive, ["12"], root / "plan.json")
                else:
                    plan = save_plan(archive, ["12"], root / "plan.json")
                    self.assertTrue(plan["blocked"])
                self.assertEqual(source.deleted, [])

    def test_upload_uses_argv_copy_then_download_check_and_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = fixture(root, Source())
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                self.assertNotIn("shell", kwargs)
                return SimpleNamespace(returncode=0)
            receipt = root / "receipt.json"
            report = upload_archive(archive, "drive:folder with 'quotes' $(no)", receipt, runner=runner)
            self.assertEqual([c[1] for c in calls], ["copy", "check"])
            self.assertIn("--immutable", calls[0])
            self.assertIn("--download", calls[1])
            self.assertEqual(report["state"], "verified")
            self.assertTrue(calls[0][3].endswith(hash_file(archive / "manifest.json")[1]))

    def test_upload_does_not_certify_copy_failure_or_local_changes(self):
        for condition in ("copy", "change"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as d:
                root = Path(d); archive = fixture(root, Source()); receipt = root / "receipt.json"
                def runner(args, **kwargs):
                    if condition == "change":
                        (archive / "tasks.md").write_text("changed")
                    return SimpleNamespace(returncode=1 if condition == "copy" else 0)
                with self.assertRaises(ValueError):
                    upload_archive(archive, "drive:backup", receipt, runner=runner)
                self.assertEqual(json.loads(receipt.read_text())["state"], "failed")

    def test_upload_refuses_an_active_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); archive = fixture(root, Source())
            (archive / ".export.lock").write_text("active")
            with self.assertRaises(ValueError):
                upload_archive(archive, "drive:backup", root / "receipt.json", runner=lambda *a, **k: self.fail("Upload attempted"))

    def test_watch_interval_includes_duration_and_no_backlog(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); clock = [0]; waits = []; calls = []
            def sleep(seconds):
                waits.append(seconds); clock[0] += seconds
            def runner(args, **kwargs):
                dest = Path(args[args.index("--out") + 1]); calls.append(dest)
                write_archive(dest, {"source": "synthetic", "objects": {}, "coverage": {"status": "partial"}})
                clock[0] += 15
                return SimpleNamespace(returncode=2)
            self.assertEqual(watch(root, 10, [], runs=2, env={"ASANA_ACCESS_TOKEN": "fixture"}, runner=runner,
                clock=lambda: clock[0], sleep=sleep), 2)
            self.assertEqual(waits, [0]); self.assertNotEqual(calls[0], calls[1])
            reports = [json.loads(p.read_text()) for p in (root / "receipts").glob("*.json")]
            self.assertTrue(all(r["effective_interval_seconds"] == 15 for r in reports))

    def test_watch_stops_on_failure_and_rejects_interactive_or_path_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            for arguments in (["--out=elsewhere"], ["--oauth"], ["--resume"]):
                with self.assertRaises(ValueError):
                    watch(d, 1, arguments, runs=1, env={"ASANA_ACCESS_TOKEN": "fixture"})
            calls = []
            def runner(*args, **kwargs):
                calls.append(args); return SimpleNamespace(returncode=1)
            with self.assertRaises(ValueError):
                watch(d, 1, [], runs=2, env={"ASANA_ACCESS_TOKEN": "fixture"}, runner=runner)
            self.assertEqual(len(calls), 1)

    def test_capture_omits_real_operations_and_records_choices(self):
        with tempfile.TemporaryDirectory() as d:
            class Capture:
                def __init__(self): self.calls=[]; self.root=Path(d)
                def operation(self, path, params=None, **kwargs):
                    self.calls.append(path)
                    if path == '/tasks/12': return {'gid':'12'}
                    return []
            archive = Capture()
            exporter = Exporter(archive, omit=['stories','attachments','statuses'])
            exporter.visit('tasks', '12'); exporter.statuses('12')
            self.assertNotIn('/tasks/12/stories', archive.calls)
            self.assertNotIn('/attachments', archive.calls)
            self.assertNotIn('/status_updates', archive.calls)
            self.assertTrue(any('stories' in x for x in exporter.gaps))


if __name__ == '__main__': unittest.main()
