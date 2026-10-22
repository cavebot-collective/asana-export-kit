"""Read-only REST transport, recoverable archive operations, and graph traversal."""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = "https://app.asana.com/api/1.0"
EPOCH = "1970-01-01T00:00:00.000Z"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path, chunk_size=1024 * 1024):
    """Bounded-memory integrity hashing, including multi-gigabyte attachments."""
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def encode(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def gid(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Expected a numeric Asana GID")
    return value


class APIError(Exception):
    def __init__(self, status, body=None):
        self.status, self.body = status, body
        super().__init__(f"Asana HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API bearer credentials must never follow a redirect.
        return None


def retry_delay(header, attempt):
    if header:
        try:
            return max(0, float(header))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    return min(60, 2 ** attempt)


class Client:
    def __init__(self, token, rpm=120, retries=6, opener=None, sleep=time.sleep):
        if not token:
            raise ValueError("ASANA_ACCESS_TOKEN is missing")
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.token, self.interval, self.retries = token, 60 / rpm, retries
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep, self.last = sleep, 0

    def get(self, path, params=None):
        if not re.fullmatch(r"/[A-Za-z0-9_/]+", path) or "//" in path:
            raise ValueError("Only relative Asana API paths are allowed")
        query = urllib.parse.urlencode(sorted((params or {}).items()))
        url = BASE + path + ("?" + query if query else "")
        for attempt in range(self.retries + 1):
            self.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            token = self.token() if callable(self.token) else self.token
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                "Accept": "application/json", "User-Agent": "asana-export-kit/0.2.0"}, method="GET")
            try:
                with self.opener.open(req, timeout=60) as response:
                    body = json.load(response)
                if not isinstance(body, dict) or "data" not in body:
                    raise APIError(502, {"message": "Response missing data"})
                return body
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read())
                except (ValueError, UnicodeError):
                    body = {"message": "Non-JSON HTTP error"}
                if (exc.code in (429, 424) or exc.code >= 500) and attempt < self.retries:
                    self.sleep(retry_delay(exc.headers.get("Retry-After"), attempt))
                    continue
                raise APIError(exc.code, body) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.retries:
                    raise APIError(0, {"message": "Transport failure; retry this run"}) from None
                self.sleep(retry_delay(None, attempt))
        raise AssertionError("unreachable")


class Archive:
    """Cache only completed operations, never resume from an expired page offset."""
    def __init__(self, root, client):
        self.root, self.client = Path(root), client
        self.root.mkdir(parents=True, exist_ok=True)
        self.errors, self.operations = [], {}

    def operation(self, path, params=None, many=False, refresh=False):
        params = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in (params or {}).items()}
        signature = {"path": path, "params": params, "many": many}
        key = digest(encode(signature))
        target = self.root / "operations" / (key + ".json")
        if target.exists() and not refresh:
            record = json.loads(target.read_text(encoding="utf-8"))
            if record["request"] != signature or record["sha256"] != digest(encode(record["data"])):
                raise ValueError("Corrupt operation cache: " + key)
            self.operations[key] = record["request"]
            return record["data"]
        results, seen, offset = [], set(), None
        while True:
            query = dict(params)
            if many:
                query["limit"] = "100"
                if offset is not None:
                    query["offset"] = offset
            try:
                response = self.client.get(path, query)
            except APIError as exc:
                error = {"request": signature, "offset": offset, "status": exc.status,
                    "error": exc.body, "at": now()}
                self.errors.append(error)
                atomic(self.root / "failures" / (key + ".json"), encode(error))
                if exc.status == 401:
                    raise
                return None
            page = {"request": {"path": path, "params": query}, "retrieved_at": now(), "response": response}
            atomic(self.root / "raw" / (digest(encode(page)) + ".json"), encode(page))
            data = response["data"]
            if not many:
                results = data
                break
            if not isinstance(data, list):
                raise ValueError("Expected list: " + path)
            results.extend(data)
            next_page = response.get("next_page")
            if not next_page:
                break
            offset = next_page.get("offset")
            if not isinstance(offset, str) or not offset or offset in seen:
                raise ValueError("Invalid or repeated pagination offset: " + path)
            seen.add(offset)
        record = {"request": signature, "data": results, "sha256": digest(encode(results)), "completed_at": now()}
        atomic(target, encode(record))
        self.operations[key] = signature
        return results


