from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from researchctl.core import ResearchCtlError, attach_session, create_anchor, create_claim, freeze_contract, gate_claim, start_run
from researchctl.db import connect, one
from researchctl.hooks import load_workflow_blocks, post_tool_hook, pre_tool_hook, stop_hook, workflow_state_hook


WORKFLOW = """# Research Workflow

[workflow-state:no_claim]
No claim block.
[/workflow-state:no_claim]

[workflow-state:draft]
Draft block.
[/workflow-state:draft]

[workflow-state:gated]
Gated block.
[/workflow-state:gated]

[workflow-state:contract_frozen]
Contract frozen block.
[/workflow-state:contract_frozen]

[workflow-state:running]
Running block.
[/workflow-state:running]
"""


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


def write_valid_verdict(claim_dir: Path, claim_id: str = "C001") -> None:
    (claim_dir / "VERDICT.yaml").write_text(
        f"""claim_id: {claim_id}
verdict: supported
confidence: medium
risk_notes:
  - evidence is bounded to this smoke claim
allowed_scope:
  abstract:
    - claim may be summarized in the tested setting
  conclusion:
    - use only within the frozen protocol
  forbidden:
    - do not generalize beyond the test
next_action:
  - merge_to_paper
""",
        encoding="utf-8",
    )


def test_workflow_state_hook_reads_blocks_from_workflow_md(tmp_path: Path) -> None:
    (tmp_path / ".aris").mkdir()
    (tmp_path / ".aris" / "workflow.md").write_text(WORKFLOW, encoding="utf-8")
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", root_arg=tmp_path)

    blocks = load_workflow_blocks(tmp_path)
    assert blocks["draft"] == "Draft block."

    payload = workflow_state_hook(tmp_path, {"session": "S1"})
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Claim: C001 (draft)" in context
    assert "Draft block." in context

    gate_claim("C001", "approve", root_arg=tmp_path)
    payload = workflow_state_hook(tmp_path, {"session": "S1"})
    gated_context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Claim: C001 (gated)" in gated_context
    assert "Gated block." in gated_context

    claim_dir = next((tmp_path / ".aris" / "anchors" / "A001_anchor" / "CLAIMS").glob("C001_*"))
    write_valid_contract(claim_dir)
    freeze_contract("C001", root_arg=tmp_path)
    payload = workflow_state_hook(tmp_path, {"session": "S1"})
    frozen_context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Claim: C001 (contract_frozen)" in frozen_context
    assert "Contract frozen block." in frozen_context
    assert "Gated block." not in frozen_context

    start_run("C001", "python train.py", root_arg=tmp_path)
    payload = workflow_state_hook(tmp_path, {"session": "S1"})
    running_context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Claim: C001 (running)" in running_context
    assert "Running block." in running_context


def test_missing_workflow_state_block_falls_back_visibly(tmp_path: Path) -> None:
    (tmp_path / ".aris").mkdir()
    (tmp_path / ".aris" / "workflow.md").write_text(
        "[workflow-state:no_claim]\nNo claim.\n[/workflow-state:no_claim]\n",
        encoding="utf-8",
    )
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", root_arg=tmp_path)

    payload = workflow_state_hook(tmp_path, {"session": "S1"})
    assert "Refer to .aris/workflow.md for current step." in payload["hookSpecificOutput"]["additionalContext"]


def test_session_local_active_claims_do_not_collide(tmp_path: Path) -> None:
    (tmp_path / ".aris").mkdir()
    (tmp_path / ".aris" / "workflow.md").write_text(WORKFLOW, encoding="utf-8")
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "First", root_arg=tmp_path)
    create_claim("A001", "Second", root_arg=tmp_path)
    attach_session("S1", "C001", root_arg=tmp_path)
    attach_session("S2", "C002", root_arg=tmp_path)

    s1 = workflow_state_hook(tmp_path, {"session": "S1"})["hookSpecificOutput"]["additionalContext"]
    s2 = workflow_state_hook(tmp_path, {"session": "S2"})["hookSpecificOutput"]["additionalContext"]
    assert "Claim: C001" in s1
    assert "Claim: C002" in s2
    assert (tmp_path / ".aris" / ".runtime" / "sessions" / "S1.json").exists()
    assert (tmp_path / ".aris" / ".runtime" / "sessions" / "S2.json").exists()


