import argparse
import getpass
import json
import os
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from .core import APIError, Archive, Client, Exporter, atomic, encode, gid
from .output import normalize_connector, verify, write_archive


@contextmanager
def lock(root):
    path = root / ".export.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Output is locked. If its exporter is no longer running, remove .export.lock and resume.") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description="Read-only Asana export with honest coverage and resumable operations")
    sub = p.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Collect directly through Asana REST")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--resume", action="store_true")
    export.add_argument("--workspace", action="append", type=gid, default=[])
    export.add_argument("--task", action="append", type=gid, default=[], help="Known orphan task seed; repeatable")
    export.add_argument("--project", action="append", type=gid, default=[], help="Additional project seed; repeatable")
    export.add_argument("--download-attachments", action="store_true")
    export.add_argument("--skip-time-entries", action="store_true")
    export.add_argument("--rpm", type=float, default=120)
    export.add_argument("--prompt-token", action="store_true", help="Read token without echoing or adding it to shell history")
    export.add_argument("--allow-partial", action="store_true", help="Return 0 for a written partial archive, instead of 2")
    norm = sub.add_parser("normalize-connector", help="Convert JSON arrays of connector capture records")
    norm.add_argument("--capture", type=Path, nargs="+", required=True)
    norm.add_argument("--out", type=Path, required=True)
    norm.add_argument("--allow-partial", action="store_true")
    check = sub.add_parser("verify", help="Verify archive checksums and detect missing/extra files")
    check.add_argument("directory", type=Path)
    pack = sub.add_parser("zip", help="Verify and package an archive; destination must be outside it")
    pack.add_argument("directory", type=Path)
    pack.add_argument("destination", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in ("verify", "zip"):
            issues = verify(args.directory)
            if issues:
                print("\n".join(issues), file=sys.stderr)
                return 1
            if args.command == "zip":
                root, destination = args.directory.resolve(), args.destination.resolve()
                if destination.is_relative_to(root):
                    raise ValueError("ZIP destination must be outside the archive directory")
                if destination.exists():
                    raise ValueError("ZIP destination already exists")
                with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as z:
                    for path in sorted(root.rglob("*")):
                        if path.is_file() and path.name != ".export.lock":
                            z.write(path, path.relative_to(root).as_posix())
                print(str(destination))
            else:
                print("All checksums verified")
            return 0
        root = args.out.resolve()
        if root.exists() and any(root.iterdir()) and not getattr(args, "resume", False):
            raise ValueError("Output directory is not empty; use --resume for an existing REST run")
        if getattr(args, "resume", False) and not (root / "run.json").exists():
            raise ValueError("No REST run.json found to resume")
        root.mkdir(parents=True, exist_ok=True)
        with lock(root):
            if args.command == "normalize-connector":
                records = []
                for index, path in enumerate(args.capture):
                    data = path.read_bytes()
                    loaded = json.loads(data)
                    if not isinstance(loaded, list):
                        raise ValueError("Each capture must be a JSON array")
                    records.extend(loaded)
                    atomic(root / "raw" / f"connector-{index:04}.json", data)
                export = normalize_connector(records)
            else:
                token = getpass.getpass("Asana access token: ") if args.prompt_token else os.environ.get("ASANA_ACCESS_TOKEN", "")
                client = Client(token, rpm=args.rpm)
                archive = Archive(root, client)
                exporter = Exporter(archive, attachments=args.download_attachments, time_entries=not args.skip_time_entries)
                if args.skip_time_entries:
                    exporter.gaps.append("Time tracking entries intentionally skipped by --skip-time-entries")
                export = exporter.run(args.workspace, args.task, args.project)
            counts = write_archive(root, export)
            print(json.dumps({"output": str(root), "counts": counts, "coverage": "partial"}))
        return 0 if args.allow_partial else 2
    except KeyboardInterrupt:
        print("Interrupted. Completed REST operations are saved; rerun with --resume.", file=sys.stderr)
        return 130
    except (APIError, ValueError, OSError, KeyError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