TASK_FIELDS = ("gid,name,resource_type,resource_subtype,notes,html_notes,created_at,modified_at,"
    "created_by.name,completed,completed_at,assignee.name,assignee_status,assignee_section.name,"
    "due_at,due_on,start_at,start_on,parent.name,permalink_url,num_subtasks,"
    "memberships.project.name,memberships.section.name,projects.name,tags.name,followers.name,"
    "custom_fields.name,custom_fields.resource_subtype,custom_fields.display_value,custom_fields.enum_value,"
    "custom_fields.multi_enum_values,custom_fields.number_value,custom_fields.text_value,custom_fields.date_value,"
    "dependencies.name,dependents.name,liked,likes.user.name,num_likes,approval_status,actual_time_minutes")
STORY_FIELDS = "gid,resource_type,resource_subtype,created_at,created_by.name,text,html_text,type,is_edited,is_pinned,source"

LIMITATIONS = [
    "Only data visible to the authenticated principal is accessible; this is not an administrator organization export.",
    "Unassigned tasks with no discovered project, tag, or graph relationship cannot be exhaustively discovered. Supply --task GIDs for known orphans.",
    "Messages/inbox, deleted records, rules, forms, dashboards, audit logs, app configuration and revision histories are not exported by this version.",
    "The archive is a time-spanning collection, not a transactionally consistent snapshot or a directly restorable Asana backup.",
    "External linked attachments may require their own service authentication; URL metadata does not guarantee file bytes.",
    "Only selected optional fields are requested. Raw means exact returned JSON, not every field the service could expose.",
]


