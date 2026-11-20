#!/usr/bin/env python3
"""Separate external grader for frozen blind exam outputs.

Verifies freeze integrity of ``artifacts/blind-exam-run`` before and after grading,
does not rerun or alter frozen outputs, keeps COR-001 marked as a development
regression rather than a blind holdout, and evaluates strictly against
``answer_key.json`` and ``scoring.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEV_REGRESSION_CASES = {"COR-001"}


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen_dir(frozen_dir: Path) -> str:
    sums_path = frozen_dir / "SHA256SUMS.txt"
    manifest_path = frozen_dir / "FROZEN_MANIFEST.json"
    if not sums_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"Missing freeze manifest files in {frozen_dir}")

    recorded: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        recorded[rel] = digest
        target = frozen_dir / rel
        if not target.is_file() or sha256_file(target) != digest:
            raise ValueError(f"Frozen integrity mismatch on {rel}")

    actual_files = {
        p.relative_to(frozen_dir).as_posix()
        for p in frozen_dir.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.txt"
    }
    if actual_files != set(recorded.keys()):
        raise ValueError(
            f"Frozen file set mismatch: extra={sorted(actual_files - set(recorded))} missing={sorted(set(recorded) - actual_files)}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel, digest in manifest["files_sha256"].items():
        if recorded.get(rel) != digest:
            raise ValueError(f"FROZEN_MANIFEST mismatch against SHA256SUMS.txt for {rel}")
    return str(manifest["manifest_sha256"])


def _grade_executed_case(case_rec: dict[str, Any], ak_entry: dict[str, Any], *, suite_role: str = "blind_holdout") -> dict[str, Any]:
    cid = case_rec["case_id"]
    expected = ak_entry["expected"]
    actual = case_rec.get("final_output") or {}
    checks: dict[str, bool] = {}
    failures: list[str] = []

    checks["status_match"] = actual.get("status") == expected["status"]
    if not checks["status_match"]:
        failures.append(f"status={actual.get('status')!r} != expected={expected['status']!r}")

    checks["deterministic_3x"] = case_rec.get("deterministic") is True
    if not checks["deterministic_3x"]:
        failures.append("3x run determinism failed")

    checks["replay_verified"] = (
        actual.get("evaluate_replay_valid") is True and actual.get("verify_valid") is True
    )
    if not checks["replay_verified"]:
        failures.append("certificate replay verification did not pass")

    actual_res = actual.get("result") or {}
    exp_res = expected["result"]

    if expected["status"] == "SUPPORTED":
        if exp_res.get("replay_verified") is True:
            checks["replay_flag_match"] = actual_res.get("replay_verified") is True
            if not checks["replay_flag_match"]:
                failures.append(f"expected replay_verified=True, got {actual_res.get('replay_verified')!r}")
        if exp_res.get("claim_holds") is True:
            checks["claim_holds_match"] = actual_res.get("claim_holds") is True
            if not checks["claim_holds_match"]:
                failures.append(f"expected claim_holds=True, got {actual_res.get('claim_holds')!r}")
        else:
            checks["entity_level_match"] = actual_res.get("entity_level") == exp_res.get("entity_level")
            checks["denominator_basis_match"] = actual_res.get("denominator_basis") == exp_res.get("denominator_basis")
            checks["numerator_count_match"] = actual_res.get("numerator_count") == exp_res.get("numerator_count")
            checks["denominator_count_match"] = actual_res.get("denominator_count") == exp_res.get("denominator_count")
            for key in ("entity_level_match", "denominator_basis_match", "numerator_count_match", "denominator_count_match"):
                if not checks[key]:
                    failures.append(f"{key} failed: actual={actual_res!r} expected={exp_res!r}")

        expected_support = sorted(expected.get("supporting_evidence_ids", []))
        actual_evidence = sorted(actual.get("evidence_refs", []))
        actual_support = sorted(actual_res.get("supporting_evidence_ids", []))
        checks["evidence_grounding_match"] = (
            actual_evidence == expected_support and actual_support == expected_support
        )
        if not checks["evidence_grounding_match"]:
            failures.append(
                f"evidence mismatch: actual_refs={actual_evidence} actual_supporting={actual_support} expected={expected_support}"
            )
    elif expected["status"] == "CONTRADICTED":
        checks["claim_holds_match"] = actual_res.get("claim_holds") is False
        if not checks["claim_holds_match"]:
            failures.append(f"expected claim_holds=False, got {actual_res.get('claim_holds')!r}")
        has_fabricated_counts = any(
            actual_res.get(k) is not None for k in ("numerator_count", "denominator_count", "numerator", "denominator")
        )
        checks["no_fabricated_counts"] = not has_fabricated_counts
        if not checks["no_fabricated_counts"]:
            failures.append(f"fabricated numeric counts on CONTRADICTED: {actual_res!r}")
        expected_contra = sorted(expected.get("contradicting_evidence_ids", []))
        actual_contra = sorted(actual_res.get("contradicting_evidence_ids", actual.get("evidence_refs", [])))
        checks["contradicting_evidence_match"] = actual_contra == expected_contra
        if not checks["contradicting_evidence_match"]:
            failures.append(f"contradicting evidence mismatch: actual={actual_contra} expected={expected_contra}")
    else:
        if exp_res.get("replay_verified") is False:
            checks["replay_rejected_match"] = actual_res.get("replay_verified") is False
            if not checks["replay_rejected_match"]:
                failures.append(f"expected replay_verified=False, got {actual_res.get('replay_verified')!r}")
        has_fabricated_counts = any(
            actual_res.get(k) is not None
            for k in ("numerator_count", "denominator_count", "numerator", "denominator", "claim_holds")
        )
        checks["no_fabricated_counts"] = not has_fabricated_counts
        if not checks["no_fabricated_counts"]:
            failures.append(f"fabricated result fields on abstention: {actual_res!r}")
        checks["no_fabricated_evidence"] = (
            actual.get("witness") == [] and actual.get("evidence_refs") == []
        )
        if not checks["no_fabricated_evidence"]:
            failures.append("non-empty witness/evidence_refs on abstention")

    passed = all(checks.values())
    if suite_role == "regression_suite":
        split = "REGRESSION_SUITE"
    else:
        split = "DEVELOPMENT_REGRESSION" if cid in DEV_REGRESSION_CASES else "BLIND_HOLDOUT"
    return {
        "case_id": cid,
        "evaluation_split": split,
        "category": ak_entry["category"],
        "severity": ak_entry["severity"],
        "governing_rules": ak_entry["governing_rules"],
        "paired_cases": ak_entry["paired_cases"],
        "disposition": "EXECUTED",
        "passed": passed,
        "checks": checks,
        "failures": failures,
        "expected_status": expected["status"],
        "actual_status": actual.get("status"),
        "expected_result": exp_res,
        "actual_result": {
            "entity_level": actual_res.get("entity_level"),
            "denominator_basis": actual_res.get("denominator_basis"),
            "numerator_count": actual_res.get("numerator_count"),
            "denominator_count": actual_res.get("denominator_count"),
            "claim_holds": actual_res.get("claim_holds"),
            "witness": actual.get("witness"),
            "evidence_refs": actual.get("evidence_refs"),
            "replay_verified": actual_res.get("replay_verified") if "replay_verified" in actual_res else checks["replay_verified"],
        },
        "deterministic": case_rec.get("deterministic"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade frozen exam or regression run outputs without altering them.")
    parser.add_argument("--frozen-run", "--run-dir", dest="frozen_run", type=Path, default=Path("artifacts/blind-exam-run"))
    parser.add_argument("--answer-key", type=Path, default=Path("cancer-evidence-exam/cancer-evidence-exam/answer_key.json"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/blind-exam-grading/grading_report.json"))
    parser.add_argument("--suite-role", choices=("blind_holdout", "regression_suite"), default="blind_holdout")
    args = parser.parse_args(argv)

    frozen_dir = args.frozen_run.resolve()
    pre_manifest_hash = verify_frozen_dir(frozen_dir)

    run_report = json.loads((frozen_dir / "blind_run_report.json").read_text(encoding="utf-8"))
    answer_key = json.loads(args.answer_key.resolve().read_text(encoding="utf-8"))
    ak_cases = answer_key["cases"]

    run_by_id = {c["case_id"]: c for c in run_report["cases"]}
    if set(run_by_id.keys()) != set(ak_cases.keys()):
        raise ValueError("Case ID set mismatch between blind_run_report.json and answer_key.json")

    blind_holdout_results: list[dict[str, Any]] = []
    dev_regression_results: list[dict[str, Any]] = []
    not_run_records: list[dict[str, Any]] = []
    exclusion_records: list[dict[str, Any]] = []

    for cid in sorted(ak_cases.keys()):
        ak = ak_cases[cid]
        rec = run_by_id[cid]
        disp = rec["disposition"]

        if disp == "PRE_DESIGNATED_EXCLUSION":
            if not ak["blocked"]:
                raise ValueError(f"{cid} marked PRE_DESIGNATED_EXCLUSION in run but blocked=False in answer_key")
            exclusion_records.append(
                {
                    "case_id": cid,
                    "category": ak["category"],
                    "severity": ak["severity"],
                    "disposition": disp,
                    "blocked_reason": ak["blocked_reason"],
                }
            )
            continue

        if ak["blocked"]:
            raise ValueError(f"{cid} is blocked=True in answer_key but had disposition={disp!r}")

        if disp == "NOT_RUN":
            not_run_records.append(
                {
                    "case_id": cid,
                    "category": ak["category"],
                    "severity": ak["severity"],
                    "disposition": disp,
                    "not_run_reasons": rec.get("not_run_reasons", []),
                    "counted_as_tamper_pass": rec.get("counted_as_tamper_pass", False),
                }
            )
            continue

        if disp != "EXECUTED":
            raise ValueError(f"Unexpected disposition {disp!r} for {cid}")

        graded = _grade_executed_case(rec, ak, suite_role=args.suite_role)
        if graded["evaluation_split"] in {"DEVELOPMENT_REGRESSION", "REGRESSION_SUITE"}:
            dev_regression_results.append(graded)
        else:
            blind_holdout_results.append(graded)

    categories = ["correctness", "abstention", "provenance", "tampering"]
    scoring_thresholds = {
        "correctness": {"min_passed": 11, "total_scored": 12, "min_critical_passed": 3},
        "abstention": {"min_passed": 13, "total_scored": 14, "min_critical_passed": 7},
        "provenance": {"min_passed": 8, "total_scored": 8, "min_critical_passed": 5},
        "tampering": {"min_passed": 8, "total_scored": 8, "min_critical_passed": 8},
    }

    category_breakdown: dict[str, Any] = {}
    for cat in categories:
        cat_scored_all = [ak for ak in ak_cases.values() if not ak["blocked"] and ak["category"] == cat]
        cat_holdout = [r for r in blind_holdout_results if r["category"] == cat]
        cat_dev = [r for r in dev_regression_results if r["category"] == cat]
        cat_not_run = [r for r in not_run_records if r["category"] == cat]
        cat_excluded = [r for r in exclusion_records if r["category"] == cat]

        holdout_passed = sum(1 for r in cat_holdout if r["passed"])
        dev_passed = sum(1 for r in cat_dev if r["passed"])
        thresh = scoring_thresholds[cat]
        total_executed_passed = holdout_passed + dev_passed
        category_breakdown[cat] = {
            "total_scored_in_exam": len(cat_scored_all),
            "excluded_blocked": len(cat_excluded),
            "blind_holdout_executed": len(cat_holdout),
            "blind_holdout_passed": holdout_passed,
            "blind_holdout_failed": len(cat_holdout) - holdout_passed,
            "development_regression_executed": len(cat_dev),
            "development_regression_passed": dev_passed,
            "not_run": len(cat_not_run),
            "blind_holdout_executed_pass_rate": (
                f"{holdout_passed}/{len(cat_holdout)}" if cat_holdout else "N/A (0 executed)"
            ),
            "blind_holdout_vs_full_exam_scored": f"{holdout_passed}/{len(cat_scored_all)}",
            "all_executed_vs_full_exam_scored": f"{total_executed_passed}/{len(cat_scored_all)}",
            "scoring_md_required_min": f"{thresh['min_passed']}/{thresh['total_scored']}",
            "scoring_md_category_gate_met": (
                total_executed_passed >= thresh["min_passed"]
                if args.suite_role == "regression_suite"
                else holdout_passed >= thresh["min_passed"]
            ),
        }

    critical_all = [ak for ak in ak_cases.values() if not ak["blocked"] and ak["severity"] == "CRITICAL"]
    critical_holdout = [r for r in blind_holdout_results if r["severity"] == "CRITICAL"]
    critical_holdout_passed = sum(1 for r in critical_holdout if r["passed"])
    critical_not_run = [r for r in not_run_records if r["severity"] == "CRITICAL"]

    holdout_passed_total = sum(1 for r in blind_holdout_results if r["passed"])
    dev_passed_total = sum(1 for r in dev_regression_results if r["passed"])
    all_executed = blind_holdout_results + dev_regression_results
    total_passed = holdout_passed_total + dev_passed_total
    critical_passed_all = sum(1 for r in all_executed if r["severity"] == "CRITICAL" and r["passed"])

    hard_gates = {
        "zero_critical_failures_on_executed": sum(1 for r in all_executed if r["severity"] == "CRITICAL" and not r["passed"]) == 0,
        "all_23_critical_cases_executed_and_passed": (
            critical_passed_all == len(critical_all)
            if args.suite_role == "regression_suite"
            else critical_holdout_passed == len(critical_all)
        ),
        "category_correctness_gate_11_of_12": category_breakdown["correctness"]["scoring_md_category_gate_met"],
        "category_abstention_gate_13_of_14": category_breakdown["abstention"]["scoring_md_category_gate_met"],
        "category_provenance_gate_8_of_8": category_breakdown["provenance"]["scoring_md_category_gate_met"],
        "category_tampering_gate_8_of_8": category_breakdown["tampering"]["scoring_md_category_gate_met"],
        "overall_scored_gate_40_of_42": (
            total_passed >= 40 if args.suite_role == "regression_suite" else holdout_passed_total >= 40
        ),
        "determinism_gate_on_executed": run_report["summary"]["determinism_failure_count"] == 0,
    }
    exam_passed = all(hard_gates.values())
    if args.suite_role == "regression_suite":
        verdict = "PASS_REGRESSION_SUITE" if exam_passed else "FAIL_REGRESSION_SUITE"
    else:
        verdict = "PASS" if exam_passed else "FAIL_INCOMPLETE_COVERAGE"

    report = {
        "grading_version": "1.1.0",
        "suite_role": args.suite_role,
        "graded_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "frozen_run_dir": str(frozen_dir),
        "frozen_manifest_sha256_before": pre_manifest_hash,
        "frozen_manifest_sha256_after": None,
        "frozen_outputs_untouched": None,
        "answer_key_path": str(args.answer_key.resolve()),
        "exam_passed": exam_passed,
        "overall_verdict": verdict,
        "summary": {
            "total_cases_in_exam": 48,
            "scored_cases_in_exam": 42,
            "pre_designated_exclusions_count": len(exclusion_records),
            "not_run_count": len(not_run_records),
            "executed_count": len(all_executed),
            "executed_passed": total_passed,
            "executed_blind_holdout_count": len(blind_holdout_results),
            "executed_blind_holdout_passed": holdout_passed_total,
            "executed_blind_holdout_failed": len(blind_holdout_results) - holdout_passed_total,
            "executed_development_regression_count": len(dev_regression_results),
            "executed_development_regression_passed": dev_passed_total,
            "critical_cases_total": len(critical_all),
            "critical_blind_holdout_executed": len(critical_holdout),
            "critical_blind_holdout_passed": critical_holdout_passed,
            "critical_executed_passed": critical_passed_all,
            "critical_not_run": len(critical_not_run),
            "determinism_failures_on_executed": run_report["summary"]["determinism_failure_count"],
        },
        "hard_gates": hard_gates,
        "category_breakdown": category_breakdown,
        "executed_blind_holdout_results": blind_holdout_results,
        "executed_development_regression_results": dev_regression_results,
        "not_run_cases": not_run_records,
        "pre_designated_exclusions": exclusion_records,
    }

    out_path = args.out if args.out.is_absolute() else (Path.cwd() / args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    post_manifest_hash = verify_frozen_dir(frozen_dir)
    report["frozen_manifest_sha256_after"] = post_manifest_hash
    report["frozen_outputs_untouched"] = pre_manifest_hash == post_manifest_hash

    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "grading_report_path": str(out_path),
                "suite_role": args.suite_role,
                "exam_passed": exam_passed,
                "overall_verdict": report["overall_verdict"],
                "frozen_outputs_untouched": report["frozen_outputs_untouched"],
                "summary": report["summary"],
                "category_breakdown": category_breakdown,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