def test_write_sessions_cannot_share_one_worktree(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    metadata = tmp_path / "worktrees" / "C001" / "worktree.yaml"
    assert metadata.exists()
    assert "claim_id: C001" in metadata.read_text(encoding="utf-8")
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    try:
        attach_session("S2", "C001", role="builder", root_arg=tmp_path)
    except ResearchCtlError as exc:
        assert "active write session" in str(exc)
    else:
        raise AssertionError("second write session should be rejected")

    readonly = attach_session("S3", "C001", role="judge", root_arg=tmp_path)
    assert readonly["session_key"] == "S3"


def test_session_attach_rejects_mismatched_worktree_metadata(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    metadata = tmp_path / "worktrees" / "C001" / "worktree.yaml"
    metadata.write_text(metadata.read_text(encoding="utf-8").replace("claim_id: C001", "claim_id: C999"), encoding="utf-8")

    try:
        attach_session("S1", "C001", role="builder", root_arg=tmp_path)
    except ResearchCtlError as exc:
        assert "metadata claim_id does not match" in str(exc)
    else:
        raise AssertionError("mismatched worktree metadata should be rejected")


def test_pre_tool_hook_denies_cross_worktree_write(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Write",
            "tool_input": {"path": "worktrees/C999/train.py"},
        },
    )
    assert code == 2
    assert payload["decision"] == "deny"
    assert "outside active claim allowed paths" in payload["blockers"][0]


def test_codex_pre_tool_platform_output_uses_permission_schema(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "researchctl",
            "--root",
            str(tmp_path),
            "hook",
            "pre-tool",
            "--platform",
            "codex",
        ],
        input=json.dumps(
            {
                "session": "S1",
                "tool_name": "Write",
                "tool_input": {"path": "worktrees/C999/train.py"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "decision" not in payload
    assert payload["continue"] is True
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    assert "outside active claim allowed paths" in hook_output["permissionDecisionReason"]


def test_codex_pre_tool_platform_output_omits_allow_permission_decision(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "researchctl",
            "--root",
            str(tmp_path),
            "hook",
            "pre-tool",
            "--platform",
            "codex",
        ],
        input=json.dumps(
            {
                "session": "S1",
                "tool_name": "Write",
                "tool_input": {"path": "artifacts/R001/metrics.json"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["continue"] is True
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hook_output


def test_pre_tool_hook_denies_bash_cross_worktree_write(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Bash",
            "tool_input": {"command": "mkdir -p worktrees/C999 && echo x > worktrees/C999/out.txt"},
        },
    )
    assert code == 2
    assert payload["decision"] == "deny"
    assert "outside active claim allowed paths" in "; ".join(payload["blockers"])


def test_pre_tool_hook_denies_powershell_and_heredoc_cross_worktree_writes(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    cases = [
        {"tool_name": "PowerShell", "tool_input": {"command": "Set-Content worktrees/C999/out.txt x"}},
        {"tool_name": "PowerShell", "tool_input": {"command": "New-Item worktrees/C999/out.txt -ItemType File"}},
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python - <<PY\nopen('worktrees/C999/out.txt','w').write('x')\nPY"},
        },
    ]
    for payload in cases:
        payload["session"] = "S1"
        code, result = pre_tool_hook(tmp_path, payload)
        assert code == 2
        assert result["decision"] == "deny"
        assert "outside active claim allowed paths" in "; ".join(result["blockers"])


def test_pre_tool_hook_allows_shared_artifact_and_log_roots(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    for path in ("artifacts/R001/metrics.json", "logs/R001.log"):
        code, payload = pre_tool_hook(
            tmp_path,
            {
                "session": "S1",
                "tool_name": "Write",
                "tool_input": {"path": path},
            },
        )
        assert code == 0
        assert payload["decision"] == "allow"

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo '{}' > artifacts/R001/metrics.json"},
        },
    )
    assert code == 0
    assert payload["decision"] == "allow"


def test_pre_tool_hook_denies_worktree_metadata_edits_and_tampered_roots(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    for payload in (
        {"session": "S1", "tool_name": "Write", "tool_input": {"path": "worktrees/C001/worktree.yaml"}},
        {"session": "S1", "tool_name": "PowerShell", "tool_input": {"command": "Set-Content worktrees/C001/worktree.yaml x"}},
    ):
        code, result = pre_tool_hook(tmp_path, payload)
        assert code == 2
        assert "cannot modify worktree metadata through tool hooks" in "; ".join(result["blockers"])

    metadata = tmp_path / "worktrees" / "C001" / "worktree.yaml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace("shared_artifact_roots:", "  - worktrees/C999\nshared_artifact_roots:"),
        encoding="utf-8",
    )
    code, result = pre_tool_hook(
        tmp_path,
        {"session": "S1", "tool_name": "Write", "tool_input": {"path": "worktrees/C999/out.txt"}},
    )
    assert code == 2
    blockers = "; ".join(result["blockers"])
    assert "worktree metadata declares unexpected allowed_write_root: worktrees/C999" in blockers
    assert "outside active claim allowed paths" in blockers


def test_pre_tool_hook_denies_mismatched_worktree_metadata_after_attach(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)
    metadata = tmp_path / "worktrees" / "C001" / "worktree.yaml"
    metadata.write_text(metadata.read_text(encoding="utf-8").replace("claim_id: C001", "claim_id: C999"), encoding="utf-8")

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Write",
            "tool_input": {"path": "worktrees/C001/train.py"},
        },
    )
    assert code == 2
    assert "worktree metadata claim_id does not match" in "; ".join(payload["blockers"])


def test_pre_tool_hook_fails_closed_for_malformed_worktree_metadata(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)
    metadata = tmp_path / "worktrees" / "C001" / "worktree.yaml"
    metadata.write_text(metadata.read_text(encoding="utf-8").replace("allowed_write_roots:", "allowed_write_roots_missing:"), encoding="utf-8")

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Write",
            "tool_input": {"path": "artifacts/R001/metrics.json"},
        },
    )
    assert code == 2
    assert "worktree metadata missing allowed_write_roots" in "; ".join(payload["blockers"])


def test_pre_tool_hook_paper_write_policy(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    code, payload = pre_tool_hook(
        tmp_path,
        {"session": "S1", "tool_name": "Edit", "tool_input": {"path": ".aris/paper/CLAIM_MATRIX.yaml"}},
    )
    assert code == 2
    assert "claim session cannot write paper file at this stage" in "; ".join(payload["blockers"])

    with connect(tmp_path) as conn:
        conn.execute("UPDATE claims SET status = 'supported', stage = 'judging', verdict = 'supported' WHERE id = 'C001'")
    code, payload = pre_tool_hook(
        tmp_path,
        {"session": "S1", "tool_name": "Edit", "tool_input": {"path": ".aris/paper/CLAIM_MATRIX.yaml"}},
    )
    assert code == 0
    assert payload["decision"] == "allow"

    code, payload = pre_tool_hook(
        tmp_path,
        {"session": "S1", "tool_name": "Edit", "tool_input": {"path": ".aris/paper/merged_claims.yaml"}},
    )
    assert code == 2
    assert "merged_claims.yaml is maintained by researchctl claim merge" in "; ".join(payload["blockers"])


def test_pre_tool_hook_denies_paper_reader_unmerged_claim_object(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    claim = create_claim("A001", "Claim", root_arg=tmp_path)
    attach_session("S1", "C001", role="paper-build", root_arg=tmp_path)

    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Read",
            "tool_input": {"path": f"{claim['object_path']}/EVIDENCE.md"},
        },
    )
    assert code == 2
    assert "paper reader cannot consume unmerged claim object" in "; ".join(payload["blockers"])


def test_pre_tool_hook_denies_draft_run_and_contract_edits_after_freeze(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    claim = create_claim("A001", "Claim", root_arg=tmp_path)
    claim_dir = tmp_path / claim["object_path"]
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    code, payload = pre_tool_hook(
        tmp_path,
        {"session": "S1", "researchctl_action": "run_start", "tool_input": {"command": "python train.py"}},
    )
    assert code == 2
    assert "draft claim cannot start a run" in payload["blockers"]

    gate_claim("C001", "approve", root_arg=tmp_path)
    write_valid_contract(claim_dir)
    freeze_contract("C001", root_arg=tmp_path)
    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Edit",
            "tool_input": {"path": ".aris/anchors/A001_anchor/CLAIMS/C001_claim/CONTRACT.yaml"},
        },
    )
    assert code == 2
    assert "cannot modify CONTRACT.yaml" in "; ".join(payload["blockers"])

    with connect(tmp_path) as conn:
        conn.execute("UPDATE claims SET status = 'supported', stage = 'judging', verdict = 'supported' WHERE id = 'C001'")
    code, payload = pre_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "tool_name": "Edit",
            "tool_input": {"path": ".aris/anchors/A001_anchor/CLAIMS/C001_claim/CONTRACT.yaml"},
        },
    )
    assert code == 2
    assert "cannot modify CONTRACT.yaml" in "; ".join(payload["blockers"])


