from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .adapters import gdc_request_plan, pdc_request_plan
from .certificate import issue_certificate
from .engine import evaluate
from .evaluator import run_hidden
from .fixture import create_synthetic_snapshot, synthetic_bundle
from .replay import replay_certificate
from .schema import write_schemas
from .snapshot import read_snapshot


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--out", type=Path, default=Path("artifacts/verified-demo"))
    snap = sub.add_parser("snapshot")
    snap_sub = snap.add_subparsers(dest="snapshot_command", required=True)
    fetch = snap_sub.add_parser("fetch")
    fetch.add_argument("--source", required=True)
    fetch.add_argument("--project", default="TCGA-LUAD")
    fetch.add_argument("--study", default="PDC000153")
    fetch.add_argument("--out", type=Path, default=Path("artifacts/snapshot"))
    inspect = snap_sub.add_parser("inspect")
    inspect.add_argument("--snapshot", type=Path, required=True)
    answer = sub.add_parser("answer")
    answer.add_argument("--query", type=Path, required=True)
    answer.add_argument("--snapshot", type=Path, required=True)
    answer.add_argument("--certificate", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--certificate", type=Path, required=True)
    verify.add_argument("--snapshot", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("--snapshot", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    evaluate_cmd = sub.add_parser("evaluate-hidden")
    evaluate_cmd.add_argument("--snapshot", type=Path, required=True)
    evaluate_cmd.add_argument("--queries", type=Path, required=True)
    evaluate_cmd.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "demo":
        snapshot_dir = args.out / "snapshot"
        try:
            create_synthetic_snapshot(snapshot_dir)
        except (OSError, ValueError):
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            create_synthetic_snapshot(snapshot_dir)
        write_schemas(args.out / "schemas")
        query = {"operation": "assay_overlap", "entity_level": "case", "left": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "rna_expression"}, "right": {"source": "gdc", "project_id": "TCGA-LUAD", "assay_type": "protein_abundance"}, "denominator": "left"}
        query_path = args.out / "overlap.json"
        query_path.write_text(json.dumps(query, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        cert = issue_certificate(snapshot_dir, query, args.out / "certificate.json")
        replay = replay_certificate(snapshot_dir, args.out / "certificate.json")
        (args.out / "replay.json").write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (args.out / "normalized.json").write_text(json.dumps(synthetic_bundle(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"snapshot": str(snapshot_dir), "status": cert["result"]["status"], "result": cert["result"]["result"], "replay": replay}, indent=2, sort_keys=True))
        if not replay.get("valid"):
            raise SystemExit(1)
        return
    if args.command == "snapshot" and args.snapshot_command == "fetch":
        if args.source == "synthetic":
            print(json.dumps(create_synthetic_snapshot(args.out), indent=2, sort_keys=True))
        elif args.source == "gdc":
            print(json.dumps(gdc_request_plan(args.project).__dict__, indent=2, sort_keys=True))
        elif args.source == "pdc":
            print(json.dumps(pdc_request_plan(args.study).__dict__, indent=2, sort_keys=True))
        else:
            raise SystemExit(f"unsupported source: {args.source}")
        return
    if args.command == "snapshot" and args.snapshot_command == "inspect":
        manifest, connection = read_snapshot(args.snapshot)
        try:
            counts = {kind: connection.execute("SELECT COUNT(*) FROM entities WHERE kind = ?", (kind,)).fetchone()[0] for kind in ["case", "sample", "aliquot", "assay", "file"]}
        finally:
            connection.close()
        print(json.dumps({"manifest": manifest, "entity_counts": counts}, indent=2, sort_keys=True))
        return
    if args.command == "answer":
        query = json.loads(args.query.read_text(encoding="utf-8"))
        issue_certificate(args.snapshot, query, args.certificate)
        print(json.dumps(evaluate(args.snapshot, query), indent=2, sort_keys=True))
        return
    if args.command == "verify":
        report = replay_certificate(args.snapshot, args.certificate)
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report.get("valid"):
            raise SystemExit(1)
        return
    if args.command == "export":
        manifest, connection = read_snapshot(args.snapshot)
        try:
            rows = connection.execute("SELECT entity_id, kind, source, source_id, attributes_json FROM entities ORDER BY entity_id").fetchall()
            payload = [{"entity_id": row[0], "kind": row[1], "source": row[2], "source_id": row[3], "attributes": json.loads(row[4])} for row in rows]
            relationships = [dict(subject_id=row[0], predicate=row[1], object_id=row[2], source_record_id=row[3], source=row[4]) for row in connection.execute("SELECT subject_id, predicate, object_id, source_record_id, source FROM relationships ORDER BY subject_id, object_id").fetchall()]
            evidence = [dict(evidence_id=row[0], entity_id=row[1], field_name=row[2], raw_value=json.loads(row[3]), canonical_value=json.loads(row[4]), source_record_id=row[5]) for row in connection.execute("SELECT evidence_id, entity_id, field_name, raw_value_json, canonical_value_json, source_record_id FROM evidence ORDER BY evidence_id").fetchall()]
        finally:
            connection.close()
        args.out.write_text(json.dumps({"snapshot": manifest["snapshot_id"], "entities": payload, "relationships": relationships, "evidence": evidence}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if args.command == "evaluate-hidden":
        print(json.dumps(run_hidden(args.snapshot, args.queries, args.out), indent=2, sort_keys=True))

