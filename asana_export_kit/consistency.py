"""Compare observations without misrepresenting equality as a source transaction."""
import json
from pathlib import Path
from .core import digest, encode, hash_file
from .output import verify


def semantic_objects(export):
    objects = {}
    for kind, rows in export["objects"].items():
        for gid, value in rows.items():
            value = dict(value)
            if kind == "attachments":
                # Signed URL rotation and local placement are not content changes.
                value.pop("download_url", None)
                value.pop("local_file", None)
            objects[(kind, gid)] = digest(encode(value))
    return objects


def compare_archives(left, right):
    for root in (left, right):
        if verify(root):
            raise ValueError("Cannot compare an archive with failed integrity checks")
    a, b = [json.loads((Path(p) / "export.json").read_text(encoding="utf-8")) for p in (left, right)]
    if not (a.get("principal") or {}).get("gid") or a["principal"]["gid"] != (b.get("principal") or {}).get("gid"):
        raise ValueError("Archives must identify the same authenticated principal")
    if a["source"] != b["source"]:
        raise ValueError("Compare archives from the same collector")
    x, y = semantic_objects(a), semantic_objects(b)
    def refs(keys):
        return [{"kind": k, "gid": v} for k, v in sorted(keys)]
    def relationships(export):
        return {json.dumps(e, sort_keys=True) for e in export.get("edges", [])}
    ex, ey = relationships(a), relationships(b)
    def binaries(root, export):
        result = {}
        for id_, obj in export["objects"].get("attachments", {}).items():
            relative = obj.get("local_file")
            if relative:
                path = (Path(root) / relative).resolve()
                if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
                    raise ValueError("Invalid attachment file reference")
                result[id_] = hash_file(path)
        return result
    bx, by = binaries(left, a), binaries(right, b)
    differences = {"newly_observed": refs(y.keys() - x.keys()), "no_longer_observed": refs(x.keys() - y.keys()),
        "changed": refs(k for k in x.keys() & y.keys() if x[k] != y[k]),
        "relationships_added": len(ey - ex), "relationships_removed": len(ex - ey), "attachment_bytes_equal": bx == by}
    equal = x == y and ex == ey and bx == by
    return {"schema_version": 1, "observed_equal": equal, "differences": differences,
        "guarantee": "observational_comparison_only", "transactional_snapshot_proven": False,
        "limitations": ["Equal observations do not prove an intervening stable interval or lossless event coverage",
            "A cached resume is not an independent scan", "No-longer-observed can mean deletion, lost access, scope changes or collection failure",
            "Unobserved resources/fields and historical intermediate versions remain outside this comparison"],
        "coverage": {"left": a["coverage"], "right": b["coverage"]}}
