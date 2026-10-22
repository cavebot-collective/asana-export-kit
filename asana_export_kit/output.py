"""Lossless JSON, readable derivatives, connector normalization and integrity checks."""
import csv
import io
import json
import re
from collections import Counter
from pathlib import Path
from .core import atomic, digest, encode, now, LIMITATIONS


def cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = str(value)
    # Mitigate spreadsheet formulas and numeric coercion of long GIDs.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) or re.fullmatch(r"[0-9]{15,}", value):
        return "'" + value
    return value


def manifest(root):
    root = Path(root)
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", ".export.lock") and not p.name.endswith((".tmp", ".part")):
            if p.is_symlink():
                raise ValueError("Archive must not contain symlinks")
            data = p.read_bytes()
            files.append({"path": p.relative_to(root).as_posix(), "bytes": len(data), "sha256": digest(data)})
    atomic(root / "manifest.json", encode({"schema_version": 1, "algorithm": "sha256", "files": files}))


def verify(root):
    root = Path(root).resolve()
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    issues, listed = [], set()
    for item in m["files"]:
        path = item["path"]
        p = root / path
        if path in listed or p.is_symlink() or not p.resolve().is_relative_to(root):
            issues.append("Unsafe or duplicate path: " + path)
            continue
        listed.add(path)
        if not p.is_file():
            issues.append("Missing: " + path)
        elif p.stat().st_size != item["bytes"] or digest(p.read_bytes()) != item["sha256"]:
            issues.append("Changed: " + path)
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name not in ("manifest.json", ".export.lock")}
    issues += ["Unlisted: " + p for p in sorted(actual - listed)]
    return issues


def write_archive(root, export):
    root = Path(root)
    atomic(root / "export.json", encode(export))
    counts = {k: len(v) for k, v in export["objects"].items()}
    atomic(root / "coverage.json", encode({**export["coverage"], "counts": counts}))
    for kind, objects in sorted(export["objects"].items()):
        rows = list(objects.values())
        if not rows:
            continue
        columns = ["gid"] + sorted(set().union(*(r.keys() for r in rows)) - {"gid"})
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell(row.get(c)) for c in columns])
        atomic(root / "tables" / (kind + ".csv"), stream.getvalue().encode("utf-8-sig"))
    lines = ["# Asana archive", "", "Source: " + export["source"], "", "Coverage: **partial** (see coverage.json).", "",
        "## Counts", ""] + [f"- {k}: {v}" for k, v in sorted(counts.items())]
    lines += ["", "## Limits", ""] + ["- " + x for x in export["coverage"].get("limitations", [])]
    lines += ["", "## How to read", "", "export.json preserves returned values and string IDs. tables/ contains spreadsheet-safe derivatives; apostrophe prefixes prevent formulas and long-ID rounding. raw/ preserves original responses and request provenance. Checksums detect accidental changes; they do not authenticate the source.", "",
        "Never publish this archive or upload it to a public repository. It may contain personal information and signed attachment URLs.", ""]
    atomic(root / "README.md", "\n".join(lines).encode())
    text = ["# Tasks", ""]
    for task in sorted(export["objects"].get("tasks", {}).values(), key=lambda x: x["gid"]):
        text += ["## " + task.get("name", "").replace("\n", " "), "", "GID: " + task["gid"],
            "", "Completed: " + str(task.get("completed", "unknown")), "", task.get("notes") or "", ""]
        for c in task.get("comments", []):
            text += ["### Comment " + str(c.get("gid", "")), "", c.get("text") or "", ""]
    atomic(root / "tasks.md", "\n".join(text).encode())
    manifest(root)
    return counts


def unwrap(result):
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                pass
    return {}


def normalize_connector(records):
    objects, errors, edges, reconciliations, unresolved = {}, [], [], [], []
    limitations = list(LIMITATIONS) + [
        "Connector-selected workspace only; other workspaces cannot be selected through this tool schema.",
        "Comments capped at 50 per task, and may be filtered from a capped activity page. Empty comments do not prove absence. Full activity history is unavailable.",
        "Project sections capped at 50; subtask paging is not exposed. Compare num_subtasks with returned subtasks.",
        "No complete status history, goal/brief content, time entries, or attachment bytes through this connector capture.",
        "Project/task count parity proves only discovered project membership coverage, not all account data.",
    ]
    principal = None
    def put(kind, obj):
        if obj and obj.get("gid"):
            objects.setdefault(kind, {})[obj["gid"]] = obj
    for rec in records:
        result = rec.get("result", {})
        body = unwrap(result)
        key = rec.get("key", "")
        if rec.get("failure") or result.get("isError") or body.get("error"):
            errors.append({"key": key, "tool": rec.get("tool"), "error": rec.get("failure") or body})
            continue
        data = body.get("data")
        if key in ("initial0", "identity") and isinstance(data, dict):
            principal = data
            put("users", data)
        elif key in ("discover1", "teams", "discover2", "users", "agents"):
            kind = {"discover1": "teams", "discover2": "users"}.get(key, key)
            objects.setdefault(kind, {})
            for obj in data or []:
                put(kind, obj)
        elif key.startswith("projects/") and isinstance(data, dict):
            put("projects", data)
            put("workspaces", data.get("workspace"))
            for s in data.get("sections") or []:
                put("sections", s)
                edges.append({"parent": data["gid"], "relation": "sections", "child": s["gid"]})
            if data.get("sections") is None:
                errors.append({"key": key, "error": "Sections unavailable; null is not an empty list"})
        elif key.startswith("users/") and isinstance(data, dict):
            put("users", data)
        elif key.startswith("tasks/") and isinstance(data, dict):
            put("tasks", data)
            for c in data.get("comments") or []:
                put("comments", c)
                edges.append({"parent": data["gid"], "relation": "comments", "child": c["gid"]})
            if data.get("num_subtasks", 0) != len(data.get("subtasks") or []):
                unresolved.append({"task": data["gid"], "num_subtasks": data.get("num_subtasks"), "returned": len(data.get("subtasks") or [])})
        elif key.startswith("attachments/"):
            objects.setdefault("attachments", {})
            for a in data or []:
                put("attachments", a)
                edges.append({"parent": key.split("/")[1], "relation": "attachments", "child": a["gid"]})
        if key.startswith("project-tasks/"):
            for t in data or []:
                edges.append({"parent": key.split("/")[1], "relation": "tasks", "child": t["gid"]})
        if body.get("next_page"):
            # Check that this cursor actually has a corresponding captured follow-up.
            offset = body["next_page"].get("offset")
            if not any(x.get("key") == key and x.get("args", {}).get("offset") == offset for x in records):
                errors.append({"key": key, "error": "Unfollowed pagination cursor"})
    for p in objects.get("projects", {}).values():
        ids = {e["child"] for e in edges if e["parent"] == p["gid"] and e["relation"] == "tasks"}
        expected = (p.get("task_counts") or {}).get("num_tasks")
        reconciliations.append({"project_gid": p["gid"], "name": p["name"], "reported": expected,
            "listed_unique": len(ids), "detail_records": len(ids & set(objects.get("tasks", {}))),
            "matches": expected is not None and expected == len(ids) and ids <= set(objects.get("tasks", {}))})
    return {"schema_version": 1, "source": "asana-connector", "finished_at": now(), "principal": principal,
        "objects": objects, "edges": edges, "coverage": {"status": "partial", "limitations": limitations,
        "errors": errors, "project_reconciliation": reconciliations, "subtask_count_mismatches": unresolved,
        "captured_calls": len(records)}}
