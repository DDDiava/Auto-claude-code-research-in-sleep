from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def run_ctl(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "researchctl", "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def read_json(stdout: str):
    return json.loads(stdout)


def write_valid_contract(claim_dir: Path, claim_id: str = "C001") -> None:
    (claim_dir / "CONTRACT.yaml").write_text(
        f"""version: 1
claim_id: {claim_id}

hypothesis:
  text: "A falsifiable claim."

scope:
  baseline_commit: abc123
  datasets:
    - toy
  primary_metric: tail_acc
  fixed_protocol:
    - same_split

success_signals:
  - id: S1
    text: "tail_acc improves over baseline"

failure_signals:
  - id: F1
    text: "metric regresses or run is unstable"

budget:
  gpu_hours_limit: 20
  token_usd_limit: 30

stop_rules:
  - "Stop if pilot evidence contradicts the primary signal."
""",
        encoding="utf-8",
    )


def write_valid_verdict(claim_dir: Path, claim_id: str = "C001", verdict: str = "supported") -> None:
    (claim_dir / "VERDICT.yaml").write_text(
        f"""claim_id: {claim_id}
verdict: {verdict}
confidence: medium

risk_notes:
  - evidence is from a toy smoke run
allowed_scope:
  abstract:
    - claim may be described in the tested setting
  conclusion:
    - result is bounded by the frozen protocol
  forbidden:
    - do not claim broad generalization
next_action:
  - merge_to_paper
""",
        encoding="utf-8",
    )


def write_valid_claim_matrix(root: Path, claim_id: str = "C001", verdict: str = "supported", run_id: str = "R001") -> None:
    (root / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text(
        f"""claims:
  - id: {claim_id}
    text: "Prototype gate improves tail accuracy"
    verdict: {verdict}
    support_runs: [{run_id}]
    figure_table_refs: [fig1]
    allowed_scope: narrow
""",
        encoding="utf-8",
    )


def write_valid_citation_ledger(root: Path) -> None:
    (root / ".aris" / "paper" / "CITATION_LEDGER.json").write_text(
        json.dumps(
            {
                "citations": [
                    {
                        "key": "smith2025proto",
                        "title": "Prototype Calibration",
                        "source_url": "https://example.org/proto",
                        "metadata_verified": True,
                        "used_in_sections": ["related_work"],
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def record_valid_run_provenance(root: Path, run_id: str = "R001", figure_ref: str = "fig1") -> None:
    artifact_dir = root / "artifacts" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "metrics.json").write_text('{"tail_acc": 1.0}\n', encoding="utf-8")
    (artifact_dir / f"{figure_ref}.png").write_bytes(b"figure")
    run_ctl(
        root,
        "run",
        "event",
        "--run",
        run_id,
        "--type",
        "metrics_recorded",
        "--payload",
        json.dumps({"metrics_path": f"artifacts/{run_id}/metrics.json"}),
    )
    run_ctl(
        root,
        "run",
        "event",
        "--run",
        run_id,
        "--type",
        "artifact_recorded",
        "--payload",
        json.dumps({"artifact_path": f"artifacts/{run_id}/{figure_ref}.png"}),
    )


def test_init_installs_workflow_constitution_and_blocks_empty_paper_build(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")

    workflow = tmp_path / ".aris" / "workflow.md"
    assert workflow.exists()
    workflow_text = workflow.read_text(encoding="utf-8")
    for state in ("no_claim", "draft", "gated", "running", "judging", "merged", "archived"):
        assert f"[workflow-state:{state}]" in workflow_text

    build = run_ctl(tmp_path, "paper", "build", check=False)
    assert build.returncode == 2
    blockers = read_json(build.stdout)["blockers"]
    assert "merged_claims.yaml has no merged claims" in blockers
    assert "CLAIM_MATRIX.yaml has no claims" in blockers


def test_claim_lifecycle_snapshot_and_finish_gate(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    anchor = read_json(run_ctl(tmp_path, "anchor", "create", "Long tail calibration").stdout)
    assert anchor["id"] == "A001"

    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Prototype gate improves tail accuracy").stdout)
    assert claim["id"] == "C001"
    assert claim["creator"] == "human"
    assert claim["assignee"] == "builder"
    assert claim["next_action"][0]["action"] == "claim-gate"
    claim_dir = tmp_path / claim["object_path"]
    assert (claim_dir / "claim.json").exists()
    assert (tmp_path / "worktrees" / "C001").is_dir()

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    blockers = read_json(finish.stdout)["blockers"]
    assert "contract is not frozen" in blockers
    assert "no runs are registered" in blockers

    run_before_freeze = run_ctl(
        tmp_path,
        "run",
        "start",
        "--claim",
        "C001",
        "--cmd",
        "python train.py",
        check=False,
    )
    assert run_before_freeze.returncode == 1
    assert "draft claim cannot start a run" in run_before_freeze.stderr

    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    default_freeze = run_ctl(tmp_path, "claim", "freeze-contract", "C001", check=False)
    assert default_freeze.returncode == 1
    assert "CONTRACT.yaml empty baseline" in default_freeze.stderr
    write_valid_contract(claim_dir)
    frozen = read_json(run_ctl(tmp_path, "claim", "freeze-contract", "C001").stdout)
    assert frozen["contract_hash"].startswith("sha256:")

    session = read_json(
        run_ctl(
            tmp_path,
            "session",
            "attach",
            "--session",
            "S1",
            "--claim",
            "C001",
            "--role",
            "builder",
        ).stdout
    )
    assert session["claim_id"] == "C001"

    snapshot = read_json(run_ctl(tmp_path, "snapshot", "session", "--session", "S1").stdout)
    assert snapshot["claim"] == "C001"
    assert snapshot["stage"] == "contract_frozen"
    assert claim["object_path"] in snapshot["allowed_paths"]

    run = read_json(
        run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py --seed 1").stdout
    )
    assert run["id"] == "R001"
    first_finish = read_json(run_ctl(tmp_path, "run", "finish", "--run", "R001").stdout)
    second_finish = read_json(run_ctl(tmp_path, "run", "finish", "--run", "R001").stdout)
    assert first_finish["status"] == "success"
    assert second_finish["idempotent"] is True
    record_valid_run_provenance(tmp_path)

    evidence = claim_dir / "EVIDENCE.md"
    evidence.write_text(
        "# Evidence Summary\n\n## Runs Included\n\n- R001\n\n## Main Metrics\n\nTail accuracy improved by 2.3 points over the frozen baseline.\n",
        encoding="utf-8",
    )
    verdict = read_json(
        run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported", "--confidence", "medium").stdout
    )
    assert verdict["verdict"] == "supported"
    write_valid_verdict(claim_dir)
    run_after_verdict = run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py", check=False)
    assert run_after_verdict.returncode == 1
    assert "final claim cannot start a run" in run_after_verdict.stderr
    write_valid_claim_matrix(tmp_path)

    finish_ok = read_json(run_ctl(tmp_path, "claim", "finish", "C001").stdout)
    assert finish_ok["ok"] is True
    finish_again = read_json(run_ctl(tmp_path, "claim", "finish", "C001").stdout)
    assert finish_again["idempotent"] is True

    conn = sqlite3.connect(tmp_path / ".aris" / "state.db")
    run_finished_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE entity_type='run' AND entity_id='R001' AND event_type='run_finished'"
    ).fetchone()[0]
    assert run_finished_events == 1
    claim_finished_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE entity_type='claim' AND entity_id='C001' AND event_type='claim_finished'"
    ).fetchone()[0]
    assert claim_finished_events == 1

    (claim_dir / "CONTRACT.yaml").write_text(
        (claim_dir / "CONTRACT.yaml").read_text(encoding="utf-8") + "\n# post hoc edit\n",
        encoding="utf-8",
    )
    finish_changed = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish_changed.returncode == 2
    assert "CONTRACT.yaml changed after freeze" in finish_changed.stdout


def test_policy_blocks_unsupported_claims_from_merge_and_paper_build(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Bad idea").stdout)
    claim_dir = tmp_path / claim["object_path"]

    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001", "--status", "failed")
    (claim_dir / "EVIDENCE.md").write_text("Evidence says this failed with repeated NaN.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "invalidated")

    merge = run_ctl(tmp_path, "claim", "merge", "C001", check=False)
    assert merge.returncode == 1
    assert "claim is not mergeable" in merge.stderr

    paper_dir = tmp_path / ".aris" / "paper"
    (paper_dir / "CLAIM_MATRIX.yaml").write_text(
        "claims:\n  - id: C001\n    text: \"Bad idea\"\n    verdict: invalidated\n",
        encoding="utf-8",
    )
    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "unsupported claim appears in paper files" in audit.stdout


def test_claim_kill_writes_failure_memory_and_blocks_execution(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    run_ctl(tmp_path, "claim", "create", "A001", "Unsafe claim")

    killed = read_json(run_ctl(tmp_path, "claim", "kill", "C001", "--reason", "novelty overlap").stdout)
    assert killed["status"] == "killed"
    assert killed["verdict"] == "killed"
    assert "novelty overlap" in (tmp_path / ".aris" / "memory" / "failed_claims.md").read_text(encoding="utf-8")

    run = run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py", check=False)
    assert run.returncode == 1
    assert "claim status cannot start a run" in run.stderr

    merge = run_ctl(tmp_path, "claim", "merge", "C001", check=False)
    assert merge.returncode == 1
    assert "claim is not mergeable" in merge.stderr


def test_paper_enqueue_is_idempotent_and_blocks_bad_verdicts(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    good = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Good idea").stdout)
    good_dir = tmp_path / good["object_path"]
    bad = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Bad idea").stdout)
    bad_dir = tmp_path / bad["object_path"]

    for claim_id, claim_dir, verdict in (("C001", good_dir, "partial_supported"), ("C002", bad_dir, "inconclusive")):
        run_ctl(tmp_path, "claim", "gate", claim_id, "--decision", "approve")
        write_valid_contract(claim_dir, claim_id)
        run_ctl(tmp_path, "claim", "freeze-contract", claim_id)
        run_ctl(tmp_path, "run", "start", "--claim", claim_id, "--cmd", "python train.py")
        run_ctl(tmp_path, "run", "finish", "--run", f"R00{claim_id[-1]}")
        record_valid_run_provenance(tmp_path, f"R00{claim_id[-1]}")
        (claim_dir / "EVIDENCE.md").write_text(f"Evidence includes R00{claim_id[-1]} and enough metrics.\n", encoding="utf-8")
        run_ctl(tmp_path, "claim", "verdict", claim_id, "--status", verdict)
        write_valid_verdict(claim_dir, claim_id, verdict)

    write_valid_claim_matrix(tmp_path, "C001", "partial_supported", "R001")
    queued = read_json(run_ctl(tmp_path, "paper", "enqueue", "C001").stdout)
    assert queued["paper_merge_status"] == "queued"
    queued_again = read_json(run_ctl(tmp_path, "paper", "enqueue", "C001").stdout)
    assert queued_again["idempotent"] is True

    blocked = run_ctl(tmp_path, "paper", "enqueue", "C002", check=False)
    assert blocked.returncode == 1
    assert "not eligible for paper enqueue" in blocked.stderr

    manual_block = read_json(run_ctl(tmp_path, "paper", "block", "--reason", "unsupported claim leakage").stdout)
    assert manual_block["status"] == "blocked"
    assert manual_block["block_reason"] == "unsupported claim leakage"


def test_paper_enqueue_requires_finished_evidence_backed_claim(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Unsupported shortcut").stdout)
    claim_dir = tmp_path / claim["object_path"]

    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)

    queued = run_ctl(tmp_path, "paper", "enqueue", "C001", check=False)
    assert queued.returncode == 1
    assert "claim finish gate failed" in queued.stderr
    assert "no runs are registered" in queued.stderr


def test_failed_finish_does_not_write_failure_memory(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Failed shortcut").stdout)
    claim_dir = tmp_path / claim["object_path"]

    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "invalidated")
    write_valid_verdict(claim_dir, "C001", "invalidated")

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "no runs are registered" in finish.stdout
    archive = run_ctl(tmp_path, "claim", "archive", "C001", check=False)
    assert archive.returncode == 1
    assert "claim finish gate failed" in archive.stderr
    memory = tmp_path / ".aris" / "memory" / "failed_claims.md"
    assert not memory.exists() or "C001" not in memory.read_text(encoding="utf-8")


def test_contract_and_verdict_schema_are_enforced(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Schema claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    contract = claim_dir / "CONTRACT.yaml"
    contract.write_text("version: 1\nclaim_id: C001\nhypothesis:\n  text: x\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")

    frozen = run_ctl(tmp_path, "claim", "freeze-contract", "C001", check=False)
    assert frozen.returncode == 1
    assert "CONTRACT.yaml missing baseline" in frozen.stderr

    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 with enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    (claim_dir / "VERDICT.yaml").write_text("claim_id: C001\nverdict: supported\nconfidence: medium\n", encoding="utf-8")
    (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text("claims:\n  - id: C001\n", encoding="utf-8")

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "VERDICT.yaml missing risk_notes" in finish.stdout


def test_finish_claim_requires_evidence_to_reference_registered_runs(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Evidence claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text("Evidence has enough metrics but omits the run id.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text("claims:\n  - id: C001\n", encoding="utf-8")

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "EVIDENCE.md does not reference registered run: R001" in finish.stdout


def test_finish_claim_requires_metrics_provenance(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Metrics claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_claim_matrix(tmp_path)

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "run missing metrics provenance: R001" in finish.stdout


def test_finish_claim_rejects_fake_metrics_event(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Fake metrics claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    run_ctl(tmp_path, "run", "event", "--run", "R001", "--type", "metrics_recorded", "--payload", json.dumps({"note": "trust me"}))
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_claim_matrix(tmp_path)

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "run missing metrics provenance: R001" in finish.stdout


def test_finish_claim_rejects_missing_metrics_event_path(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Missing metrics claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    run_ctl(
        tmp_path,
        "run",
        "event",
        "--run",
        "R001",
        "--type",
        "metrics_recorded",
        "--payload",
        json.dumps({"metrics_path": "artifacts/R001/missing/metrics.json"}),
    )
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_claim_matrix(tmp_path)

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "run missing metrics provenance: R001" in finish.stdout


def test_finish_claim_requires_claim_matrix_not_just_merged_claims(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Matrix claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    (tmp_path / ".aris" / "paper" / "merged_claims.yaml").write_text("claims:\n  - C001\n", encoding="utf-8")

    finish = run_ctl(tmp_path, "claim", "finish", "C001", check=False)
    assert finish.returncode == 2
    assert "CLAIM_MATRIX.yaml does not include this mergeable claim" in finish.stdout


def test_direct_merge_requires_complete_claim_matrix_entry(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Matrix complete claim").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text("claims:\n  - id: C001\n", encoding="utf-8")

    merge = run_ctl(tmp_path, "claim", "merge", "C001", check=False)
    assert merge.returncode == 1
    assert "CLAIM_MATRIX.yaml entry missing text: C001" in merge.stderr
    assert "CLAIM_MATRIX.yaml entry missing support_runs: C001" in merge.stderr


def test_merge_does_not_duplicate_existing_claim_matrix_entry(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Good idea").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_claim_matrix(tmp_path)

    finish = read_json(run_ctl(tmp_path, "claim", "finish", "C001").stdout)
    assert finish["ok"] is True
    merged = read_json(run_ctl(tmp_path, "claim", "merge", "C001").stdout)
    assert merged["paper_merge_status"] == "merged"

    matrix = (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").read_text(encoding="utf-8")
    assert matrix.count("id: C001") == 1
    assert "figure_table_refs: [pending]" not in matrix


def test_supported_claim_can_merge_and_paper_build(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Good idea").stdout)
    claim_dir = tmp_path / claim["object_path"]

    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path)
    (claim_dir / "EVIDENCE.md").write_text(
        "Evidence includes R001 and enough metrics to support the claim.\n",
        encoding="utf-8",
    )
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)

    direct_merge = run_ctl(tmp_path, "claim", "merge", "C001", check=False)
    assert direct_merge.returncode == 1
    assert "CLAIM_MATRIX.yaml does not include this mergeable claim" in direct_merge.stderr

    write_valid_claim_matrix(tmp_path)
    merged = read_json(run_ctl(tmp_path, "claim", "merge", "C001").stdout)
    assert merged["paper_merge_status"] == "merged"
    run_after_merge = run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py", check=False)
    assert run_after_merge.returncode == 1
    assert "final claim cannot start a run" in run_after_merge.stderr

    with sqlite3.connect(tmp_path / ".aris" / "state.db") as conn:
        claim_finished_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE entity_type='claim' AND entity_id='C001' AND event_type='claim_finished'"
        ).fetchone()[0]
    assert claim_finished_events == 1

    write_valid_citation_ledger(tmp_path)
    audit = read_json(run_ctl(tmp_path, "audit", "paper-build").stdout)
    assert audit["ok"] is True
    build = read_json(run_ctl(tmp_path, "paper", "build").stdout)
    assert build["status"] == "success"