class Exporter:
    def __init__(self, archive, attachments=False, time_entries=True, max_attachment_bytes=None, extended=False):
        self.a, self.download, self.time_entries = archive, attachments, time_entries
        self.max_attachment_bytes, self.extended = max_attachment_bytes, extended
        self.objects, self.edges, self.gaps = {}, [], list(LIMITATIONS)
        if not extended:
            self.gaps.append("Project/task templates and unified membership detail are omitted; enable --extended to collect them.")
        self.pending, self.seen = deque(), set()

    def enqueue(self, kind, value):
        if value:
            self.pending.append((kind, gid(value)))

    def put(self, kind, obj):
        if obj and obj.get("gid"):
            self.objects.setdefault(kind, {})[obj["gid"]] = obj

    def one(self, kind, id_, params=None):
        obj = self.a.operation(f"/{kind}/{gid(id_)}", params)
        self.put(kind, obj)
        return obj or {}

    def listing(self, path, params=None, kind=None, parent=None, relation=None):
        rows = self.a.operation(path, params, many=True)
        for obj in rows or []:
            if kind:
                self.put(kind, obj)
            if parent and obj.get("gid"):
                self.edges.append({"parent": parent, "relation": relation or path.rsplit("/", 1)[-1], "child": obj["gid"]})
        return rows or []

    def children(self, path, params, kind, parent=None, relation=None):
        rows = self.listing(path, params, parent=parent, relation=relation)
        for obj in rows:
            self.enqueue(kind, obj["gid"])
        return rows

    def statuses(self, id_):
        self.children("/status_updates", {"parent": id_}, "status_updates", id_, "status_updates")

    def attachments(self, id_):
        self.children("/attachments", {"parent": id_}, "attachments", id_, "attachments")

    def run(self, workspaces=(), tasks=(), projects=()):
        # Refresh identity even when resuming, to prevent mixing two accounts.
        me = self.a.operation("/users/me", refresh=True)
        if not me:
            raise ValueError("Could not retrieve identity")
        meta_path = self.a.root / "run.json"
        config = {"principal": me["gid"], "workspaces": sorted(workspaces), "task_seeds": sorted(tasks),
            "project_seeds": sorted(projects), "download_attachments": self.download, "time_entries": self.time_entries,
            "max_attachment_bytes": self.max_attachment_bytes, "extended": self.extended}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            # v0.1.0 resumes retain their original attachment cap and coverage.
            meta["config"].setdefault("max_attachment_bytes", 1024 ** 3)
            meta["config"].setdefault("extended", False)
            if meta["config"] != config:
                raise ValueError("Resume configuration/principal differs; use a new output directory")
        else:
            meta = {"schema_version": 1, "started_at": now(), "config": config}
            atomic(meta_path, encode(meta))
        self.put("users", me)
        available = self.listing("/workspaces", kind="workspaces")
        if workspaces and set(workspaces) - {w["gid"] for w in available}:
            raise ValueError("Requested workspace is not accessible")
        for w in available:
            if not workspaces or w["gid"] in workspaces:
                self.enqueue("workspaces", w["gid"])
        for t in tasks:
            self.enqueue("tasks", t)
        for p in projects:
            self.enqueue("projects", p)
        while self.pending:
            kind, id_ = self.pending.popleft()
            if (kind, id_) in self.seen:
                continue
            self.seen.add((kind, id_))
            self.visit(kind, id_)
            if len(self.seen) % 25 == 0:
                print(f"Collected {len(self.seen)} objects; {len(self.pending)} queued; {len(self.a.errors)} unavailable operations", flush=True)
        return {"schema_version": 1, "source": "asana-rest", "started_at": meta["started_at"], "finished_at": now(),
            "principal": me, "objects": self.objects, "edges": self.edges,
            "coverage": {"status": "partial", "limitations": self.gaps, "errors": self.a.errors,
                "completed_operations": len(self.a.operations)}}

    def visit(self, kind, id_):
        if kind == "workspaces":
            self.one(kind, id_)
            users = self.children(f"/workspaces/{id_}/users", {}, "users")
            self.children(f"/workspaces/{id_}/teams", {}, "teams")
            self.children(f"/workspaces/{id_}/tags", {}, "tags")
            self.children(f"/workspaces/{id_}/custom_fields", {}, "custom_fields")
            self.listing("/time_periods", {"workspace": id_}, "time_periods")
            for archived in (False, True):
                self.children(f"/workspaces/{id_}/projects", {"archived": archived}, "projects")
            self.children("/goals", {"workspace": id_}, "goals")
            if self.extended:
                self.children("/project_templates", {"workspace": id_}, "project_templates")
            for user in users:
                u = gid(user["gid"])
                self.children("/tasks", {"workspace": id_, "assignee": u, "completed_since": EPOCH}, "tasks")
                self.children("/portfolios", {"workspace": id_, "owner": u}, "portfolios")
                task_list = self.a.operation(f"/users/{u}/user_task_list", {"workspace": id_})
                if task_list:
                    self.put("user_task_lists", task_list)
                    self.children(f"/user_task_lists/{gid(task_list['gid'])}/tasks", {"completed_since": EPOCH}, "tasks")
        elif kind == "teams":
            self.one(kind, id_)
            self.listing(f"/teams/{id_}/team_memberships", kind="team_memberships", parent=id_)
            self.children(f"/teams/{id_}/users", {}, "users")
        elif kind == "projects":
            p = self.one(kind, id_)
            # Supplement default full response with optional brief pointer.
            extra = self.a.operation(f"/projects/{id_}", {"opt_fields": "project_brief,html_notes,custom_field_settings.custom_field.name"})
            if extra:
                p.update(extra)
            self.enqueue("project_briefs", (p.get("project_brief") or {}).get("gid"))
            self.listing(f"/projects/{id_}/sections", kind="sections", parent=id_)
            self.listing(f"/projects/{id_}/project_memberships", kind="project_memberships", parent=id_)
            self.listing(f"/projects/{id_}/custom_field_settings", kind="custom_field_settings", parent=id_)
            self.children(f"/projects/{id_}/tasks", {"completed_since": EPOCH}, "tasks", id_, "tasks")
            if self.extended:
                self.children("/task_templates", {"project": id_}, "task_templates", id_, "task_templates")
                self.children("/memberships", {"parent": id_}, "memberships", id_, "memberships")
            self.statuses(id_)
            self.attachments(id_)
        elif kind == "tasks":
            t = self.one(kind, id_)
            if not t:
                return
            extra = self.a.operation(f"/tasks/{id_}", {"opt_fields": TASK_FIELDS})
            if extra:
                t.update(extra)
            self.enqueue("tasks", (t.get("parent") or {}).get("gid"))
            for sub in ("subtasks", "dependencies", "dependents"):
                self.children(f"/tasks/{id_}/{sub}", {}, "tasks", id_, sub)
            self.listing(f"/tasks/{id_}/stories", {"opt_fields": STORY_FIELDS}, "stories", id_)
            if self.time_entries:
                self.listing(f"/tasks/{id_}/time_tracking_entries",
                    {"opt_fields": "gid,duration_minutes,entered_on,created_by.name,assignee.name,attributable_to.name"}, "time_tracking_entries", id_)
            self.attachments(id_)
            for f in t.get("custom_fields", []):
                self.enqueue("custom_fields", f["gid"])
        elif kind == "tags":
            self.one(kind, id_)
            self.children(f"/tags/{id_}/tasks", {"completed_since": EPOCH}, "tasks")
        elif kind == "portfolios":
            self.one(kind, id_)
            items = self.listing(f"/portfolios/{id_}/items", parent=id_, relation="items")
            for item in items:
                target = {"project": "projects", "portfolio": "portfolios", "goal": "goals"}.get(item.get("resource_type"))
                if target:
                    self.enqueue(target, item["gid"])
                else:
                    self.gaps.append("Unknown portfolio item type: " + str(item.get("resource_type")))
            self.listing(f"/portfolios/{id_}/portfolio_memberships", kind="portfolio_memberships", parent=id_)
            self.statuses(id_)
            if self.extended:
                self.children("/memberships", {"parent": id_}, "memberships", id_, "memberships")
        elif kind == "goals":
            self.one(kind, id_)
            relationships = self.listing("/goal_relationships", {"supported_goal": id_,
                "opt_fields": "gid,resource_subtype,supported_goal,supporting_resource,contribution_weight"}, "goal_relationships", id_)
            for r in relationships:
                support = r.get("supporting_resource") or {}
                target = {"goal": "goals", "project": "projects", "task": "tasks", "portfolio": "portfolios"}.get(support.get("resource_type"))
                if target:
                    self.enqueue(target, support["gid"])
            self.statuses(id_)
            if self.extended:
                self.children("/memberships", {"parent": id_}, "memberships", id_, "memberships")
        elif kind == "attachments":
            a = self.one(kind, id_)
            if a and self.download:
                from .downloads import download_attachment
                # Signed URLs expire. Refresh metadata on every download/resume.
                fresh = self.a.operation(f"/attachments/{id_}", refresh=True)
                if fresh:
                    a.update(fresh)
                try:
                    a["local_file"] = download_attachment(self.a.root, a, max_bytes=self.max_attachment_bytes)
                except (ValueError, OSError) as exc:
                    self.gaps.append(f"Attachment {id_}: {type(exc).__name__}; bytes unavailable")
            elif a:
                self.gaps.append(f"Attachment {id_}: metadata only; enable --download-attachments for bytes")
        else:
            self.one(kind, id_)
