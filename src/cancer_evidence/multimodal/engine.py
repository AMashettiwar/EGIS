from __future__ import annotations

from collections import Counter, defaultdict
from itertools import product
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .models import (
    AlignmentPolicy,
    CohortRequest,
    CohortResult,
    FunnelStage,
    GateReason,
    MappingStatus,
    ModalityObservation,
    PacketStatus,
    PatientPacket,
    QCStatus,
    ReportStatus,
)
from .store import MultimodalSnapshot, canonical_json_sha256, sha256_file


def _parse_features(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _check_crosswalk(
    crosswalk_rows: list[dict[str, Any]],
    required_sources: set[str],
    require_approved: bool,
) -> tuple[PacketStatus | None, GateReason | None, dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in crosswalk_rows:
        by_source[str(row["source"])].append(row)

    # First check for explicit conflict across any mapped source for this patient
    for source in sorted(required_sources):
        rows = by_source.get(source, [])
        statuses = {str(r["mapping_status"]) for r in rows}
        approved_ids = {
            str(r["source_patient_id"])
            for r in rows
            if str(r["mapping_status"]) == MappingStatus.APPROVED.value
        }
        if MappingStatus.CONFLICT.value in statuses or len(approved_ids) > 1:
            return (
                PacketStatus.CONFLICT,
                GateReason.CONFLICTING_MAPPING,
                {"source": source, "approved_ids": sorted(approved_ids), "statuses": sorted(statuses)},
            )

    if require_approved:
        for source in sorted(required_sources):
            rows = by_source.get(source, [])
            approved_ids = {
                str(r["source_patient_id"])
                for r in rows
                if str(r["mapping_status"]) == MappingStatus.APPROVED.value
            }
            if not approved_ids:
                statuses = sorted({str(r["mapping_status"]) for r in rows})
                reason = GateReason.UNAPPROVED_MAPPING if rows else GateReason.UNLINKED_PATIENT
                return (
                    PacketStatus.UNKNOWN,
                    reason,
                    {"source": source, "statuses": statuses},
                )

    return None, None, {}


def _evaluate_alignment(
    selected: dict[str, ModalityObservation],
    policy: AlignmentPolicy,
) -> tuple[bool, str, int | None, str | None]:
    """Check sample type, episode identity, and timestamp window across selected runs."""
    obs_list = list(selected.values())
    if not obs_list:
        return False, "EMPTY", None, None

    if policy.required_sample_type:
        for obs in obs_list:
            if obs.sample_type != policy.required_sample_type:
                return False, "SAMPLE_TYPE_MISMATCH", None, None

    episodes = {obs.episode_id for obs in obs_list if obs.episode_id}
    if policy.require_same_episode and len(episodes) > 1:
        return False, "EPISODE_MISMATCH", None, None
    resolved_episode = next(iter(episodes)) if len(episodes) == 1 else None

    days = [obs.collection_day_offset for obs in obs_list]
    if all(d is not None for d in days):
        day_ints = [int(d) for d in days if d is not None]
        delta = max(day_ints) - min(day_ints)
        if policy.max_day_delta is not None and delta > policy.max_day_delta:
            return False, "DAY_WINDOW_EXCEEDED", delta, resolved_episode
        return True, "TIMESTAMP_WINDOW", delta, resolved_episode

    # Partial or missing timestamps: never guess silently!
    specimens = {obs.source_specimen_id for obs in obs_list if obs.source_specimen_id}
    if (
        policy.allow_shared_specimen_fallback
        and len(specimens) == 1
        and all(bool(obs.source_specimen_id) for obs in obs_list)
    ):
        return True, "SHARED_SPECIMEN_ID", None, resolved_episode

    if policy.allow_missing_timestamps:
        if policy.require_same_episode and len(episodes) == 1 and all(bool(obs.episode_id) for obs in obs_list):
            return True, "SHARED_EPISODE_ID", None, resolved_episode
        if not policy.require_same_episode:
            return True, "EXPLICIT_ALLOW_MISSING_TIMESTAMPS", None, resolved_episode

    return False, "MISSING_TIMESTAMPS_AND_UNMATCHED_SPECIMEN", None, resolved_episode


def _select_modality_candidates(
    runs: list[ModalityObservation],
    policy: AlignmentPolicy,
) -> tuple[list[ModalityObservation], GateReason | None, PacketStatus | None, dict[str, Any]]:
    if not runs:
        return [], GateReason.MISSING_MODALITY, PacketStatus.UNKNOWN, {}

    active = [r for r in runs if r.report_status == ReportStatus.ACTIVE_FINAL.value]
    if not active:
        statuses = sorted({r.report_status for r in runs})
        if all(r.report_status == ReportStatus.SUPERSEDED.value for r in runs):
            return [], GateReason.SUPERSEDED_ONLY, PacketStatus.UNKNOWN, {"report_statuses": statuses}
        return [], GateReason.MISSING_MODALITY, PacketStatus.UNKNOWN, {"report_statuses": statuses}

    if policy.required_sample_type:
        matching_type = [r for r in active if r.sample_type == policy.required_sample_type]
        if not matching_type:
            return (
                [],
                GateReason.SAMPLE_TYPE_MISMATCH,
                PacketStatus.UNKNOWN,
                {
                    "required_sample_type": policy.required_sample_type,
                    "observed_sample_types": sorted({r.sample_type for r in active}),
                },
            )
    else:
        matching_type = active

    passing = [r for r in matching_type if r.qc_status == QCStatus.PASS.value]
    if not passing:
        return (
            [],
            GateReason.QC_FAILED,
            PacketStatus.UNKNOWN,
            {"observed_qc_statuses": sorted({r.qc_status for r in matching_type})},
        )

    # Detect unresolved conflicting ACTIVE_FINAL reports for the exact same specimen/episode
    by_slot: dict[tuple[str | None, str], list[ModalityObservation]] = defaultdict(list)
    for obs in passing:
        by_slot[(obs.episode_id, obs.source_specimen_id)].append(obs)

    deduped: list[ModalityObservation] = []
    for slot_key, slot_runs in sorted(by_slot.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        distinct_payloads = {canonical_json_sha256(r.features) for r in slot_runs}
        if len(distinct_payloads) > 1:
            return (
                [],
                GateReason.CONFLICTING_ACTIVE_REPORTS,
                PacketStatus.CONFLICT,
                {
                    "slot": {"episode_id": slot_key[0], "source_specimen_id": slot_key[1]},
                    "conflicting_run_ids": sorted(r.run_id for r in slot_runs),
                },
            )
        deduped.append(sorted(slot_runs, key=lambda r: r.run_id)[0])

    return deduped, None, None, {}


def evaluate_cohort(snapshot: MultimodalSnapshot, request: CohortRequest) -> CohortResult:
    """Deterministically gate a cohort and assemble verified per-patient multimodal packets."""
    if not request.required_modalities:
        raise ValueError("CohortRequest.required_modalities must not be empty")

    required_mods = tuple(dict.fromkeys(request.required_modalities))
    policy = request.alignment_policy

    # Load core tables deterministically via DuckDB
    patient_rows = snapshot.conn.execute(
        """
        SELECT canonical_patient_id, source, source_patient_id, is_control_or_pool,
               disease_type, primary_site, ajcc_stage
        FROM patients
        ORDER BY canonical_patient_id
        """
    ).fetchall()

    crosswalk_rows = snapshot.conn.execute(
        """
        SELECT canonical_patient_id, source, source_patient_id, mapping_status, authority, evidence_note
        FROM identity_crosswalk
        ORDER BY canonical_patient_id, source, source_patient_id
        """
    ).fetchall()

    run_rows = snapshot.conn.execute(
        """
        SELECT run_id, modality, canonical_patient_id, specimen_id, source_specimen_id,
               episode_id, source, source_run_id, qc_status, report_status, sample_type,
               collection_day_offset, pipeline_version, artifact_uri, artifact_sha256, features_json
        FROM modality_runs
        ORDER BY canonical_patient_id, modality, run_id
        """
    ).fetchall()

    crosswalk_by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in crosswalk_rows:
        crosswalk_by_patient[str(r[0])].append({
            "canonical_patient_id": r[0],
            "source": r[1],
            "source_patient_id": r[2],
            "mapping_status": r[3],
            "authority": r[4],
            "evidence_note": r[5],
        })

    runs_by_patient_mod: dict[tuple[str, str], list[ModalityObservation]] = defaultdict(list)
    sources_by_modality: dict[str, set[str]] = defaultdict(set)
    for r in run_rows:
        obs = ModalityObservation(
            run_id=str(r[0]),
            modality=str(r[1]),
            specimen_id=str(r[3] or ""),
            source_specimen_id=str(r[4] or ""),
            episode_id=str(r[5]) if r[5] is not None else None,
            source=str(r[6]),
            source_run_id=str(r[7] or ""),
            qc_status=str(r[8]),
            report_status=str(r[9]),
            sample_type=str(r[10] or ""),
            collection_day_offset=int(r[11]) if r[11] is not None else None,
            pipeline_version=str(r[12] or ""),
            artifact_uri=str(r[13]) if r[13] is not None else None,
            artifact_sha256=str(r[14]) if r[14] is not None else None,
            features=_parse_features(r[15]),
        )
        runs_by_patient_mod[(str(r[2]), obs.modality)].append(obs)
        sources_by_modality[obs.modality].add(obs.source)

    total_raw = len(patient_rows)
    controls = [p for p in patient_rows if bool(p[3])]
    humans = [p for p in patient_rows if not bool(p[3])]

    disease_filter = set(request.disease_types) if request.disease_types else None
    if disease_filter is not None:
        eligible_humans = [p for p in humans if p[4] in disease_filter]
        disease_excluded = len(humans) - len(eligible_humans)
    else:
        eligible_humans = humans
        disease_excluded = 0

    packets: list[PatientPacket] = []
    reason_counts: Counter[str] = Counter()

    # Track sequential funnel stages over eligible human patients
    crosswalk_ok_count = 0
    modalities_qc_ok_count = 0
    aligned_ready_count = 0

    for p in eligible_humans:
        pid = str(p[0])
        disease_type = str(p[4]) if p[4] is not None else None
        primary_site = str(p[5]) if p[5] is not None else None
        ajcc_stage = str(p[6]) if p[6] is not None else None

        # Determine which sources are needed for the required modalities
        patient_required_sources: set[str] = set()
        for mod in required_mods:
            obs_for_mod = runs_by_patient_mod.get((pid, mod), [])
            if obs_for_mod:
                patient_required_sources.update(o.source for o in obs_for_mod)
            elif mod in sources_by_modality:
                patient_required_sources.update(sources_by_modality[mod])

        # Stage 1: Check cross-silo identity crosswalk
        cw_status, cw_reason, cw_diag = _check_crosswalk(
            crosswalk_by_patient.get(pid, []),
            patient_required_sources,
            request.require_approved_mappings,
        )
        # Note: if a patient lacks runs for a modality AND has no crosswalk for that source,
        # report UNLINKED_PATIENT / UNAPPROVED_MAPPING only if crosswalk explicitly failed or has non-approved rows;
        # if crosswalk is clean (or only missing because patient simply wasn't assayed in that silo), check modality runs too.
        if cw_status == PacketStatus.CONFLICT and cw_reason is not None:
            reason_counts[cw_reason.value] += 1
            packets.append(
                PatientPacket(
                    canonical_patient_id=pid,
                    status=PacketStatus.CONFLICT,
                    reason_codes=(cw_reason,),
                    disease_type=disease_type,
                    primary_site=primary_site,
                    ajcc_stage=ajcc_stage,
                    episode_id=None,
                    alignment_mode="BLOCKED_CROSSWALK",
                    max_observed_day_delta=None,
                    modalities={},
                    witness_ids=(pid,),
                    diagnostics={"crosswalk": cw_diag},
                )
            )
            continue

        if cw_reason == GateReason.UNAPPROVED_MAPPING:
            reason_counts[cw_reason.value] += 1
            packets.append(
                PatientPacket(
                    canonical_patient_id=pid,
                    status=PacketStatus.UNKNOWN,
                    reason_codes=(cw_reason,),
                    disease_type=disease_type,
                    primary_site=primary_site,
                    ajcc_stage=ajcc_stage,
                    episode_id=None,
                    alignment_mode="BLOCKED_CROSSWALK",
                    max_observed_day_delta=None,
                    modalities={},
                    witness_ids=(pid,),
                    diagnostics={"crosswalk": cw_diag},
                )
            )
            continue

        # Check crosswalk for sources where patient ACTUALLY has runs
        present_sources = {
            obs.source
            for mod in required_mods
            for obs in runs_by_patient_mod.get((pid, mod), [])
        }
        present_cw_status, present_cw_reason, present_cw_diag = _check_crosswalk(
            crosswalk_by_patient.get(pid, []),
            present_sources,
            request.require_approved_mappings,
        )
        if present_cw_status is not None and present_cw_reason is not None:
            reason_counts[present_cw_reason.value] += 1
            packets.append(
                PatientPacket(
                    canonical_patient_id=pid,
                    status=present_cw_status,
                    reason_codes=(present_cw_reason,),
                    disease_type=disease_type,
                    primary_site=primary_site,
                    ajcc_stage=ajcc_stage,
                    episode_id=None,
                    alignment_mode="BLOCKED_CROSSWALK",
                    max_observed_day_delta=None,
                    modalities={},
                    witness_ids=(pid,),
                    diagnostics={"crosswalk": present_cw_diag},
                )
            )
            continue

        crosswalk_ok_count += 1

        # Stage 2: Per-modality presence, ACTIVE_FINAL, sample type, QC, and conflict checks
        candidates_by_mod: dict[str, list[ModalityObservation]] = {}
        mod_reasons: list[GateReason] = []
        mod_statuses: list[PacketStatus] = []
        mod_diag: dict[str, Any] = {}

        for mod in required_mods:
            cands, reason, status, diag = _select_modality_candidates(
                runs_by_patient_mod.get((pid, mod), []),
                policy,
            )
            if reason is not None and status is not None:
                mod_reasons.append(reason)
                mod_statuses.append(status)
                mod_diag[mod] = {"reason": reason.value, **diag}
            else:
                candidates_by_mod[mod] = cands

        if mod_reasons:
            final_status = (
                PacketStatus.CONFLICT
                if PacketStatus.CONFLICT in mod_statuses
                else PacketStatus.UNKNOWN
            )
            unique_reasons = tuple(dict.fromkeys(mod_reasons))
            for r in unique_reasons:
                reason_counts[r.value] += 1
            packets.append(
                PatientPacket(
                    canonical_patient_id=pid,
                    status=final_status,
                    reason_codes=unique_reasons,
                    disease_type=disease_type,
                    primary_site=primary_site,
                    ajcc_stage=ajcc_stage,
                    episode_id=None,
                    alignment_mode="BLOCKED_MODALITY_GATE",
                    max_observed_day_delta=None,
                    modalities={},
                    witness_ids=(pid,),
                    diagnostics={"modality_gates": mod_diag},
                )
            )
            continue

        modalities_qc_ok_count += 1

        # Stage 3: Temporal & Episode Alignment across all required modalities
        mod_order = list(required_mods)
        best_valid: list[tuple[dict[str, ModalityObservation], str, int | None, str | None]] = []
        rejection_modes: list[tuple[str, int | None]] = []

        for combo in product(*(candidates_by_mod[m] for m in mod_order)):
            selected = dict(zip(mod_order, combo))
            ok, mode, delta, ep = _evaluate_alignment(selected, policy)
            if ok:
                best_valid.append((selected, mode, delta, ep))
            else:
                rejection_modes.append((mode, delta))

        if not best_valid:
            reason_counts[GateReason.TEMPORAL_MISALIGNMENT.value] += 1
            min_delta = min((d for _, d in rejection_modes if d is not None), default=None)
            modes = sorted({m for m, _ in rejection_modes})
            packets.append(
                PatientPacket(
                    canonical_patient_id=pid,
                    status=PacketStatus.UNKNOWN,
                    reason_codes=(GateReason.TEMPORAL_MISALIGNMENT,),
                    disease_type=disease_type,
                    primary_site=primary_site,
                    ajcc_stage=ajcc_stage,
                    episode_id=None,
                    alignment_mode=modes[0] if modes else "TEMPORAL_MISALIGNMENT",
                    max_observed_day_delta=min_delta,
                    modalities={},
                    witness_ids=(pid,),
                    diagnostics={
                        "alignment_rejections": modes,
                        "min_observed_day_delta": min_delta,
                        "max_allowed_day_delta": policy.max_day_delta,
                    },
                )
            )
            continue

        # Prefer TIMESTAMP_WINDOW over fallback, then smallest day delta, then deterministic run_id order
        mode_rank = {
            "TIMESTAMP_WINDOW": 0,
            "SHARED_SPECIMEN_ID": 1,
            "SHARED_EPISODE_ID": 2,
            "EXPLICIT_ALLOW_MISSING_TIMESTAMPS": 3,
        }
        best_valid.sort(
            key=lambda item: (
                mode_rank.get(item[1], 9),
                item[2] if item[2] is not None else 10**9,
                tuple(item[0][m].run_id for m in mod_order),
            )
        )
        chosen_mods, chosen_mode, chosen_delta, chosen_ep = best_valid[0]
        aligned_ready_count += 1
        witness_ids = tuple(
            [pid]
            + [chosen_mods[m].source_specimen_id for m in mod_order if chosen_mods[m].source_specimen_id]
            + [chosen_mods[m].run_id for m in mod_order]
        )

        packets.append(
            PatientPacket(
                canonical_patient_id=pid,
                status=PacketStatus.READY,
                reason_codes=(GateReason.NONE,),
                disease_type=disease_type,
                primary_site=primary_site,
                ajcc_stage=ajcc_stage,
                episode_id=chosen_ep,
                alignment_mode=chosen_mode,
                max_observed_day_delta=chosen_delta,
                modalities=chosen_mods,
                witness_ids=tuple(dict.fromkeys(witness_ids)),
                diagnostics={"candidate_aligned_combinations": len(best_valid)},
            )
        )

    funnel = (
        FunnelStage("raw_records_in_snapshot", total_raw, 0, "ALL_RECORDS"),
        FunnelStage(
            "human_patients_excluding_controls",
            len(humans),
            len(controls),
            GateReason.CONTROL_OR_NON_PATIENT.value,
        ),
        FunnelStage(
            "disease_eligible_patients",
            len(eligible_humans),
            disease_excluded,
            GateReason.DISEASE_FILTER_EXCLUDED.value,
        ),
        FunnelStage(
            "cross_silo_identity_approved",
            crosswalk_ok_count,
            len(eligible_humans) - crosswalk_ok_count,
            "CROSSWALK_GATE",
        ),
        FunnelStage(
            "all_required_modalities_active_and_qc_pass",
            modalities_qc_ok_count,
            crosswalk_ok_count - modalities_qc_ok_count,
            "MODALITY_COMPLETENESS_AND_QC_GATE",
        ),
        FunnelStage(
            "temporally_and_specimen_aligned_ready",
            aligned_ready_count,
            modalities_qc_ok_count - aligned_ready_count,
            GateReason.TEMPORAL_MISALIGNMENT.value,
        ),
    )

    ready_count = sum(1 for p in packets if p.status == PacketStatus.READY)
    unknown_count = sum(1 for p in packets if p.status == PacketStatus.UNKNOWN)
    conflict_count = sum(1 for p in packets if p.status == PacketStatus.CONFLICT)

    disease_desc = (
        f"disease_type IN {sorted(disease_filter)}" if disease_filter else "all human disease types"
    )
    denom_def = (
        f"Non-control human patients in snapshot '{snapshot.snapshot_id}' ({disease_desc}) "
        f"evaluated for required modalities {list(required_mods)} under sample_type="
        f"'{policy.required_sample_type}', max_day_delta={policy.max_day_delta}, "
        f"require_same_episode={policy.require_same_episode}."
    )

    return CohortResult(
        snapshot_id=snapshot.snapshot_id,
        snapshot_manifest_sha256=snapshot.manifest_sha256,
        request=request,
        denominator_definition=denom_def,
        total_raw_records=total_raw,
        excluded_controls=len(controls),
        eligible_human_patients=len(eligible_humans),
        ready_count=ready_count,
        unknown_count=unknown_count,
        conflict_count=conflict_count,
        funnel=funnel,
        reason_breakdown=dict(reason_counts),
        packets=tuple(packets),
    )


def export_cohort_result(result: CohortResult, out_dir: str | Path) -> dict[str, Any]:
    """Export locked cohort summary + READY patient feature packets to JSON and Parquet."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)

    summary_path = root / "cohort_result.json"
    summary_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    ready_rows: list[dict[str, Any]] = []
    for pkt in result.packets:
        if pkt.status != PacketStatus.READY:
            continue
        row: dict[str, Any] = {
            "canonical_patient_id": pkt.canonical_patient_id,
            "disease_type": pkt.disease_type,
            "primary_site": pkt.primary_site,
            "ajcc_stage": pkt.ajcc_stage,
            "episode_id": pkt.episode_id,
            "alignment_mode": pkt.alignment_mode,
            "max_observed_day_delta": pkt.max_observed_day_delta,
            "witness_ids_json": json.dumps(list(pkt.witness_ids)),
        }
        for mod_name, obs in sorted(pkt.modalities.items()):
            row[f"{mod_name}__run_id"] = obs.run_id
            row[f"{mod_name}__specimen_id"] = obs.source_specimen_id
            row[f"{mod_name}__features_json"] = json.dumps(obs.features, sort_keys=True)
        ready_rows.append(row)

    parquet_path = root / "ready_patient_packets.parquet"
    if ready_rows:
        table = pa.Table.from_pylist(ready_rows)
    else:
        table = pa.table({"canonical_patient_id": pa.array([], type=pa.string())})
    pq.write_table(table, parquet_path, compression="zstd")

    manifest_body = {
        "snapshot_id": result.snapshot_id,
        "snapshot_manifest_sha256": result.snapshot_manifest_sha256,
        "cohort_id": result.request.cohort_id,
        "ready_count": result.ready_count,
        "eligible_human_patients": result.eligible_human_patients,
        "files": {
            "cohort_result.json": {"sha256": sha256_file(summary_path)},
            "ready_patient_packets.parquet": {
                "sha256": sha256_file(parquet_path),
                "row_count": len(ready_rows),
            },
        },
    }
    export_manifest = dict(manifest_body)
    export_manifest["export_sha256"] = canonical_json_sha256(manifest_body)
    (root / "export_manifest.json").write_text(
        json.dumps(export_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return export_manifest