def test_post_tool_hook_registers_run_and_artifact_events(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    claim = create_claim("A001", "Claim", root_arg=tmp_path)
    claim_dir = tmp_path / claim["object_path"]
    gate_claim("C001", "approve", root_arg=tmp_path)
    write_valid_contract(claim_dir)
    freeze_contract("C001", root_arg=tmp_path)
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)

    code, payload = post_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "researchctl": {
                "register_run": True,
                "run_command": "python train.py --seed 1",
                "artifact_path": ".aris/anchors/A001_anchor/CLAIMS/C001_claim/RUNS/R001",
                "metrics_path": ".aris/anchors/A001_anchor/CLAIMS/C001_claim/RUNS/R001/metrics.json",
            },
        },
    )
    assert code == 0
    assert payload["events"][0]["type"] == "run_started"

    with connect(tmp_path) as conn:
        run = one(conn, "SELECT * FROM runs WHERE id = 'R001'")
        assert run is not None
        metrics = one(conn, "SELECT * FROM events WHERE entity_id = 'R001' AND event_type = 'metrics_recorded'")
        assert metrics is not None


def test_stop_hook_is_idempotent_for_finished_claim(tmp_path: Path) -> None:
    create_anchor("Anchor", root_arg=tmp_path)
    claim = create_claim("A001", "Claim", root_arg=tmp_path)
    claim_dir = tmp_path / claim["object_path"]
    gate_claim("C001", "approve", root_arg=tmp_path)
    write_valid_contract(claim_dir)
    freeze_contract("C001", root_arg=tmp_path)
    post_tool_hook(tmp_path, {"session": "S1"}, "S1")
    attach_session("S1", "C001", role="builder", root_arg=tmp_path)
    post_tool_hook(
        tmp_path,
        {"session": "S1", "researchctl": {"register_run": True, "run_command": "python train.py"}},
    )
    post_tool_hook(
        tmp_path,
        {
            "session": "S1",
            "researchctl": {
                "run_id": "R001",
                "metrics_path": ".aris/anchors/A001_anchor/CLAIMS/C001_claim/RUNS/R001/metrics.json",
            },
        },
    )
    with connect(tmp_path) as conn:
        conn.execute("UPDATE runs SET status = 'success', finished_at = 'now' WHERE id = 'R001'")
    run_dir = claim_dir / "RUNS" / "R001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text('{"tail_acc": 1.0}\n', encoding="utf-8")
    (run_dir / "fig1.png").write_bytes(b"figure")
    (claim_dir / "EVIDENCE.md").write_text("Evidence references R001 and enough metrics to decide.\n", encoding="utf-8")
    with connect(tmp_path) as conn:
        conn.execute(
            "UPDATE claims SET status = 'supported', stage = 'judging', verdict = 'supported', paper_merge_status = 'eligible' WHERE id = 'C001'"
    )
    write_valid_verdict(claim_dir)
    (tmp_path / ".aris" / "paper" / "CLAIM_MATRIX.yaml").write_text(
        """claims:
  - id: C001
    text: "Claim is supported in smoke"
    verdict: supported
    support_runs: [R001]
    figure_table_refs: [fig1]
    allowed_scope: narrow
""",
        encoding="utf-8",
    )

    first_code, first = stop_hook(tmp_path, {"session": "S1"})
    second_code, second = stop_hook(tmp_path, {"session": "S1"})
    assert first_code == 0
    assert first["ok"] is True
    assert second_code == 0
    assert second["idempotent"] is True
