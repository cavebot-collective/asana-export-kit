"""Deterministic local benchmarks. These never measure Asana server performance."""
import json
import math
import platform
import sys
import time
import tracemalloc
from pathlib import Path
from .core import atomic, encode, hash_file, now
from .output import write_archive, verify


def synthetic(task_count, comments_per_task=5):
    tasks, stories, edges = {}, {}, []
    projects = {str(1000 + p): {"gid": str(1000 + p), "name": f"Synthetic project {p}"}
        for p in range(max(1, math.ceil(task_count / 100)))}
    project_ids = list(projects)
    for i in range(task_count):
        gid = str(1000000000000000 + i)
        p = project_ids[i // 100]
        tasks[gid] = {"gid": gid, "name": f"Synthetic task {i}", "completed": i % 3 == 0,
            "notes": "Synthetic description. " * 20, "projects": [{"gid": p}],
            "memberships": [{"project": {"gid": p}}], "custom_fields": [],
            "parent": {"gid": str(1000000000000000 + i - 1)} if i % 10 == 1 else None}
        edges.append({"parent": p, "relation": "tasks", "child": gid})
        for j in range(comments_per_task):
            sid = str(2000000000000000 + i * comments_per_task + j)
            stories[sid] = {"gid": sid, "resource_subtype": "comment_added", "text": "Synthetic comment " + str(j)}
            edges.append({"parent": gid, "relation": "stories", "child": sid})
    return {"schema_version": 1, "source": "synthetic-benchmark", "principal": {"gid": "1"},
        "objects": {"projects": projects, "tasks": tasks, "stories": stories}, "edges": edges,
        "coverage": {"status": "partial", "limitations": ["Synthetic data; no API calls or network measurements"], "errors": []}}


def run_benchmark(out, tasks=1000, comments=5, attachment_bytes=0):
    if tasks < 0 or comments < 0 or attachment_bytes < 0:
        raise ValueError("Benchmark sizes cannot be negative")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Benchmark destination must be empty")
    out.mkdir(parents=True, exist_ok=True)
    archive = out / "archive"
    phases = {}
    tracemalloc.start()
    try:
        start = time.perf_counter()
        export = synthetic(tasks, comments)
        phases["generate_seconds"] = time.perf_counter() - start
        start = time.perf_counter()
        write_archive(archive, export)
        phases["render_and_manifest_seconds"] = time.perf_counter() - start
        start = time.perf_counter()
        issues = verify(archive)
        phases["verify_seconds"] = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    large = None
    if attachment_bytes:
        # A local sparse zero-filled fixture tests sequential reads/hash memory,
        # not network downloads or realistic physical disk throughput.
        blob = out / "large-file-fixture.bin"
        with open(blob, "wb") as f:
            f.truncate(attachment_bytes)
        tracemalloc.start()
        try:
            start = time.perf_counter()
            size, sha = hash_file(blob)
            seconds = time.perf_counter() - start
            _, hash_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        large = {"bytes_read": size, "sha256": sha, "seconds": seconds,
            "peak_python_bytes": hash_peak, "fixture": "sparse zero-filled file; not network throughput"}
    report = {"schema_version": 1, "kind": "local-synthetic", "at": now(),
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "shape": {"tasks": tasks, "comments_per_task": comments, "projects": len(export["objects"]["projects"])},
        "timings": phases, "peak_python_bytes": peak,
        "archive_bytes": sum(p.stat().st_size for p in archive.rglob("*") if p.is_file()),
        "integrity_errors": issues, "large_file": large,
        "caveats": ["Python allocator peak, not process RSS", "Tracemalloc overhead included", "Single run; filesystem cache uncontrolled", "No API latency, throttling, retries or download bandwidth measured"]}
    atomic(out / "benchmark.json", encode(report))
    return report


def profile_archive(root):
    """Metadata sizing without publishing task text, identities or resource IDs."""
    root = Path(root)
    problems = verify(root)
    if problems:
        raise ValueError("Archive failed integrity verification")
    e = json.loads((root / "export.json").read_text(encoding="utf-8"))
    records = e["objects"].get("tasks", {}).values()
    note_lengths = sorted(len((t.get("notes") or "").encode()) for t in records)
    def percentile(p):
        return note_lengths[max(0, math.ceil(len(note_lengths) * p) - 1)] if note_lengths else 0
    return {"kind": "archive-profile", "counts": {k: len(v) for k, v in e["objects"].items()},
        "task_note_bytes": {"median": percentile(.5), "p95": percentile(.95), "max": percentile(1)},
        "archive_bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
        "source_coverage": e["coverage"]["status"],
        "caveat": "Describes captured records only; omissions make workload estimates lower bounds"}
