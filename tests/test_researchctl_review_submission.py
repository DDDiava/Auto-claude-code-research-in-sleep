from __future__ import annotations

import json
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


def write_valid_contract(claim_dir: Path) -> None:
    (claim_dir / "CONTRACT.yaml").write_text(
        """version: 1
claim_id: C001
hypothesis:
  text: "A falsifiable claim."
scope:
  baseline_commit: abc123
  primary_metric: tail_acc
success_signals:
  - id: S1
    text: "tail_acc improves"
failure_signals:
  - id: F1
    text: "head_acc drops"
budget:
  gpu_hours_limit: 1
  token_usd_limit: 1
stop_rules:
  - stop on NaN
""",
        encoding="utf-8",
    )


def write_valid_verdict(claim_dir: Path) -> None:
    (claim_dir / "VERDICT.yaml").write_text(
        """claim_id: C001
verdict: supported
confidence: medium
risk_notes:
  - smoke result only
allowed_scope:
  abstract:
    - describe the result only in this protocol
  conclusion:
    - keep the conclusion narrow
  forbidden:
    - do not imply broad generalization
next_action:
  - merge_to_paper
""",
        encoding="utf-8",
    )


def write_valid_paper_inputs(root: Path) -> None:
    (root / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text(
        """claims:
  - id: C001
    text: "Prototype gate improves tail accuracy"
    verdict: supported
    support_runs: [R001]
    figure_table_refs: [fig1]
    allowed_scope: narrow
""",
        encoding="utf-8",
    )
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


def write_valid_submission_checklist(root: Path) -> None:
    (root / ".aris" / "paper" / "SUBMISSION_CHECKLIST.json").write_text(
        json.dumps(
            {
                "repro_manifest": {
                    "code": "worktrees/C001",
                    "data": "toy split documented",
                    "hardware": "local test runner",
                    "seed_policy": "single smoke seed",
                },
                "authorship_ai_disclosure": "LLM assistance disclosed in appendix",
                "venue_format": {"venue": "internal-smoke", "verified": True},
                "external_artifacts_checked": True,
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


def prepare_submission_candidate(root: Path) -> Path:
    run_ctl(root, "init")
    run_ctl(root, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(root, "claim", "create", "A001", "Good idea").stdout)
    claim_dir = root / claim["object_path"]
    run_ctl(root, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(root, "claim", "freeze-contract", "C001")
    run_ctl(root, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(root, "run", "fail", "--run", "R001", "--status", "crashed")
    crashed = read_json(run_ctl(root, "run", "list", "--claim", "C001").stdout)[0]
    assert crashed["status"] == "crashed"
    run_ctl(root, "run", "finish", "--run", "R001", "--status", "success")
    record_valid_run_provenance(root)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(root, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_paper_inputs(root)
    run_ctl(root, "claim", "merge", "C001")
    write_valid_submission_checklist(root)
    return claim_dir


def test_submission_audit_requires_review_for_merged_claim(tmp_path: Path) -> None:
    prepare_submission_candidate(tmp_path)

    missing_review = run_ctl(tmp_path, "audit", "submission", check=False)
    assert missing_review.returncode == 2
    assert "submission missing successful paper build" in missing_review.stdout
    assert "submission missing review for merged claim: C001" in missing_review.stdout

    review = read_json(
        run_ctl(
            tmp_path,
            "review",
            "add",
            "--claim",
            "C001",
            "--role",
            "reviewer",
            "--decision",
            "supported",
            "--source",
            "human",
        ).stdout
    )
    assert review["id"] == "RV001"

    missing_build = run_ctl(tmp_path, "audit", "submission", check=False)
    assert missing_build.returncode == 2
    assert "submission missing successful paper build" in missing_build.stdout

    build = read_json(run_ctl(tmp_path, "paper", "build").stdout)
    assert build["status"] == "success"

    passed = read_json(run_ctl(tmp_path, "audit", "submission").stdout)
    assert passed["ok"] is True
    assert passed["reviews_checked"] == 1
    assert passed["reviews_by_claim"]["C001"][0]["decision"] == "supported"
    assert passed["paper_build_manifest"]["build_id"] == "PB001"


def test_submission_review_decisions_block_reject_and_comment_only(tmp_path: Path) -> None:
    prepare_submission_candidate(tmp_path)
    run_ctl(tmp_path, "paper", "build")

    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "reviewer", "--decision", "comment")
    comment_only = run_ctl(tmp_path, "audit", "submission", check=False)
    assert comment_only.returncode == 2
    assert "submission missing passing review for merged claim: C001" in comment_only.stdout

    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "reviewer", "--decision", "reject")
    rejected = run_ctl(tmp_path, "audit", "submission", check=False)
    assert rejected.returncode == 2
    assert "submission has blocking review for merged claim: C001 decision=reject" in rejected.stdout

    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "reviewer", "--decision", "supported")
    still_rejected = run_ctl(tmp_path, "audit", "submission", check=False)
    assert still_rejected.returncode == 2
    assert "submission has blocking review for merged claim: C001 decision=reject" in still_rejected.stdout


def test_submission_requires_current_paper_inputs_to_match_build_manifest(tmp_path: Path) -> None:
    prepare_submission_candidate(tmp_path)
    run_ctl(tmp_path, "paper", "build")
    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "reviewer", "--decision", "supported")

    matrix = tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml"
    matrix.write_text(matrix.read_text(encoding="utf-8").replace("allowed_scope: narrow", "allowed_scope: narrow bounded"), encoding="utf-8")

    changed = run_ctl(tmp_path, "audit", "submission", check=False)
    assert changed.returncode == 2
    assert "BUILD_MANIFEST.json claim_matrix_hash does not match current CLAIM_MATRIX.yaml" in changed.stdout


def test_submission_requires_submission_checklist(tmp_path: Path) -> None:
    prepare_submission_candidate(tmp_path)
    run_ctl(tmp_path, "paper", "build")
    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "reviewer", "--decision", "supported")
    (tmp_path / ".aris" / "paper" / "SUBMISSION_CHECKLIST.json").write_text(
        json.dumps(
            {
                "repro_manifest": {"code": "worktrees/C001"},
                "authorship_ai_disclosure": "",
                "venue_format": {"venue": "internal-smoke", "verified": False},
                "external_artifacts_checked": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    blocked = run_ctl(tmp_path, "audit", "submission", check=False)
    assert blocked.returncode == 2
    assert "SUBMISSION_CHECKLIST.json missing repro_manifest.data" in blocked.stdout
    assert "SUBMISSION_CHECKLIST.json missing authorship_ai_disclosure" in blocked.stdout
    assert "SUBMISSION_CHECKLIST.json venue_format.verified is not true" in blocked.stdout
    assert "SUBMISSION_CHECKLIST.json external_artifacts_checked is not true" in blocked.stdout


def test_review_list_can_filter_by_claim(tmp_path: Path) -> None:
    prepare_submission_candidate(tmp_path)
    run_ctl(tmp_path, "review", "add", "--claim", "C001", "--role", "judge", "--decision", "supported")

    reviews = read_json(run_ctl(tmp_path, "review", "list", "--claim", "C001").stdout)
    assert len(reviews) == 1
    assert reviews[0]["claim_id"] == "C001"
