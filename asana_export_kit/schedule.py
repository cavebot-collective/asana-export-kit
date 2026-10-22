"""Serial snapshot cycles; no overlapping runs or accumulated missed ticks."""
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from .core import atomic, encode, now
from .output import verify
from .storage import upload_archive


def watch(root, interval, export_args, runs=0, remote=None, rclone="rclone", env=None,
          runner=subprocess.run, clock=time.monotonic, sleep=time.sleep):
    if not math.isfinite(interval) or interval <= 0 or runs < 0:
        raise ValueError("Interval must be positive and finite; runs cannot be negative")
    forbidden = {"--out", "--resume", "--oauth", "--oauth-no-browser", "--prompt-token", "--remote", "--receipt"}
    if any(arg.split("=", 1)[0] in forbidden for arg in export_args):
        raise ValueError("Watch owns output/auth/remote settings; supply a local token once, not interactive export flags")
    # Parse before starting so unsupported options cannot become a failed loop.
    from .cli import parser
    parser().parse_args(["export", "--out", "validation-only", *export_args])
    env = dict(os.environ if env is None else env)
    if not env.get("ASANA_ACCESS_TOKEN"):
        raise ValueError("Watch requires ASANA_ACCESS_TOKEN or its --prompt-token option")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    while runs == 0 or count < runs:
        started = clock()
        run_id = now().replace(":", "-") + "-" + uuid.uuid4().hex[:8]
        destination = root / run_id
        status = root / "receipts" / (run_id + ".json")
        report = {"run_id": run_id, "started_at": now(), "state": "collecting", "requested_interval_seconds": interval}
        atomic(status, encode(report))
        try:
            result = runner([sys.executable, "-m", "asana_export_kit", "export", "--out", str(destination), *export_args],
                env=env, check=False)
            if result.returncode not in (0, 2):
                raise ValueError("Collection failed; schedule stopped; inspect the retained run")
            if verify(destination):
                raise ValueError("Scheduled archive failed integrity verification")
            report["export_exit_code"] = result.returncode
            if remote:
                upload_archive(destination, remote, root / "receipts" / (run_id + ".upload.json"), rclone, runner)
            elapsed = clock() - started
            report.update(state="complete", finished_at=now(), elapsed_seconds=elapsed,
                effective_interval_seconds=max(interval, elapsed), wait_seconds=max(0, interval - elapsed))
        except (OSError, ValueError):
            report.update(state="failed", finished_at=now())
            atomic(status, encode(report))
            raise
        atomic(status, encode(report))
        print(f"Snapshot {run_id}: complete in {elapsed:.1f}s; interval floor {max(interval, elapsed):.1f}s", flush=True)
        count += 1
        if runs == 0 or count < runs:
            sleep(max(0, interval - elapsed))
    return count
