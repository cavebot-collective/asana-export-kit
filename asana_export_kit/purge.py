"""Two-phase, explicitly selected task deletion. Never called by export/watch."""
import json
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .core import APIError, BASE, Client, TASK_FIELDS, STORY_FIELDS, atomic, digest, encode, gid, hash_file, now
from .downloads import download_attachment
from .output import verify

LIMITS = [
    "Only isolated leaf tasks are eligible: no parent, subtasks, dependencies or dependents.",
    "Verification covers the captured API surface, not unavailable revisions, rules or every Asana feature.",
    "Writers (including integrations) must remain paused; Asana supplies no atomic compare-and-delete here.",
    "DELETE moves tasks to Asana trash; this is not permanent erasure or deletion of external provider files.",
    "The archive must be retained. A valid checksum does not establish full restorability.",
]


def canonical(data):
    if isinstance(data, list):
        values = [canonical(x) for x in data]
        return sorted(values, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(data, dict):
        return {k: canonical(v) for k, v in data.items() if k not in ("download_url", "view_url", "local_file")}
    return data


def fingerprint(data):
    return digest(encode(canonical(data)))


def requests_for(task):
    base = f"/tasks/{gid(task)}"
    rows = [(base, {}, False), (base, {"opt_fields": TASK_FIELDS}, False)]
    rows += [(base + "/" + relation, {}, True) for relation in ("subtasks", "dependencies", "dependents")]
    rows += [(base + "/stories", {"opt_fields": STORY_FIELDS}, True),
        (base + "/time_tracking_entries", {"opt_fields": "gid,duration_minutes,entered_on,created_by.name,assignee.name,attributable_to.name"}, True),
        ("/attachments", {"parent": task}, True)]
    return [{"path": p, "params": q, "many": many} for p, q, many in rows]


def cached(root, request):
    path = root / "operations" / (digest(encode(request)) + ".json")
    if not path.is_file():
        raise ValueError("Missing successful capture: " + request["path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["request"] != request or record["sha256"] != digest(encode(record["data"])):
        raise ValueError("Invalid captured operation")
    return record["data"]


def build_plan(root, tasks):
    root = Path(root).resolve()
    if (root / ".export.lock").exists() or verify(root):
        raise ValueError("Purge requires an idle, integrity-verified archive")
    export = json.loads((root / "export.json").read_text(encoding="utf-8"))
    if export["source"] != "asana-rest":
        raise ValueError("Purge requires REST evidence; connector captures have insufficient coverage")
    selected = sorted({gid(t) for t in tasks})
    if not selected:
        raise ValueError("Select explicit task IDs; there is no implicit delete-all")
    targets, blocked = [], []
    for task in selected:
        try:
            row = export["objects"].get("tasks", {}).get(task)
            if not row:
                raise ValueError("Task is not in the archive")
            proofs, captured = [], {}
            for request in requests_for(task):
                data = cached(root, request)
                captured[request["path"]] = data
                proofs.append({"request": request, "fingerprint": fingerprint(data)})
            detail = captured[f"/tasks/{task}"]
            if not isinstance(detail, dict) or detail.get("gid") != task or "parent" not in detail or "num_subtasks" not in detail:
                raise ValueError("Task relationship fields missing from expanded capture")
            if detail["parent"] is not None or detail["num_subtasks"] != 0 or any(captured[f"/tasks/{task}/{k}"] for k in ("subtasks", "dependencies", "dependents")):
                raise ValueError("Task has parent/subtasks/dependencies/dependents; graph purge is not supported")
            files = []
            for item in captured["/attachments"]:
                attachment = gid(item["gid"])
                obj = export["objects"].get("attachments", {}).get(attachment, {})
                local = obj.get("local_file")
                if not local:
                    raise ValueError("Attachment bytes missing: " + attachment)
                path = (root / local).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("Invalid attachment file reference")
                req = {"path": "/attachments/" + attachment, "params": {}, "many": False}
                proofs.append({"request": req, "fingerprint": fingerprint(cached(root, req))})
                size, sha = hash_file(path)
                files.append({"gid": attachment, "bytes": size, "sha256": sha})
            targets.append({"gid": task, "name": row.get("name", ""), "proofs": proofs, "attachments": files})
        except (ValueError, KeyError) as exc:
            blocked.append({"gid": task, "reason": str(exc)})
    plan = {"schema_version": 1, "operation": "delete-isolated-tasks", "created_at": now(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "archive": str(root), "manifest_sha256": hash_file(root / "manifest.json")[1],
        "principal": export["principal"]["gid"], "selected": selected, "targets": targets,
        "blocked": blocked, "coverage": export["coverage"], "limitations": LIMITS}
    return {**plan, "plan_id": digest(encode(plan))}


def save_plan(root, tasks, destination):
    destination = Path(destination).resolve()
    if destination.is_relative_to(Path(root).resolve()) or destination.exists():
        raise ValueError("Use a new plan path outside the sealed archive")
    plan = build_plan(root, tasks)
    atomic(destination, encode(plan))
    return plan


def live_read(client, request):
    rows, seen, offset = [], set(), None
    while True:
        params = dict(request["params"])
        if request["many"]:
            params["limit"] = "100"
            if offset:
                params["offset"] = offset
        response = client.get(request["path"], params)
        if not request["many"]:
            return response["data"]
        if not isinstance(response["data"], list):
            raise ValueError("Expected live list")
        rows.extend(response["data"])
        page = response.get("next_page")
        if not page:
            return rows
        offset = page.get("offset")
        if not isinstance(offset, str) or not offset or offset in seen:
            raise ValueError("Invalid live pagination")
        seen.add(offset)


def recheck(client, target):
    attachments = {}
    for proof in target["proofs"]:
        data = live_read(client, proof["request"])
        if fingerprint(data) != proof["fingerprint"]:
            raise ValueError("Source changed or access differs; re-export task " + target["gid"])
        if proof["request"]["path"].startswith("/attachments/"):
            attachments[data["gid"]] = data
    if target["attachments"]:
        scratch = "/var/tmp" if Path("/var/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="asana-purge-check-", dir=scratch) as tmp:
            for expected in target["attachments"]:
                relative = download_attachment(Path(tmp), attachments[expected["gid"]], max_bytes=expected["bytes"])
                if hash_file(Path(tmp) / relative) != (expected["bytes"], expected["sha256"]):
                    raise ValueError("Attachment bytes changed; re-export before deleting")


class DeleteClient(Client):
    def delete_task(self, task):
        # This method deliberately has no retry loop. A timeout can mean applied.
        self.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        token = self.token() if callable(self.token) else self.token
        request = urllib.request.Request(BASE + "/tasks/" + gid(task), method="DELETE",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise ValueError("Unexpected DELETE response; reconcile journal before retrying")
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from None


def apply_plan(plan_path, confirmation, client, writers_paused=False, accept_limits=False):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_id = plan.pop("plan_id")
    if digest(encode(plan)) != plan_id or confirmation != plan_id:
        raise ValueError("Plan changed or confirmation does not match its exact ID")
    if not writers_paused or not accept_limits:
        raise ValueError("Apply requires --writers-paused and --accept-coverage-limits")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(plan["expires_at"]):
        raise ValueError("Purge plan expired; make a fresh plan")
    if plan["blocked"] or not plan["targets"]:
        raise ValueError("Plan is blocked or empty; no deletion attempted")
    root = Path(plan["archive"])
    fresh = build_plan(root, plan["selected"])
    for key in ("manifest_sha256", "principal", "targets", "blocked"):
        if fresh[key] != plan[key]:
            raise ValueError("Archive/plan evidence changed")
    if client.get("/users/me")["data"]["gid"] != plan["principal"]:
        raise ValueError("Delete credential belongs to a different principal")
    # Same directory/plan ID lock cannot be sidestepped by changing journal path.
    lock_path = plan_path.parent / (".purge-" + plan_id + ".lock")
    journal_path = plan_path.parent / ("purge-" + plan_id + ".journal.json")
    try:
        with open(lock_path, "x") as stream:
            stream.write(now())
    except FileExistsError:
        raise ValueError("Purge plan is locked; investigate any interrupted apply") from None
    archive_lock = root / ".export.lock"
    owns_archive_lock = False
    try:
        with open(archive_lock, "x") as stream:
            stream.write("purge " + plan_id)
        owns_archive_lock = True
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {"plan_id": plan_id, "items": {}}
        if journal["plan_id"] != plan_id or any(v != "deleted" for v in journal["items"].values()):
            raise ValueError("Unresolved DELETE intent; reconcile manually, never blindly retry")
        pending = [t for t in plan["targets"] if t["gid"] not in journal["items"]]
        # Check every candidate before the first deletion, then check again at
        # each mutation. External writer quiescence is still required.
        for target in pending:
            recheck(client, target)
        for target in pending:
            if verify(root) or hash_file(root / "manifest.json")[1] != plan["manifest_sha256"]:
                raise ValueError("Archive changed during purge")
            recheck(client, target)
            if datetime.now(timezone.utc) >= datetime.fromisoformat(plan["expires_at"]):
                raise ValueError("Purge plan expired during verification; no further deletion attempted")
            journal["items"][target["gid"]] = "intent"
            atomic(journal_path, encode(journal))
            client.delete_task(target["gid"])
            journal["items"][target["gid"]] = "deleted"
            atomic(journal_path, encode(journal))
        return journal
    finally:
        if owns_archive_lock:
            archive_lock.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
