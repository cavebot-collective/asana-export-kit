"""Verified archive handoff to a separately configured rclone remote."""
import re
import subprocess
from pathlib import Path
from .core import atomic, encode, hash_file, now
from .output import verify


def upload_archive(root, remote, receipt, executable="rclone", runner=subprocess.run):
    root, receipt = Path(root).resolve(), Path(receipt).resolve()
    if receipt.is_relative_to(root):
        raise ValueError("Upload receipt must be outside the sealed archive")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[^\r\n\x00]*", remote):
        raise ValueError("Use a configured rclone remote, for example drive:Asana")
    if (root / ".export.lock").exists() or verify(root):
        raise ValueError("Archive integrity verification failed before upload")
    before = hash_file(root / "manifest.json")[1]
    destination = remote.rstrip("/") + "/" + before
    report = {"schema_version": 1, "manifest_sha256": before, "destination": destination,
        "started_at": now(), "state": "copying", "verification": "rclone check --download"}
    atomic(receipt, encode(report))
    try:
        # argv only: paths never become shell code. Content-addressed destination
        # and --immutable prevent accidental replacement of a differing object.
        for command in ([executable, "copy", str(root), destination, "--immutable", "--exclude", ".export.lock"],
                        [executable, "check", str(root), destination, "--download", "--exclude", ".export.lock"]):
            result = runner(command, check=False)
            if result.returncode:
                raise ValueError("rclone failed; receipt does not certify a remote backup")
        if verify(root) or hash_file(root / "manifest.json")[1] != before:
            raise ValueError("Local archive changed during upload")
    except (OSError, ValueError):
        report.update(state="failed", finished_at=now())
        atomic(receipt, encode(report))
        raise
    report.update(state="verified", finished_at=now())
    atomic(receipt, encode(report))
    return report
