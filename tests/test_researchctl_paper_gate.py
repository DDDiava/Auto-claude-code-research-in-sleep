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
  primary_metric: tail_acc
success_signals:
  - id: S1
    text: "tail_acc improves"
failure_signals:
  - id: F1
    text: "head_acc regresses"
budget:
  gpu_hours_limit: 1
  token_usd_limit: 1
stop_rules:
  - stop on NaN
""",
        encoding="utf-8",
    )


def write_valid_verdict(claim_dir: Path, claim_id: str = "C001", verdict: str = "supported") -> None:
    (claim_dir / "VERDICT.yaml").write_text(
        f"""claim_id: {claim_id}
verdict: {verdict}
confidence: medium
risk_notes:
  - limited smoke evidence
allowed_scope:
  abstract:
    - describe only the frozen toy setting
  conclusion:
    - claim remains protocol-bound
  forbidden:
    - no broad generalization
next_action:
  - merge_to_paper
""",
        encoding="utf-8",
    )


def write_valid_matrix(root: Path, run_id: str = "R001") -> None:
    (root / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text(
        f"""claims:
  - id: C001
    text: "Prototype gate improves tail accuracy"
    verdict: supported
    support_runs: [{run_id}]
    figure_table_refs: [fig1]
    allowed_scope: narrow
""",
        encoding="utf-8",
    )


def write_valid_citations(root: Path) -> None:
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


def prepare_merged_claim(root: Path) -> Path:
    run_ctl(root, "init")
    run_ctl(root, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(root, "claim", "create", "A001", "Good idea").stdout)
    claim_dir = root / claim["object_path"]
    run_ctl(root, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(root, "claim", "freeze-contract", "C001")
    run_ctl(root, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(root, "run", "finish", "--run", "R001")
    record_valid_run_provenance(root)
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(root, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_matrix(root)
    run_ctl(root, "claim", "merge", "C001")
    return claim_dir


def test_empty_citation_ledger_blocks_paper_build(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_matrix(tmp_path)

    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "CITATION_LEDGER.json has no citations" in audit.stdout


def test_incomplete_citation_metadata_blocks_paper_build(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_matrix(tmp_path)
    (tmp_path / ".aris" / "paper" / "CITATION_LEDGER.json").write_text(
        '{"citations":[{"key":"smith2025proto","title":"Prototype","source_url":"https://example.org"}]}\n',
        encoding="utf-8",
    )

    audit = run_ctl(tmp_path, "audit", "citation", check=False)
    assert audit.returncode == 2
    assert "metadata_verified is not true" in audit.stdout
    assert "missing used_in_sections" in audit.stdout


def test_id_only_claim_matrix_blocks_paper_build(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_citations(tmp_path)
    (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text("claims:\n  - id: C001\n", encoding="utf-8")

    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "CLAIM_MATRIX.yaml entry missing text: C001" in audit.stdout
    assert "CLAIM_MATRIX.yaml entry missing support_runs: C001" in audit.stdout
    assert "CLAIM_MATRIX.yaml entry missing figure_table_refs: C001" in audit.stdout


def test_claim_matrix_rejects_nonterminal_support_run(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_citations(tmp_path)
    with sqlite3.connect(tmp_path / ".aris" / "state.db") as conn:
        conn.execute(
            "INSERT INTO runs(id, claim_id, host, status, command, artifact_root, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("R999", "C001", "local", "running", "python train.py", "worktrees/C001/R999", "now", None),
        )
        conn.execute("UPDATE claims SET run_ids_json = ? WHERE id = 'C001'", (json.dumps(["R001", "R999"]),))
    write_valid_matrix(tmp_path, "R999")

    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "CLAIM_MATRIX.yaml support_run is not terminal: R999" in audit.stdout


def test_claim_matrix_requires_figure_table_provenance(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_citations(tmp_path)
    write_valid_matrix(tmp_path, "R001")
    matrix = tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml"
    matrix.write_text(matrix.read_text(encoding="utf-8").replace("figure_table_refs: [fig1]", "figure_table_refs: [missing_fig]"), encoding="utf-8")

    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "CLAIM_MATRIX.yaml figure_table_ref has no run provenance for C001: missing_fig" in audit.stdout


def test_claim_matrix_requires_existing_figure_artifact_path(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_citations(tmp_path)
    (tmp_path / "artifacts" / "R001" / "fig1.png").unlink()

    audit = run_ctl(tmp_path, "audit", "paper-build", check=False)
    assert audit.returncode == 2
    assert "CLAIM_MATRIX.yaml figure_table_ref has no run provenance for C001: fig1" in audit.stdout


def test_claim_matrix_does_not_accept_substring_figure_provenance(tmp_path: Path) -> None:
    run_ctl(tmp_path, "init")
    run_ctl(tmp_path, "anchor", "create", "Calibration")
    claim = read_json(run_ctl(tmp_path, "claim", "create", "A001", "Good idea").stdout)
    claim_dir = tmp_path / claim["object_path"]
    run_ctl(tmp_path, "claim", "gate", "C001", "--decision", "approve")
    write_valid_contract(claim_dir)
    run_ctl(tmp_path, "claim", "freeze-contract", "C001")
    run_ctl(tmp_path, "run", "start", "--claim", "C001", "--cmd", "python train.py")
    run_ctl(tmp_path, "run", "finish", "--run", "R001")
    record_valid_run_provenance(tmp_path, figure_ref="notfig10")
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics.\n", encoding="utf-8")
    run_ctl(tmp_path, "claim", "verdict", "C001", "--status", "supported")
    write_valid_verdict(claim_dir)
    write_valid_matrix(tmp_path, "R001")
    write_valid_citations(tmp_path)

    merge = run_ctl(tmp_path, "claim", "merge", "C001", check=False)
    assert merge.returncode == 1
    assert "CLAIM_MATRIX.yaml figure_table_ref has no run provenance for C001: fig1" in merge.stderr


def test_successful_paper_build_writes_manifest_and_blocked_build_preserves_it(tmp_path: Path) -> None:
    prepare_merged_claim(tmp_path)
    write_valid_matrix(tmp_path)
    write_valid_citations(tmp_path)

    build = read_json(run_ctl(tmp_path, "paper", "build").stdout)
    assert build["status"] == "success"
    manifest_path = tmp_path / ".aris" / "paper" / "BUILD_MANIFEST.json"
    manifest = read_json(manifest_path.read_text(encoding="utf-8"))
    assert manifest["build_id"] == "PB001"
    assert manifest["claims_checked"] == ["C001"]
    assert manifest["claim_matrix_hash"].startswith("sha256:")
    assert manifest["citation_ledger_hash"].startswith("sha256:")

    (tmp_path / ".aris" / "paper" / "CITATION_LEDGER.json").write_text('{"citations":[]}\n', encoding="utf-8")
    blocked = run_ctl(tmp_path, "paper", "build", check=False)
    assert blocked.returncode == 2
    preserved = read_json(manifest_path.read_text(encoding="utf-8"))
    assert preserved["build_id"] == "PB001"
