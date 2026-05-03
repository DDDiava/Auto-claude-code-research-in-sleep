from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path


DIR_ARIS = ".aris"
DIR_ANCHORS = "anchors"
DIR_EVENTS = "events"
DIR_MEMORY = "memory"
DIR_PAPER = "paper"
DIR_RUNTIME = ".runtime"
DIR_SESSIONS = "sessions"
DIR_LOCKS = "locks"
FILE_STATE_DB = "state.db"


DEFAULT_WORKFLOW = """# Research Workflow

## Core Principles

1. Evidence loop first.
2. Claim PR is the review unit.
3. State lives outside the model.
4. This file is the workflow constitution.

## Phase Index

```text
Phase 0: no_claim
Phase 1: draft
Phase 2: gated
Phase 3: contract_frozen
Phase 4: running
Phase 5: judging
Phase 6: merged
Phase 7: archived
```

[workflow-state:no_claim]
No active claim is attached to this session.
Default next action: create or attach a claim with `python -m researchctl claim create` and `python -m researchctl session attach`.
Do not edit paper or research code directly without an active claim.
[/workflow-state:no_claim]

[workflow-state:draft]
The active claim is still a draft.
Default next action: refine CLAIM.md, run novelty review, then gate the claim.
Do not start runs before the claim is gated and CONTRACT.yaml is frozen.
[/workflow-state:draft]

[workflow-state:gated]
The active claim is approved but not executing yet.
Default next action: freeze CONTRACT.yaml with `python -m researchctl claim freeze-contract`.
Do not change success or failure signals after the contract hash is recorded.
[/workflow-state:gated]

[workflow-state:running]
The active claim is executing.
Default next action: monitor registered runs, collect artifacts, and update EVIDENCE.md.
Do not rewrite the claim or silently modify CONTRACT.yaml.
[/workflow-state:running]

[workflow-state:judging]
The active claim has enough evidence for a verdict.
Default next action: run claim audit, write VERDICT.yaml, then call `python -m researchctl claim finish`.
Do not write manuscript narrative before verdict.
[/workflow-state:judging]

[workflow-state:merged]
The active claim is merged into the paper input set.
Default next action: update CLAIM_MATRIX.yaml and paper build artifacts from merged claims only.
Do not let invalidated, inconclusive, killed, draft, or running claims enter the manuscript.
[/workflow-state:merged]

[workflow-state:archived]
The active claim is archived.
Default next action: preserve failure memory or handoff notes, then detach the session.
Do not revive archived claims without explicitly creating a new claim or follow-up claim.
[/workflow-state:archived]
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root(root: str | os.PathLike[str] | None = None) -> Path:
    return Path(root or os.getcwd()).resolve()


def aris_dir(root: Path) -> Path:
    return root / DIR_ARIS


def state_db_path(root: Path) -> Path:
    return aris_dir(root) / FILE_STATE_DB


def events_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_EVENTS


def runtime_sessions_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_RUNTIME / DIR_SESSIONS


def runtime_locks_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_RUNTIME / DIR_LOCKS


def anchors_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_ANCHORS


def paper_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_PAPER


def memory_dir(root: Path) -> Path:
    return aris_dir(root) / DIR_MEMORY


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def ensure_project_dirs(root: Path) -> None:
    for path in (
        aris_dir(root),
        anchors_dir(root),
        events_dir(root),
        runtime_sessions_dir(root),
        runtime_locks_dir(root),
        memory_dir(root),
        paper_dir(root),
        root / "worktrees",
    ):
        path.mkdir(parents=True, exist_ok=True)

    config = aris_dir(root) / "config.yaml"
    if not config.exists():
        config.write_text(
            "\n".join(
                [
                    "version: 1",
                    "control_plane: claim-pr",
                    "",
                    "state:",
                    "  database: .aris/state.db",
                    "  events: .aris/events",
                    "  runtime_sessions: .aris/.runtime/sessions",
                    "",
                    "policy:",
                    "  active_claim_scope: session",
                    "  require_contract_freeze_before_run: true",
                    "  require_verdict_before_merge: true",
                    "  paper_build_reads_merged_claims_only: true",
                    "",
                    "worktree_policy:",
                    "  default_root: worktrees",
                    "  single_write_session_per_worktree: true",
                    "  shared_artifact_roots:",
                    "    - artifacts",
                    "    - logs",
                    "  paper_read_policy: merged_claims_only",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    _write_if_missing(aris_dir(root) / "workflow.md", DEFAULT_WORKFLOW)
    _write_if_missing(paper_dir(root) / "merged_claims.yaml", "claims:\n")
    _write_if_missing(paper_dir(root) / "CLAIM_MATRIX.yaml", "claims:\n")
    _write_if_missing(paper_dir(root) / "CITATION_LEDGER.json", '{\n  "citations": []\n}\n')
    _write_if_missing(
        paper_dir(root) / "SUBMISSION_CHECKLIST.json",
        "{\n"
        '  "repro_manifest": {\n'
        '    "code": "",\n'
        '    "data": "",\n'
        '    "hardware": "",\n'
        '    "seed_policy": ""\n'
        "  },\n"
        '  "authorship_ai_disclosure": "",\n'
        '  "venue_format": {\n'
        '    "venue": "",\n'
        '    "verified": false\n'
        "  },\n"
        '  "external_artifacts_checked": false\n'
        "}\n",
    )

    gitignore = aris_dir(root) / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "\n".join(
                [
                    "state.db",
                    "events/",
                    ".runtime/",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "item"


def object_path(root: Path, path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    return path if path.is_absolute() else root / path
