from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path

from cancer_evidence.multimodal import (
    AlignmentPolicy,
    CohortRequest,
    MultimodalSnapshot,
    evaluate_cohort,
    export_cohort_result,
)
from cancer_evidence.multimodal.demo_ui import compute_rna_protein_summary, write_multimodal_demo_ui

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "snapshots" / "multimodal" / "cptac3_lung_v1"
DEMO_DIR = REPO_ROOT / "artifacts" / "multimodal-demo" / "cptac3_lung_v1"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline EGIS Multimodal Lung Cancer demo.")
    parser.add_argument("--serve", action="store_true", help="Serve the read-only HTML demo on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8766, help="Local port when --serve is set")
    args = parser.parse_args()

    with MultimodalSnapshot(SNAPSHOT_DIR) as snap:
        req_4mod = CohortRequest(
            cohort_id="cptac3-luad-4mod-primary-tumor",
            required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=True,
            ),
            disease_types=("Adenomas and Adenocarcinomas",),
        )
        res_4mod = evaluate_cohort(snap, req_4mod)

        req_strict_ts = CohortRequest(
            cohort_id="cptac3-luad-4mod-strict-timestamp-only",
            required_modalities=("clinical", "rna", "somatic_mutations", "proteomics"),
            alignment_policy=AlignmentPolicy(
                required_sample_type="Primary Tumor",
                max_day_delta=30,
                require_same_episode=True,
                allow_missing_timestamps=False,
                allow_shared_specimen_fallback=False,
            ),
            disease_types=("Adenomas and Adenocarcinomas",),
        )
        res_strict = evaluate_cohort(snap, req_strict_ts)

    export_manifest = export_cohort_result(res_4mod, DEMO_DIR)
    html_path = write_multimodal_demo_ui(res_4mod, DEMO_DIR / "index.html", strict_ts_result=res_strict)
    rna_prot = compute_rna_protein_summary(res_4mod)

    print(f"Verified offline snapshot: {res_4mod.snapshot_id} ({res_4mod.snapshot_manifest_sha256[:16]}...)")
    print(f"READY={res_4mod.ready_count} / {res_4mod.eligible_human_patients} eligible LUAD (controls excluded={res_4mod.excluded_controls})")
    print(f"Strict timestamp-only check: READY={res_strict.ready_count}, UNKNOWN={res_strict.unknown_count}")
    print("\nRNA vs Measured Protein Summary (Real CPTAC-3 LUAD READY cohort):")
    for row in rna_prot:
        print(
            f"  {row['gene']:8s} n={row['paired_patients']:3d}  r={str(row['pearson_r']):6s}  "
            f"discordant={row['discordant_count']:2d} ({row['discordant_pct']:4.1f}%)"
        )
    print(f"\nDemo HTML written -> {html_path}")
    print(f"Export SHA-256   -> {export_manifest['export_sha256']}")

    if args.serve:
        html_bytes = html_path.read_bytes()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path not in ("/", "/index.html"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline';")
                self.end_headers()
                self.wfile.write(html_bytes)

            def log_message(self, fmt: str, *msg_args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", args.port), _Handler)
        print(f"Serving read-only multimodal demo at http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
