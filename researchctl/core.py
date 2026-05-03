from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .db import connect, next_id, one
from .events import record_event
from .locking import file_lock
from .paths import (
    anchors_dir,
    memory_dir,
    object_path,
    paper_dir,
    repo_root,
    runtime_sessions_dir,
    slugify,
    utc_now,
)


MERGEABLE_VERDICTS = {"supported", "partial_supported"}
FAILURE_VERDICTS = {"invalidated", "inconclusive", "killed"}
FINAL_VERDICTS = MERGEABLE_VERDICTS | FAILURE_VERDICTS
TERMINAL_RUN_STATUSES = {"success", "failed", "crashed"}
READ_ONLY_SESSION_ROLES = {"scout", "judge", "reviewer", "reader", "paper-build", "paper_builder"}
PASSING_REVIEW_DECISIONS = {"supported", "partial_supported"}
BLOCKING_REVIEW_DECISIONS = {"reject", "revise", "invalidated", "inconclusive"}
NON_DECISIVE_REVIEW_DECISIONS = {"comment"}
REVIEW_DECISIONS = PASSING_REVIEW_DECISIONS | BLOCKING_REVIEW_DECISIONS | NON_DECISIVE_REVIEW_DECISIONS


class ResearchCtlError(RuntimeError):
    pass


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _current_branch(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        branch = result.stdout.strip()
        return branch or "main"
    except OSError:
        return "main"


def _write_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _has_key(text: str, key: str) -> bool:
    return re.search(rf"(?m)^\s*{re.escape(key)}\s*:", text) is not None


def _section_has_list_item(text: str, section: str) -> bool:
    match = re.search(rf"(?ms)^\s*{re.escape(section)}\s*:\s*\n(?P<body>.*?)(?=^\S|\Z)", text)
    return bool(match and re.search(r"(?m)^\s*-\s+", match.group("body")))


def _is_meaningful_value(value: str | None) -> bool:
    if value is None:
        return False
    cleaned = value.strip().strip('"').strip("'")
    lowered = cleaned.lower()
    return bool(cleaned) and lowered not in {"todo", "tbd", "null", "none", "[]", "{}", "...", "pending"} and not lowered.startswith(
        ("todo:", "tbd:", "replace ")
    )


def _has_meaningful_scalar(text: str, *keys: str) -> bool:
    return any(_is_meaningful_value(_scalar_value(text, key)) for key in keys)


def _section_has_meaningful_list_item(text: str, section: str) -> bool:
    match = re.search(rf"(?ms)^\s*{re.escape(section)}\s*:\s*\n(?P<body>.*?)(?=^\S|\Z)", text)
    if not match:
        return False
    body = match.group("body")
    if not re.search(r"(?m)^\s*-\s+", body):
        return False
    text_values = re.findall(r"(?m)^\s*text\s*:\s*(.*?)\s*$", body)
    if text_values:
        return any(_is_meaningful_value(value) for value in text_values)
    item_values = re.findall(r"(?m)^\s*-\s*(.*?)\s*$", body)
    return any(_is_meaningful_value(value) and not value.strip().lower().startswith("id:") for value in item_values)


def _scalar_value(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", text)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _parse_inline_list(value: str | None) -> list[str]:
    if value is None:
        return []
    raw = value.strip()
    if not raw or raw in {"[]", "{}"}:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]
    return [raw.strip('"').strip("'")]


def _format_inline_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def _section_values(text: str, section: str) -> list[str]:
    lines = text.splitlines()
    values: list[str] = []
    in_section = False
    section_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if not in_section:
            match = re.match(rf"^\s*{re.escape(section)}\s*:\s*(.*?)\s*$", line)
            if match:
                values.extend(_parse_inline_list(match.group(1)))
                in_section = True
                section_indent = indent
            continue
        if indent <= section_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip('"').strip("'"))
        elif stripped.startswith("text:"):
            values.append(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
    return values


def _parse_yamlish_list_maps(path: Path, section: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_key: str | None = None
    in_section = False
    section_indent = 0
    item_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if not in_section:
            match = re.match(rf"^\s*{re.escape(section)}\s*:\s*(.*?)\s*$", line)
            if match:
                in_section = True
                section_indent = indent
            continue
        if indent <= section_indent and not stripped.startswith("-"):
            break
        if current is not None and indent > item_indent and current_key and stripped.startswith("- "):
            current.setdefault(current_key, []).append(stripped[2:].strip().strip('"').strip("'"))
            continue
        if stripped.startswith("- "):
            item_indent = indent
            value = stripped[2:].strip()
            if current is not None:
                entries.append(current)
            current = {}
            current_key = None
            if ":" in value:
                key, raw = value.split(":", 1)
                raw = raw.strip()
                current[key.strip()] = _parse_inline_list(raw) if raw.startswith("[") else raw.strip('"').strip("'")
                current_key = key.strip() if not raw else None
            elif value:
                current["value"] = value.strip('"').strip("'")
            continue
        if current is None or indent <= item_indent:
            continue
        if ":" in stripped:
            key, raw = stripped.split(":", 1)
            key = key.strip()
            raw = raw.strip()
            current[key] = _parse_inline_list(raw) if raw.startswith("[") else raw.strip('"').strip("'")
            current_key = key if not raw else None
    if current is not None:
        entries.append(current)
    return entries


def _section_has_meaningful_values(text: str, section: str) -> bool:
    return any(_is_meaningful_value(value) for value in _section_values(text, section))


def _nested_section_values(text: str, parent: str, child: str) -> list[str]:
    parent_match = re.search(rf"(?ms)^\s*{re.escape(parent)}\s*:\s*\n(?P<body>.*?)(?=^\S|\Z)", text)
    if not parent_match:
        return []
    return _section_values(parent_match.group("body"), child)


def _nested_section_has_meaningful_values(text: str, parent: str, child: str) -> bool:
    return any(_is_meaningful_value(value) for value in _nested_section_values(text, parent, child))


def validate_contract_file(path: Path) -> list[str]:
    text = _read_text(path)
    blockers: list[str] = []
    if not text:
        return ["CONTRACT.yaml is missing"]
    for key in ("hypothesis", "scope", "budget"):
        if not _has_key(text, key):
            blockers.append(f"CONTRACT.yaml missing {key}")
    if not (_has_key(text, "baseline_commit") or _has_key(text, "baseline")):
        blockers.append("CONTRACT.yaml missing baseline")
    elif not _has_meaningful_scalar(text, "baseline_commit", "baseline"):
        blockers.append("CONTRACT.yaml empty baseline")
    if not (_has_key(text, "primary_metric") or _has_key(text, "metric")):
        blockers.append("CONTRACT.yaml missing primary metric")
    elif not _has_meaningful_scalar(text, "primary_metric", "metric"):
        blockers.append("CONTRACT.yaml empty primary metric")
    for section in ("success_signals", "failure_signals"):
        if not _section_has_meaningful_list_item(text, section):
            blockers.append(f"CONTRACT.yaml missing {section}")
    if not (_section_has_meaningful_list_item(text, "stop_rules") or _section_has_meaningful_list_item(text, "reporting_rules")):
        blockers.append("CONTRACT.yaml missing stop_rules or reporting_rules")
    return blockers


def validate_verdict_file(path: Path, expected_claim_id: str, expected_verdict: str | None) -> list[str]:
    text = _read_text(path)
    blockers: list[str] = []
    if not text:
        return ["VERDICT.yaml is missing"]
    if _scalar_value(text, "claim_id") != expected_claim_id:
        blockers.append("VERDICT.yaml claim_id does not match")
    if not expected_verdict or _scalar_value(text, "verdict") != expected_verdict:
        blockers.append("VERDICT.yaml verdict does not match the recorded verdict")
    if not _has_meaningful_scalar(text, "confidence"):
        blockers.append("VERDICT.yaml missing confidence")
    if not _has_key(text, "risk_notes"):
        blockers.append("VERDICT.yaml missing risk_notes")
    elif not _section_has_meaningful_values(text, "risk_notes"):
        blockers.append("VERDICT.yaml empty risk_notes")
    if not _has_key(text, "allowed_scope"):
        blockers.append("VERDICT.yaml missing allowed_scope")
    else:
        has_allowed_narrative = _nested_section_has_meaningful_values(text, "allowed_scope", "abstract") or _nested_section_has_meaningful_values(
            text, "allowed_scope", "conclusion"
        )
        if not has_allowed_narrative:
            blockers.append("VERDICT.yaml empty allowed_scope abstract/conclusion")
        if expected_verdict in MERGEABLE_VERDICTS and not _nested_section_has_meaningful_values(text, "allowed_scope", "forbidden"):
            blockers.append("VERDICT.yaml missing allowed_scope forbidden")
    if not _has_key(text, "next_action"):
        blockers.append("VERDICT.yaml missing next_action")
    elif not _section_has_meaningful_values(text, "next_action"):
        blockers.append("VERDICT.yaml empty next_action")
    return blockers


def _worktree_metadata_text(root: Path, claim_row: Any) -> str:
    allowed_roots = [claim_row["worktree_path"], claim_row["object_path"], "artifacts", "logs"]
    return "\n".join(
        [
            "version: 1",
            f"claim_id: {claim_row['id']}",
            f"branch: {claim_row['branch'] or ''}",
            f"base_branch: {claim_row['base_branch'] or ''}",
            "allowed_write_roots:",
            *[f"  - {root_path}" for root_path in allowed_roots if root_path],
            "shared_artifact_roots:",
            "  - artifacts",
            "  - logs",
            "",
        ]
    )


def _expected_worktree_allowed_paths(claim_row: Any) -> list[str]:
    expected = [claim_row["worktree_path"], claim_row["object_path"], "artifacts", "logs"]
    return [path for path in dict.fromkeys(expected) if path]


def _validate_worktree_manifest_text(text: str, claim_row: Any) -> list[str]:
    blockers: list[str] = []
    if _scalar_value(text, "claim_id") != claim_row["id"]:
        blockers.append("worktree metadata claim_id does not match")
    expected_roots = set(_expected_worktree_allowed_paths(claim_row))
    allowed_roots = set(_section_values(text, "allowed_write_roots"))
    shared_roots = set(_section_values(text, "shared_artifact_roots"))
    if not allowed_roots:
        blockers.append("worktree metadata missing allowed_write_roots")
    else:
        for root_path in sorted(expected_roots - allowed_roots):
            blockers.append(f"worktree metadata missing expected allowed_write_root: {root_path}")
        for root_path in sorted(allowed_roots - expected_roots):
            blockers.append(f"worktree metadata declares unexpected allowed_write_root: {root_path}")
    expected_shared = {"artifacts", "logs"}
    if not shared_roots:
        blockers.append("worktree metadata missing shared_artifact_roots")
    else:
        for root_path in sorted(expected_shared - shared_roots):
            blockers.append(f"worktree metadata missing expected shared_artifact_root: {root_path}")
        for root_path in sorted(shared_roots - expected_shared):
            blockers.append(f"worktree metadata declares unexpected shared_artifact_root: {root_path}")
    return blockers


def _write_worktree_metadata(root: Path, claim_row: Any) -> None:
    if not claim_row["worktree_path"]:
        return
    worktree_dir = root / claim_row["worktree_path"]
    worktree_dir.mkdir(parents=True, exist_ok=True)
    (worktree_dir / "worktree.yaml").write_text(_worktree_metadata_text(root, claim_row), encoding="utf-8")


def _worktree_metadata_allowed_paths(root: Path, claim_row: Any) -> tuple[list[str], list[str]]:
    allowed_paths = _expected_worktree_allowed_paths(claim_row)
    blockers: list[str] = []
    if not claim_row["worktree_path"]:
        return list(dict.fromkeys(allowed_paths)), blockers
    metadata = root / claim_row["worktree_path"] / "worktree.yaml"
    if not metadata.is_file():
        return list(dict.fromkeys(allowed_paths)), blockers
    text = metadata.read_text(encoding="utf-8", errors="replace")
    blockers.extend(_validate_worktree_manifest_text(text, claim_row))
    if "worktree metadata claim_id does not match" in blockers:
        return list(dict.fromkeys(allowed_paths)), blockers
    return [path for path in dict.fromkeys(allowed_paths) if path], blockers


def _validate_worktree_metadata(root: Path, claim_row: Any, worktree_path: str | None) -> list[str]:
    blockers: list[str] = []
    if not worktree_path:
        blockers.append("worktree path is missing")
        return blockers
    if worktree_path != claim_row["worktree_path"]:
        blockers.append(f"worktree does not match claim worktree: {worktree_path} != {claim_row['worktree_path']}")
    metadata = root / worktree_path / "worktree.yaml"
    if not metadata.is_file():
        blockers.append(f"worktree metadata is missing: {worktree_path}/worktree.yaml")
        return blockers
    text = metadata.read_text(encoding="utf-8", errors="replace")
    blockers.extend(_validate_worktree_manifest_text(text, claim_row))
    return blockers


def _event_exists(conn, entity_type: str, entity_id: str, event_type: str) -> bool:
    return bool(
        one(
            conn,
            "SELECT id FROM events WHERE entity_type = ? AND entity_id = ? AND event_type = ? LIMIT 1",
            (entity_type, entity_id, event_type),
        )
    )


def _role_is_write(role: str) -> bool:
    return role.replace("-", "_").lower() not in READ_ONLY_SESSION_ROLES


def _budget_remaining_from_claim(claim_row: Any) -> dict[str, float]:
    budget = _json_loads(claim_row["budget_json"], {})
    return {
        "gpu_hours": float(budget.get("gpu_hours_limit", 0)) - float(budget.get("gpu_hours_used", 0)),
        "token_usd": float(budget.get("token_usd_limit", 0)) - float(budget.get("token_usd_used", 0)),
    }


def _budget_exhausted(claim_row: Any) -> bool:
    remaining = _budget_remaining_from_claim(claim_row)
    return remaining["gpu_hours"] <= 0 or remaining["token_usd"] <= 0


def _claim_payload(row: Any) -> dict[str, Any]:
    meta = _json_loads(row["meta_json"], {})
    return {
        "id": row["id"],
        "title": row["title"],
        "anchor_id": row["anchor_id"],
        "creator": meta.get("creator", "human"),
        "assignee": meta.get("assignee", "builder"),
        "status": row["status"],
        "stage": row["stage"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "branch": row["branch"],
        "base_branch": row["base_branch"],
        "worktree_path": row["worktree_path"],
        "contract_hash": row["contract_hash"],
        "run_ids": _json_loads(row["run_ids_json"], []),
        "verdict": row["verdict"],
        "paper_merge_status": row["paper_merge_status"],
        "budget": _json_loads(row["budget_json"], {}),
        "next_action": meta.get("next_action", _default_next_action(row["status"], row["stage"])),
        "meta": meta,
    }


def _default_next_action(status: str, stage: str) -> list[dict[str, str | int]]:
    if status == "draft":
        return [{"phase": 1, "action": "claim-gate"}]
    if status == "gated" or stage == "contract_frozen":
        return [{"phase": 2, "action": "claim-run"}]
    if status == "running":
        return [{"phase": 4, "action": "claim-verdict"}]
    if status in FINAL_VERDICTS:
        return [{"phase": 5, "action": "claim-merge" if status in MERGEABLE_VERDICTS else "claim-archive"}]
    return [{"phase": 0, "action": "inspect"}]


def _sync_claim_json(root: Path, row: Any) -> None:
    claim_dir = object_path(root, row["object_path"])
    if claim_dir is None:
        return
    (claim_dir / "claim.json").write_text(
        json.dumps(_claim_payload(row), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _get_anchor(conn, anchor_id: str):
    row = one(conn, "SELECT * FROM anchors WHERE id = ?", (anchor_id,))
    if not row:
        raise ResearchCtlError(f"anchor not found: {anchor_id}")
    return row


def _get_claim(conn, claim_id: str):
    row = one(conn, "SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not row:
        raise ResearchCtlError(f"claim not found: {claim_id}")
    return row


def _get_run(conn, run_id: str):
    row = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise ResearchCtlError(f"run not found: {run_id}")
    return row


def _update_claim(root: Path, conn, claim_id: str, **fields: Any):
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE claims SET {assignments} WHERE id = ?",
        (*fields.values(), claim_id),
    )
    row = _get_claim(conn, claim_id)
    _sync_claim_json(root, row)
    return row


def init_project(root_arg: str | os.PathLike[str] | None = None) -> dict[str, str]:
    root = repo_root(root_arg)
    with connect(root):
        pass
    return {"root": str(root), "aris_dir": str(root / ".aris")}


def create_anchor(title: str, slug: str | None = None, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        anchor_id = next_id(conn, "anchors", "A")
        anchor_slug = slug or slugify(title)
        anchor_dir = anchors_dir(root) / f"{anchor_id}_{anchor_slug}"
        now = utc_now()
        _write_if_missing(
            anchor_dir / "ANCHOR.md",
            f"# Anchor: {anchor_id} {title}\n\n## Problem\n\nTODO\n\n## Baseline Setting\n\n- Repo:\n- Dataset:\n- Primary metric:\n\n## Constraints\n\n- TODO\n",
        )
        _write_if_missing(
            anchor_dir / "BASELINE.md",
            "# Baseline\n\n## Source\n\n- Paper:\n- Repo:\n- Commit:\n\n## Reproduction Status\n\n- [ ] Env passes\n- [ ] Data verified\n- [ ] Metrics reproduced\n",
        )
        _write_if_missing(anchor_dir / "claim-batch.md", "# Claim Batch\n\n- TODO\n")
        (anchor_dir / "CLAIMS").mkdir(parents=True, exist_ok=True)
        conn.execute(
            "INSERT INTO anchors(id, slug, title, status, object_path, created_at, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (anchor_id, anchor_slug, title, "active", _rel(root, anchor_dir), now, "{}"),
        )
        record_event(conn, root, "anchor", anchor_id, "anchor_created", {"title": title})
        return dict(_get_anchor(conn, anchor_id))


def list_anchors(root_arg: str | os.PathLike[str] | None = None) -> list[dict]:
    root = repo_root(root_arg)
    with connect(root) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM anchors ORDER BY id")]


def archive_anchor(anchor_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        _get_anchor(conn, anchor_id)
        conn.execute("UPDATE anchors SET status = ? WHERE id = ?", ("archived", anchor_id))
        record_event(conn, root, "anchor", anchor_id, "anchor_archived", {})
        return dict(_get_anchor(conn, anchor_id))


def create_claim(
    anchor_id: str,
    title: str,
    slug: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _create_claim_unlocked(anchor_id, title, slug, root)


def _create_claim_unlocked(
    anchor_id: str,
    title: str,
    slug: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        anchor = _get_anchor(conn, anchor_id)
        claim_id = next_id(conn, "claims", "C")
        claim_slug = slug or slugify(title)
        anchor_path = object_path(root, anchor["object_path"])
        if anchor_path is None:
            raise ResearchCtlError(f"anchor path missing: {anchor_id}")
        claim_dir = anchor_path / "CLAIMS" / f"{claim_id}_{claim_slug}"
        now = utc_now()
        base_branch = _current_branch(root)
        branch = f"claim/{claim_id.lower()}-{claim_slug}"
        worktree_path = f"worktrees/{claim_id}"
        budget = {"gpu_hours_limit": 20.0, "gpu_hours_used": 0.0, "token_usd_limit": 30.0, "token_usd_used": 0.0}
        meta = {
            "creator": "human",
            "assignee": "builder",
            "next_action": [
                {"phase": 1, "action": "claim-gate"},
                {"phase": 2, "action": "contract-freeze"},
                {"phase": 3, "action": "claim-run"},
                {"phase": 4, "action": "claim-verdict"},
                {"phase": 5, "action": "claim-merge"},
            ],
        }
        claim_dir.mkdir(parents=True, exist_ok=True)
        worktree_dir = root / worktree_path
        worktree_dir.mkdir(parents=True, exist_ok=True)
        _write_if_missing(
            claim_dir / "CLAIM.md",
            f"# Claim: {claim_id} {title}\n\n## Claim Text\n\nTODO\n\n## Delta over baseline\n\n- TODO\n\n## Not claiming\n\n- TODO\n",
        )
        _write_if_missing(
            claim_dir / "NOVELTY.md",
            "# Novelty Review\n\n## Closest Prior Work\n\n1.\n\n## Risk Rating\n\n- novelty_risk: medium\n\n## Gate Recommendation\n\n- [ ] proceed\n- [ ] revise claim\n- [ ] kill\n",
        )
        _write_if_missing(
            claim_dir / "CONTRACT.yaml",
            f"version: 1\nclaim_id: {claim_id}\n\nhypothesis:\n  text: \"{title}\"\n\nscope:\n  baseline_commit: \"\"\n  datasets: []\n  primary_metric: \"\"\n  fixed_protocol: []\n\nsuccess_signals:\n  - id: S1\n    text: \"TODO\"\n\nfailure_signals:\n  - id: F1\n    text: \"TODO\"\n\nbudget:\n  gpu_hours_limit: 20\n  token_usd_limit: 30\n\nstop_rules:\n  - \"Stop if pilot evidence contradicts the primary signal.\"\n\nreporting_rules:\n  - \"Do not modify success/failure signals after observing results.\"\n",
        )
        _write_if_missing(claim_dir / "PLAN.md", "# Experiment Plan\n\n## Order\n\n1. baseline check\n2. pilot run\n3. verdict\n")
        _write_if_missing(claim_dir / "experiment.jsonl", "")
        _write_if_missing(claim_dir / "judge.jsonl", "")
        _write_if_missing(claim_dir / "writing.jsonl", "")
        _write_if_missing(claim_dir / "EVIDENCE.md", "# Evidence Summary\n\n## Runs Included\n\n\n## Main Metrics\n\nTODO\n")
        _write_if_missing(
            claim_dir / "VERDICT.yaml",
            f"claim_id: {claim_id}\nverdict: null\nconfidence: null\nrisk_notes: []\nallowed_scope:\n  abstract: []\n  conclusion: []\n  forbidden: []\nnext_action: []\n",
        )
        (claim_dir / "RUNS").mkdir(exist_ok=True)
        conn.execute(
            """
            INSERT INTO claims(
              id, anchor_id, title, status, stage, object_path, branch, base_branch,
              worktree_path, contract_hash, run_ids_json, verdict, paper_merge_status,
              budget_json, created_at, updated_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                anchor_id,
                title,
                "draft",
                "planning",
                _rel(root, claim_dir),
                branch,
                base_branch,
                worktree_path,
                None,
                "[]",
                None,
                "not_eligible",
                json.dumps(budget, sort_keys=True),
                now,
                now,
                json.dumps(meta, sort_keys=True),
            ),
        )
        row = _get_claim(conn, claim_id)
        _sync_claim_json(root, row)
        _write_worktree_metadata(root, row)
        record_event(conn, root, "claim", claim_id, "claim_created", {"anchor_id": anchor_id, "title": title})
        return _claim_payload(row) | {"object_path": row["object_path"]}


def list_claims(status: str | None = None, root_arg: str | os.PathLike[str] | None = None) -> list[dict]:
    root = repo_root(root_arg)
    with connect(root) as conn:
        if status:
            rows = conn.execute("SELECT * FROM claims WHERE status = ? ORDER BY id", (status,))
        else:
            rows = conn.execute("SELECT * FROM claims ORDER BY id")
        return [_claim_payload(row) | {"object_path": row["object_path"]} for row in rows]


def show_claim(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        row = _get_claim(conn, claim_id)
        return _claim_payload(row) | {"object_path": row["object_path"]}


def gate_claim(claim_id: str, decision: str, by: str = "human", root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _gate_claim_unlocked(claim_id, decision, by, root)


def _gate_claim_unlocked(claim_id: str, decision: str, by: str = "human", root_arg: str | os.PathLike[str] | None = None) -> dict:
    if decision not in {"approve", "reject"}:
        raise ResearchCtlError("decision must be approve or reject")
    root = repo_root(root_arg)
    with connect(root) as conn:
        _get_claim(conn, claim_id)
        if decision == "approve":
            row = _update_claim(root, conn, claim_id, status="gated", stage="gated")
            event = "claim_gate_approved"
        else:
            _append_failure_memory(root, claim_id, "killed", "claim gate rejected")
            row = _update_claim(root, conn, claim_id, status="killed", stage="archived", verdict="killed", paper_merge_status="not_eligible")
            event = "claim_gate_rejected"
        record_event(conn, root, "claim", claim_id, event, {"by": by})
        return _claim_payload(row)


def contract_hash(root: Path, claim_row: Any) -> str:
    claim_dir = object_path(root, claim_row["object_path"])
    if claim_dir is None:
        raise ResearchCtlError(f"claim path missing: {claim_row['id']}")
    path = claim_dir / "CONTRACT.yaml"
    if not path.is_file():
        raise ResearchCtlError(f"CONTRACT.yaml missing for {claim_row['id']}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def freeze_contract(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _freeze_contract_unlocked(claim_id, root)


def _freeze_contract_unlocked(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        row = _get_claim(conn, claim_id)
        claim_dir = object_path(root, row["object_path"])
        if claim_dir is None:
            raise ResearchCtlError(f"claim path missing: {claim_id}")
        blockers = validate_contract_file(claim_dir / "CONTRACT.yaml")
        if blockers:
            raise ResearchCtlError("contract schema invalid: " + "; ".join(blockers))
        current = contract_hash(root, row)
        existing = row["contract_hash"]
        if existing and existing != current:
            raise ResearchCtlError(f"contract hash changed for {claim_id}: frozen={existing}, current={current}")
        if existing == current:
            return {"claim_id": claim_id, "contract_hash": current, "idempotent": True}
        row = _update_claim(root, conn, claim_id, contract_hash=current, stage="contract_frozen")
        record_event(conn, root, "claim", claim_id, "contract_frozen", {"hash": current})
        return _claim_payload(row)


def _assert_contract_current(root: Path, claim_row: Any) -> None:
    if not claim_row["contract_hash"]:
        raise ResearchCtlError(f"contract not frozen for {claim_row['id']}")
    current = contract_hash(root, claim_row)
    if current != claim_row["contract_hash"]:
        raise ResearchCtlError(f"contract changed after freeze for {claim_row['id']}")


def start_run(
    claim_id: str,
    command: str,
    host: str = "local",
    artifact_root: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _start_run_unlocked(claim_id, command, host, artifact_root, root)


def _start_run_unlocked(
    claim_id: str,
    command: str,
    host: str = "local",
    artifact_root: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["status"] == "draft":
            raise ResearchCtlError("draft claim cannot start a run")
        if claim["status"] in {"killed", "archived", "invalidated", "inconclusive"}:
            raise ResearchCtlError(f"claim status cannot start a run: {claim['status']}")
        if (
            claim["status"] in MERGEABLE_VERDICTS | {"merged"}
            or claim["verdict"] in FINAL_VERDICTS
            or claim["paper_merge_status"] in {"queued", "merged"}
        ):
            raise ResearchCtlError(
                f"final claim cannot start a run: status={claim['status']} verdict={claim['verdict']} "
                f"paper_merge_status={claim['paper_merge_status']}"
            )
        if _budget_exhausted(claim):
            raise ResearchCtlError("claim budget is exhausted")
        _assert_contract_current(root, claim)
        run_id = next_id(conn, "runs", "R")
        claim_dir = object_path(root, claim["object_path"])
        if claim_dir is None:
            raise ResearchCtlError(f"claim path missing: {claim_id}")
        run_root = Path(artifact_root) if artifact_root else claim_dir / "RUNS" / run_id
        if not run_root.is_absolute():
            run_root = root / run_root
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "command.sh").write_text(command + "\n", encoding="utf-8")
        started = utc_now()
        conn.execute(
            "INSERT INTO runs(id, claim_id, host, status, command, artifact_root, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, claim_id, host, "running", command, _rel(root, run_root), started, None),
        )
        run_ids = _json_loads(claim["run_ids_json"], [])
        if run_id not in run_ids:
            run_ids.append(run_id)
        _update_claim(root, conn, claim_id, status="running", stage="running", run_ids_json=json.dumps(run_ids))
        record_event(conn, root, "run", run_id, "run_started", {"claim_id": claim_id, "host": host})
        return dict(_get_run(conn, run_id))


def run_event(run_id: str, event_type: str, payload: dict | None = None, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _run_event_unlocked(run_id, event_type, payload, root)


def _run_event_unlocked(run_id: str, event_type: str, payload: dict | None = None, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        _get_run(conn, run_id)
        record_event(conn, root, "run", run_id, event_type, payload or {})
        return {"run_id": run_id, "event_type": event_type}


def finish_run(run_id: str, status: str = "success", root_arg: str | os.PathLike[str] | None = None) -> dict:
    if status not in {"success", "failed", "crashed"}:
        raise ResearchCtlError("run status must be success, failed, or crashed")
    root = repo_root(root_arg)
    with file_lock(root, f"run-{run_id}"):
        with connect(root) as conn:
            row = _get_run(conn, run_id)
            if row["status"] == status:
                result = dict(row)
                result["idempotent"] = True
                return result
            finished = utc_now()
            conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, finished, run_id))
            record_event(conn, root, "run", run_id, "run_finished", {"status": status})
            return dict(_get_run(conn, run_id))


def list_runs(claim_id: str | None = None, root_arg: str | os.PathLike[str] | None = None) -> list[dict]:
    root = repo_root(root_arg)
    with connect(root) as conn:
        if claim_id:
            rows = conn.execute("SELECT * FROM runs WHERE claim_id = ? ORDER BY id", (claim_id,))
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY id")
        return [dict(row) for row in rows]


def write_verdict(
    claim_id: str,
    verdict: str,
    confidence: str = "medium",
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _write_verdict_unlocked(claim_id, verdict, confidence, root)


def _write_verdict_unlocked(
    claim_id: str,
    verdict: str,
    confidence: str = "medium",
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    if verdict not in FINAL_VERDICTS:
        raise ResearchCtlError(f"unsupported verdict: {verdict}")
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        claim_dir = object_path(root, claim["object_path"])
        if claim_dir is None:
            raise ResearchCtlError(f"claim path missing: {claim_id}")
        merge_status = "eligible" if verdict in MERGEABLE_VERDICTS else "not_eligible"
        (claim_dir / "VERDICT.yaml").write_text(
            f"claim_id: {claim_id}\nverdict: {verdict}\nconfidence: {confidence}\n\nrisk_notes: []\nallowed_scope:\n  abstract: []\n  conclusion: []\n  forbidden: []\nnext_action:\n  - {'merge_to_paper' if verdict in MERGEABLE_VERDICTS else 'archive_failure_memory'}\n",
            encoding="utf-8",
        )
        row = _update_claim(
            root,
            conn,
            claim_id,
            status=verdict,
            stage="judging",
            verdict=verdict,
            paper_merge_status=merge_status,
        )
        record_event(conn, root, "claim", claim_id, "verdict_written", {"verdict": verdict, "confidence": confidence})
        return _claim_payload(row)


def _evidence_ready(claim_dir: Path) -> bool:
    path = claim_dir / "EVIDENCE.md"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    meaningful = [line for line in text.splitlines() if line.strip() and line.strip().lower() not in {"todo", "# evidence summary"}]
    return len("\n".join(meaningful)) > 20


def _verdict_file_ready(claim_dir: Path, verdict: str | None) -> bool:
    path = claim_dir / "VERDICT.yaml"
    if not path.is_file() or not verdict:
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return f"verdict: {verdict}" in text


def _evidence_references_runs(claim_dir: Path, run_ids: list[str]) -> list[str]:
    text = _read_text(claim_dir / "EVIDENCE.md")
    return [run_id for run_id in run_ids if run_id not in text]


def _payload_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for child in value.values():
            values.extend(_payload_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_payload_values(child))
        return values
    if value is None:
        return []
    return [str(value)]


def _run_event_payload_values(conn, run_id: str, event_types: set[str] | None = None) -> list[str]:
    if event_types:
        placeholders = ", ".join("?" for _ in event_types)
        rows = conn.execute(
            f"SELECT payload_json FROM events WHERE entity_type = 'run' AND entity_id = ? AND event_type IN ({placeholders})",
            (run_id, *sorted(event_types)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT payload_json FROM events WHERE entity_type = 'run' AND entity_id = ?", (run_id,)).fetchall()
    values: list[str] = []
    for row in rows:
        values.extend(_payload_values(_json_loads(row["payload_json"], {})))
    return values


def _run_event_payloads(conn, run_id: str, event_types: set[str] | None = None) -> list[dict[str, Any]]:
    if event_types:
        placeholders = ", ".join("?" for _ in event_types)
        rows = conn.execute(
            f"SELECT payload_json FROM events WHERE entity_type = 'run' AND entity_id = ? AND event_type IN ({placeholders})",
            (run_id, *sorted(event_types)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT payload_json FROM events WHERE entity_type = 'run' AND entity_id = ?", (run_id,)).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row["payload_json"], {})
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _run_artifact_path(root: Path, run: Any, *parts: str) -> Path | None:
    artifact_root = run["artifact_root"]
    if not artifact_root:
        return None
    path = Path(artifact_root)
    if not path.is_absolute():
        path = root / path
    return path.joinpath(*parts)


def _payload_path_value(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and _is_meaningful_value(value):
            return value
    return None


def _path_matches_ref(value: str, ref: str) -> bool:
    path = Path(value.strip().strip('"').strip("'"))
    name = path.name
    stem = path.stem
    return name == ref or stem == ref


def _existing_nonempty_file(path: Path | None) -> bool:
    return bool(path and path.is_file() and path.stat().st_size > 0)


def _resolve_event_artifact_path(root: Path, run: Any, value: str) -> Path | None:
    raw = value.strip().strip('"').strip("'")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            return None
        return resolved
    repo_candidate = root / candidate
    if repo_candidate.exists():
        return repo_candidate
    artifact_root = _run_artifact_path(root, run)
    if artifact_root:
        return artifact_root / candidate
    return repo_candidate


def _path_belongs_to_run(root: Path, run: Any, path: Path | None) -> bool:
    if path is None:
        return False
    resolved = path.resolve()
    artifact_root = _run_artifact_path(root, run)
    if artifact_root:
        try:
            resolved.relative_to(artifact_root.resolve())
            return True
        except ValueError:
            pass
    try:
        rel = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    run_id = run["id"]
    return rel == f"artifacts/{run_id}" or rel.startswith(f"artifacts/{run_id}/") or rel == f"logs/{run_id}" or rel.startswith(f"logs/{run_id}/")


def _run_has_metrics_provenance(root: Path, conn, run: Any) -> bool:
    metrics_path = _run_artifact_path(root, run, "metrics.json")
    if _existing_nonempty_file(metrics_path):
        return True
    for payload in _run_event_payloads(conn, run["id"], {"metrics_recorded"}):
        metrics_ref = _payload_path_value(payload, "metrics_path", "path", "file", "artifact_path")
        resolved = _resolve_event_artifact_path(root, run, metrics_ref) if metrics_ref else None
        if metrics_ref and Path(metrics_ref).name == "metrics.json" and _existing_nonempty_file(resolved) and _path_belongs_to_run(root, run, resolved):
            return True
    return False


def _run_has_figure_provenance(root: Path, conn, run: Any, figure_ref: str) -> bool:
    ref = figure_ref.strip().strip('"').strip("'")
    if not _is_meaningful_value(ref):
        return False
    artifact_root = _run_artifact_path(root, run)
    if artifact_root and artifact_root.is_dir():
        for path in artifact_root.rglob("*"):
            if _existing_nonempty_file(path) and (path.stem == ref or path.name == ref):
                return True
    for payload in _run_event_payloads(conn, run["id"], {"artifact_recorded", "figure_recorded", "table_recorded"}):
        path_ref = _payload_path_value(payload, "artifact_path", "figure_path", "table_path", "path", "file")
        resolved = _resolve_event_artifact_path(root, run, path_ref) if path_ref else None
        if path_ref and _path_matches_ref(path_ref, ref) and _existing_nonempty_file(resolved) and _path_belongs_to_run(root, run, resolved):
            return True
    return False


def _paper_file_claim_ids(root: Path) -> set[str]:
    ids = set(_extract_claim_ids_from_yamlish(paper_dir(root) / "merged_claims.yaml"))
    ids.update(_extract_claim_ids_from_yamlish(paper_dir(root) / "CLAIM_MATRIX.yaml"))
    return ids


def _git_dirty_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        dirty.append(path.replace("\\", "/"))
    return dirty


def _path_in_allowed(path: str, allowed: list[str]) -> bool:
    path = path.replace("\\", "/").strip("/")
    for root_path in allowed:
        root_norm = root_path.replace("\\", "/").strip("/")
        if path == root_norm or path.startswith(root_norm + "/"):
            return True
    return False


def _dirty_outside_claim_paths(root: Path, claim: Any) -> list[str]:
    allowed = [p for p in [claim["object_path"], claim["worktree_path"]] if p]
    if claim["verdict"] in MERGEABLE_VERDICTS:
        allowed.append(".aris/paper")
    if claim["verdict"] in FAILURE_VERDICTS:
        allowed.append(".aris/memory")
    return [path for path in _git_dirty_files(root) if path and not _path_in_allowed(path, allowed)]


def _append_failure_memory(root: Path, claim_id: str, verdict: str, reason: str | None = None) -> None:
    path = memory_dir(root) / "failed_claims.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Failed Claims\n\n"
    marker = f"- {claim_id}:"
    if marker in existing:
        return
    detail = f" - {reason}" if reason else ""
    path.write_text(existing.rstrip() + f"\n- {claim_id}: {verdict}{detail} ({utc_now()})\n", encoding="utf-8")


def _failure_memory_contains(root: Path, claim_id: str) -> bool:
    path = memory_dir(root) / "failed_claims.md"
    if not path.is_file():
        return False
    return f"- {claim_id}:" in path.read_text(encoding="utf-8", errors="replace")


def kill_claim(claim_id: str, reason: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _kill_claim_unlocked(claim_id, reason, root)


def _kill_claim_unlocked(claim_id: str, reason: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        _get_claim(conn, claim_id)
        _append_failure_memory(root, claim_id, "killed", reason)
        row = _update_claim(root, conn, claim_id, status="killed", stage="archived", verdict="killed", paper_merge_status="not_eligible")
        if not _event_exists(conn, "claim", claim_id, "claim_killed"):
            record_event(conn, root, "claim", claim_id, "claim_killed", {"reason": reason})
        return _claim_payload(row)


def finish_claim(
    claim_id: str,
    root_arg: str | os.PathLike[str] | None = None,
    record: bool = True,
    require_paper_entry: bool = True,
) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _finish_claim_unlocked(claim_id, root, record, require_paper_entry)


def _finish_claim_unlocked(
    claim_id: str,
    root_arg: str | os.PathLike[str] | None = None,
    record: bool = True,
    require_paper_entry: bool = True,
) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        blockers: list[str] = []
        claim_dir = object_path(root, claim["object_path"])
        if claim_dir is None:
            blockers.append("claim object path is missing")
        else:
            blockers.extend(validate_contract_file(claim_dir / "CONTRACT.yaml"))
            if not _evidence_ready(claim_dir):
                blockers.append("EVIDENCE.md is missing or still empty")
            blockers.extend(validate_verdict_file(claim_dir / "VERDICT.yaml", claim_id, claim["verdict"]))

        if not claim["contract_hash"]:
            blockers.append("contract is not frozen")
        else:
            try:
                if contract_hash(root, claim) != claim["contract_hash"]:
                    blockers.append("CONTRACT.yaml changed after freeze")
            except ResearchCtlError as exc:
                blockers.append(str(exc))

        run_ids = _json_loads(claim["run_ids_json"], [])
        if not run_ids:
            blockers.append("no runs are registered")
        else:
            if claim_dir is not None:
                missing_refs = _evidence_references_runs(claim_dir, run_ids)
                for run_id in missing_refs:
                    blockers.append(f"EVIDENCE.md does not reference registered run: {run_id}")
            for run_id in run_ids:
                run = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
                if not run:
                    blockers.append(f"registered run not found: {run_id}")
                elif run["status"] not in TERMINAL_RUN_STATUSES:
                    blockers.append(f"run is not terminal: {run_id}")
                elif not _run_has_metrics_provenance(root, conn, run):
                    blockers.append(f"run missing metrics provenance: {run_id}")

        if claim["verdict"] not in FINAL_VERDICTS:
            blockers.append("no final verdict is recorded")
        elif claim["verdict"] in MERGEABLE_VERDICTS and require_paper_entry:
            blockers.extend(_validate_claim_matrix_claim(root, conn, claim_id))
        elif claim["verdict"] in FAILURE_VERDICTS and not record and not _failure_memory_contains(root, claim_id):
            blockers.append("failure memory does not include this claim")

        dirty_outside = _dirty_outside_claim_paths(root, claim)
        for path in dirty_outside:
            blockers.append(f"dirty file is outside active claim allowed paths: {path}")

        ok = not blockers
        result = {"claim_id": claim_id, "ok": ok, "blockers": blockers, "verdict": claim["verdict"]}
        if ok and record:
            if claim["verdict"] in FAILURE_VERDICTS:
                _append_failure_memory(root, claim_id, claim["verdict"])
            if _event_exists(conn, "claim", claim_id, "claim_finished"):
                result["idempotent"] = True
            else:
                record_event(conn, root, "claim", claim_id, "claim_finished", {"verdict": claim["verdict"]})
        return result


def _append_unique_line(path: Path, line: str, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else header.rstrip() + "\n"
    if line not in text:
        text = text.rstrip() + "\n" + line + "\n"
        path.write_text(text, encoding="utf-8")


def merge_claim(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _merge_claim_unlocked(claim_id, root)


def _merge_claim_unlocked(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["paper_merge_status"] == "merged" or claim["status"] == "merged":
            return {"claim_id": claim_id, "idempotent": True, "paper_merge_status": "merged"}
        if claim["verdict"] not in MERGEABLE_VERDICTS:
            raise ResearchCtlError(f"claim is not mergeable: verdict={claim['verdict']}")
    finish = finish_claim(claim_id, root, record=True, require_paper_entry=True)
    if not finish["ok"]:
        raise ResearchCtlError("claim finish gate failed: " + "; ".join(finish["blockers"]))
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        _append_unique_line(
            paper_dir(root) / "merged_claims.yaml",
            f"  - {claim_id}",
            "claims:\n",
        )
        row = _update_claim(root, conn, claim_id, status="merged", stage="merged", paper_merge_status="merged")
        record_event(conn, root, "claim", claim_id, "claim_merged", {"verdict": claim["verdict"]})
        return _claim_payload(row)


def paper_enqueue(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _paper_enqueue_unlocked(claim_id, root)


def _paper_enqueue_unlocked(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["verdict"] not in MERGEABLE_VERDICTS:
            raise ResearchCtlError(f"claim is not eligible for paper enqueue: verdict={claim['verdict']}")
    finish = finish_claim(claim_id, root, record=False, require_paper_entry=True)
    if not finish["ok"]:
        raise ResearchCtlError("claim finish gate failed: " + "; ".join(finish["blockers"]))
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["paper_merge_status"] in {"queued", "merged"}:
            return {"claim_id": claim_id, "idempotent": True, "paper_merge_status": claim["paper_merge_status"]}
        row = _update_claim(root, conn, claim_id, paper_merge_status="queued")
        record_event(conn, root, "claim", claim_id, "paper_enqueue", {"verdict": claim["verdict"]})
        return _claim_payload(row)


def paper_block(reason: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _paper_block_unlocked(reason, root)


def _paper_block_unlocked(reason: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        build_id = next_id(conn, "paper_builds", "PB")
        conn.execute(
            "INSERT INTO paper_builds(id, status, merged_claims_json, block_reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (build_id, "blocked", "[]", reason, utc_now()),
        )
        record_event(conn, root, "paper", build_id, "paper_build_blocked", {"reason": reason})
        return {"id": build_id, "status": "blocked", "block_reason": reason}


def archive_claim(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _archive_claim_unlocked(claim_id, root)


def _archive_claim_unlocked(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["status"] == "archived":
            return {"claim_id": claim_id, "idempotent": True, "status": "archived"}
        verdict = claim["verdict"]

    if verdict != "killed":
        finish = finish_claim(claim_id, root, record=True, require_paper_entry=verdict in MERGEABLE_VERDICTS)
        if not finish["ok"]:
            raise ResearchCtlError("claim finish gate failed: " + "; ".join(finish["blockers"]))

    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        if claim["verdict"] in FAILURE_VERDICTS:
            _append_failure_memory(root, claim_id, claim["verdict"])
        row = _update_claim(root, conn, claim_id, status="archived", stage="archived")
        record_event(conn, root, "claim", claim_id, "claim_archived", {"verdict": claim["verdict"]})
        return _claim_payload(row)


def attach_session(
    session_key: str,
    claim_id: str,
    role: str = "builder",
    platform: str = "codex",
    worktree: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _attach_session_unlocked(session_key, claim_id, role, platform, worktree, root)


def _attach_session_unlocked(
    session_key: str,
    claim_id: str,
    role: str = "builder",
    platform: str = "codex",
    worktree: str | None = None,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        worktree_path = worktree or claim["worktree_path"]
        if worktree_path:
            (root / worktree_path).mkdir(parents=True, exist_ok=True)
            blockers = _validate_worktree_metadata(root, claim, worktree_path)
            if blockers:
                raise ResearchCtlError("; ".join(blockers))
        if worktree_path and _role_is_write(role):
            conflict = one(
                conn,
                "SELECT session_key FROM sessions WHERE worktree_path = ? AND session_key != ? AND role NOT IN (?, ?, ?, ?, ?, ?)",
                (worktree_path, session_key, *sorted(READ_ONLY_SESSION_ROLES)),
            )
            if conflict:
                raise ResearchCtlError(f"worktree already has an active write session: {worktree_path} ({conflict['session_key']})")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO sessions(session_key, claim_id, role, platform, worktree_path, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
              claim_id=excluded.claim_id,
              role=excluded.role,
              platform=excluded.platform,
              worktree_path=excluded.worktree_path,
              last_seen_at=excluded.last_seen_at
            """,
            (session_key, claim_id, role, platform, worktree_path, now),
        )
        runtime_path = runtime_sessions_dir(root) / f"{session_key}.json"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(
            json.dumps(
                {
                    "active_anchor": claim["anchor_id"],
                    "active_claim": claim_id,
                    "role": role,
                    "platform": platform,
                    "stage": claim["stage"],
                    "worktree": worktree_path,
                    "last_seen_at": now,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        record_event(conn, root, "session", session_key, "session_attached", {"claim_id": claim_id, "role": role})
        return dict(one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,)))


def detach_session(session_key: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "state"):
        return _detach_session_unlocked(session_key, root)


def _detach_session_unlocked(session_key: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        previous = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,))
        conn.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
        runtime_path = runtime_sessions_dir(root) / f"{session_key}.json"
        if runtime_path.exists():
            runtime_path.unlink()
        record_event(conn, root, "session", session_key, "session_detached", {})
        return dict(previous) if previous else {"session_key": session_key}


def show_session(session_key: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        row = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,))
        if not row:
            raise ResearchCtlError(f"session not found: {session_key}")
        return dict(row)


def list_sessions(root_arg: str | os.PathLike[str] | None = None) -> list[dict]:
    root = repo_root(root_arg)
    with connect(root) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM sessions ORDER BY session_key")]


def add_review(
    claim_id: str,
    reviewer_role: str,
    decision: str,
    source: str = "human",
    root_arg: str | os.PathLike[str] | None = None,
) -> dict:
    if decision not in REVIEW_DECISIONS:
        raise ResearchCtlError("review decision is unsupported")
    root = repo_root(root_arg)
    with file_lock(root, f"claim-{claim_id}"):
        with connect(root) as conn:
            _get_claim(conn, claim_id)
            review_id = next_id(conn, "reviews", "RV")
            now = utc_now()
            conn.execute(
                "INSERT INTO reviews(id, claim_id, reviewer_role, decision, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, claim_id, reviewer_role, decision, source, now),
            )
            record_event(
                conn,
                root,
                "claim",
                claim_id,
                "review_recorded",
                {"review_id": review_id, "reviewer_role": reviewer_role, "decision": decision, "source": source},
            )
            return dict(one(conn, "SELECT * FROM reviews WHERE id = ?", (review_id,)))


def list_reviews(claim_id: str | None = None, root_arg: str | os.PathLike[str] | None = None) -> list[dict]:
    root = repo_root(root_arg)
    with connect(root) as conn:
        if claim_id:
            _get_claim(conn, claim_id)
            rows = conn.execute("SELECT * FROM reviews WHERE claim_id = ? ORDER BY id", (claim_id,))
        else:
            rows = conn.execute("SELECT * FROM reviews ORDER BY id")
        return [dict(row) for row in rows]


def next_required_action(claim: Any | None) -> str:
    if claim is None:
        return "create or attach a claim"
    status = claim["status"]
    stage = claim["stage"]
    if status == "draft":
        return "run claim gate and freeze CONTRACT.yaml before execution"
    if stage == "gated":
        return "freeze CONTRACT.yaml"
    if stage == "contract_frozen":
        return "start or monitor registered runs"
    if status == "running":
        return "finish active runs and update EVIDENCE.md"
    if stage == "judging":
        return "run claim audit and finish or merge the claim"
    if status == "merged":
        return "paper build may consume this claim"
    return "inspect claim state"


def snapshot_session(session_key: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        session = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,))
        if not session:
            return {
                "session": session_key,
                "claim": None,
                "stage": "no_claim",
                "next_required_action": "create or attach a claim",
                "allowed_paths": [],
                "read_files": [".aris/workflow.md"],
            }
        claim = _get_claim(conn, session["claim_id"]) if session["claim_id"] else None
        if claim is None:
            return dict(session)
        budget = _json_loads(claim["budget_json"], {})
        budget_remaining = {
            "gpu_hours": float(budget.get("gpu_hours_limit", 0)) - float(budget.get("gpu_hours_used", 0)),
            "token_usd": float(budget.get("token_usd_limit", 0)) - float(budget.get("token_usd_used", 0)),
        }
        allowed_paths, metadata_blockers = _worktree_metadata_allowed_paths(root, claim)
        return {
            "session": session_key,
            "claim": claim["id"],
            "stage": claim["stage"],
            "status": claim["status"],
            "role": session["role"],
            "platform": session["platform"],
            "worktree": session["worktree_path"],
            "allowed_paths": allowed_paths,
            "metadata_blockers": metadata_blockers,
            "next_required_action": next_required_action(claim),
            "budget_remaining": budget_remaining,
            "read_files": [
                ".aris/workflow.md",
                f"{claim['object_path']}/CONTRACT.yaml",
                f"{claim['object_path']}/EVIDENCE.md",
            ],
        }


def snapshot_claim(claim_id: str, root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id)
        required_inputs = [
            f"{claim['object_path']}/CONTRACT.yaml",
            f"{claim['object_path']}/EVIDENCE.md",
            f"{claim['object_path']}/VERDICT.yaml",
        ]
        return {
            "claim_id": claim_id,
            "title": claim["title"],
            "status": claim["status"],
            "stage": claim["stage"],
            "verdict": claim["verdict"],
            "verdict_ready": claim["verdict"] in FINAL_VERDICTS,
            "required_inputs": required_inputs,
            "forbidden_actions": ["modify_contract_after_freeze", "write_paper_narrative_before_verdict"],
            "next_required_action": next_required_action(claim),
        }


def snapshot_paper(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with connect(root) as conn:
        rows = conn.execute(
            "SELECT id, title, verdict, paper_merge_status FROM claims WHERE paper_merge_status = 'merged' ORDER BY id"
        ).fetchall()
        return {
            "merged_claims": [dict(row) for row in rows],
            "claim_matrix": str(paper_dir(root) / "CLAIM_MATRIX.yaml"),
            "citation_ledger": str(paper_dir(root) / "CITATION_LEDGER.json"),
        }


def _context_entry(root: Path, path: Path, role: str) -> dict[str, Any]:
    return {
        "path": _rel(root, path),
        "role": role,
        "exists": path.exists(),
        "type": "dir" if path.is_dir() else "file",
    }


def _context_event_path_entries(root: Path, conn, run: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    event_keys = {
        "metrics_recorded": ("metrics_path", "path", "file", "artifact_path"),
        "artifact_recorded": ("artifact_path", "path", "file"),
        "figure_recorded": ("figure_path", "artifact_path", "path", "file"),
        "table_recorded": ("table_path", "artifact_path", "path", "file"),
    }
    for event_type, keys in event_keys.items():
        for payload in _run_event_payloads(conn, run["id"], {event_type}):
            ref = _payload_path_value(payload, *keys)
            if not ref:
                continue
            resolved = _resolve_event_artifact_path(root, run, ref)
            if resolved:
                entries.append(_context_entry(root, resolved, f"run_{event_type}"))
    return entries


def _write_context_jsonl(path: Path, entries: list[dict[str, Any]], kind: str, claim_id: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            payload = {"kind": kind, "claim_id": claim_id, **entry}
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def context_payload(
    kind: str,
    claim_id: str | None = None,
    write: bool = False,
    root_arg: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if kind not in {"experiment", "judge", "writing"}:
        raise ResearchCtlError(f"unsupported context kind: {kind}")
    if kind in {"experiment", "judge"} and not claim_id:
        raise ResearchCtlError(f"context {kind} requires --claim")

    root = repo_root(root_arg)
    entries: list[dict[str, Any]] = []
    output_path: Path | None = None
    with connect(root) as conn:
        claim = _get_claim(conn, claim_id) if claim_id else None
        claim_dir = object_path(root, claim["object_path"]) if claim else None
        worktree_dir = object_path(root, claim["worktree_path"]) if claim and claim["worktree_path"] else None

        if kind == "experiment":
            if claim_dir is None or worktree_dir is None:
                raise ResearchCtlError(f"claim paths missing: {claim_id}")
            entries.extend(
                [
                    _context_entry(root, claim_dir / "CONTRACT.yaml", "frozen_contract"),
                    _context_entry(root, claim_dir / "PLAN.md", "experiment_plan"),
                    _context_entry(root, worktree_dir, "claim_worktree"),
                ]
            )
            output_path = claim_dir / "experiment.jsonl"

        elif kind == "judge":
            if claim_dir is None:
                raise ResearchCtlError(f"claim path missing: {claim_id}")
            entries.extend(
                [
                    _context_entry(root, claim_dir / "CONTRACT.yaml", "frozen_contract"),
                    _context_entry(root, claim_dir / "EVIDENCE.md", "evidence_summary"),
                    _context_entry(root, claim_dir / "VERDICT.yaml", "verdict_target"),
                ]
            )
            for run in conn.execute("SELECT * FROM runs WHERE claim_id = ? ORDER BY id", (claim_id,)):
                artifact_root = _run_artifact_path(root, run)
                if artifact_root:
                    entries.append(_context_entry(root, artifact_root, f"run_{run['id']}_artifact_root"))
                    entries.append(_context_entry(root, artifact_root / "metrics.json", f"run_{run['id']}_metrics"))
                entries.extend(_context_event_path_entries(root, conn, run))
            output_path = claim_dir / "judge.jsonl"

        else:
            entries.extend(
                [
                    _context_entry(root, paper_dir(root) / "CLAIM_MATRIX.yaml", "claim_matrix"),
                    _context_entry(root, paper_dir(root) / "CITATION_LEDGER.json", "citation_ledger"),
                ]
            )
            params: tuple[Any, ...]
            query = "SELECT * FROM claims WHERE paper_merge_status = 'merged'"
            if claim_id:
                query += " AND id = ?"
                params = (claim_id,)
            else:
                params = ()
            for merged in conn.execute(query + " ORDER BY id", params):
                merged_dir = object_path(root, merged["object_path"])
                if not merged_dir:
                    continue
                entries.extend(
                    [
                        _context_entry(root, merged_dir / "CLAIM.md", f"merged_claim_{merged['id']}_claim"),
                        _context_entry(root, merged_dir / "EVIDENCE.md", f"merged_claim_{merged['id']}_evidence"),
                        _context_entry(root, merged_dir / "VERDICT.yaml", f"merged_claim_{merged['id']}_verdict"),
                    ]
                )
            output_path = (claim_dir / "writing.jsonl") if claim_dir else (paper_dir(root) / "writing.jsonl")

    payload: dict[str, Any] = {
        "kind": kind,
        "claim_id": claim_id,
        "files": entries,
    }
    if write and output_path:
        _write_context_jsonl(output_path, entries, kind, claim_id)
        payload["written"] = _rel(root, output_path)
    return payload


def _normalize_rel(path: str, root: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            return candidate.as_posix()
    rel = candidate.as_posix().replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def _path_allowed(path: str, allowed_paths: list[str], root: Path) -> bool:
    rel = _normalize_rel(path, root).strip("/")
    for allowed in allowed_paths:
        allowed_rel = _normalize_rel(allowed, root).strip("/")
        if rel == allowed_rel or rel.startswith(allowed_rel + "/"):
            return True
    return False


def _is_worktree_metadata_path(rel: str) -> bool:
    return bool(re.match(r"^worktrees/[^/]+/worktree\.yaml$", rel.strip("/")))


def _paper_write_blocker(rel: str, claim: Any | None, role_key: str) -> str | None:
    rel = rel.strip("/")
    if not rel.startswith(".aris/paper/"):
        return None
    filename = Path(rel).name
    paper_writer_role = role_key in {"paper_build", "paper_builder", "writer"}
    paper_gate_files = {"CLAIM_MATRIX.yaml", "CITATION_LEDGER.json", "SUBMISSION_CHECKLIST.json", "NARRATIVE_REPORT.md"}
    if filename == "merged_claims.yaml":
        return "merged_claims.yaml is maintained by researchctl claim merge"
    if paper_writer_role:
        return None if filename in paper_gate_files else f"paper writer cannot write non-gate paper file: {rel}"
    if filename == "CLAIM_MATRIX.yaml" and claim and claim["verdict"] in MERGEABLE_VERDICTS and claim["stage"] == "judging":
        return None
    return f"claim session cannot write paper file at this stage: {rel}"


def _claim_for_path(conn, rel: str):
    rows = conn.execute("SELECT * FROM claims ORDER BY length(object_path) DESC").fetchall()
    rel = rel.strip("/")
    for row in rows:
        object_rel = str(row["object_path"]).strip("/")
        if rel == object_rel or rel.startswith(object_rel + "/"):
            return row
    return None


def evaluate_pre_tool_policy(
    root_arg: str | os.PathLike[str] | None = None,
    session_key: str | None = None,
    is_run_start: bool = False,
    write_paths: list[str] | None = None,
    read_paths: list[str] | None = None,
) -> dict:
    root = repo_root(root_arg)
    snapshot = snapshot_session(session_key or "unknown", root)
    blockers: list[str] = []
    with connect(root) as conn:
        session = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,)) if session_key else None
        claim = _get_claim(conn, session["claim_id"]) if session and session["claim_id"] else None
        role = str(session["role"]) if session else ""
        role_key = role.replace("-", "_").lower()

        if is_run_start:
            if not claim:
                blockers.append("cannot start a run without an active claim")
            else:
                if claim["status"] == "draft":
                    blockers.append("draft claim cannot start a run")
                if claim["status"] in {"killed", "archived", "invalidated", "inconclusive"}:
                    blockers.append(f"claim status cannot start a run: {claim['status']}")
                if claim["status"] in MERGEABLE_VERDICTS | {"merged"} or claim["verdict"] in FINAL_VERDICTS:
                    blockers.append("final claim cannot start a run")
                if _budget_exhausted(claim):
                    blockers.append("claim budget is exhausted")
                if not claim["contract_hash"]:
                    blockers.append("contract is not frozen")

        allowed_paths = [str(path) for path in snapshot.get("allowed_paths", [])]
        for path in write_paths or []:
            rel = _normalize_rel(path, root)
            if _is_worktree_metadata_path(rel):
                blockers.append("cannot modify worktree metadata through tool hooks")
                continue
            if role_key in {"paper_build", "paper_builder", "writer"} and not rel.startswith(".aris/paper/"):
                blockers.append(f"paper writer cannot write outside paper gate files: {rel}")
                continue
            paper_blocker = _paper_write_blocker(rel, claim, role_key)
            if paper_blocker:
                blockers.append(paper_blocker)
                continue
            if rel.startswith(".aris/paper/"):
                continue
            if claim and rel.endswith("CONTRACT.yaml") and claim["contract_hash"]:
                blockers.append("cannot modify CONTRACT.yaml after contract freeze")
            if allowed_paths and not _path_allowed(path, allowed_paths, root):
                blockers.append(f"path is outside active claim allowed paths: {rel}")
            elif not allowed_paths:
                blockers.append(f"cannot write without an active claim: {rel}")

        paper_role = role_key in {"paper_build", "paper_builder", "writer", "reader"}
        blockers.extend(str(blocker) for blocker in snapshot.get("metadata_blockers", []))
        for path in read_paths or []:
            rel = _normalize_rel(path, root)
            if rel.startswith(".aris/paper/") and claim and claim["paper_merge_status"] != "merged":
                blockers.append("paper files may only be consumed from merged claims")
            read_claim = _claim_for_path(conn, rel)
            if paper_role and read_claim and (
                read_claim["paper_merge_status"] != "merged" or read_claim["verdict"] not in MERGEABLE_VERDICTS
            ):
                blockers.append(f"paper reader cannot consume unmerged claim object: {read_claim['id']}")

    return {"ok": not blockers, "blockers": blockers, "snapshot": snapshot}


def _extract_claim_ids_from_yamlish(path: Path) -> list[str]:
    if not path.is_file():
        return []
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("- id:"):
            value = stripped.split(":", 1)[1].strip().strip('"')
            if value.startswith("C") and value[1:].isdigit():
                ids.append(value)
        elif stripped.startswith("- "):
            value = stripped[2:].strip()
            if value.startswith("C") and value[1:].isdigit():
                ids.append(value)
        if stripped.startswith("id:"):
            value = stripped.split(":", 1)[1].strip().strip('"')
            if value.startswith("C") and value[1:].isdigit():
                ids.append(value)
    return list(dict.fromkeys(ids))


def _entry_values(entry: dict[str, Any], key: str) -> list[str]:
    value = entry.get(key)
    if isinstance(value, list):
        return [str(item).strip().strip('"').strip("'") for item in value if str(item).strip()]
    if isinstance(value, str):
        return _parse_inline_list(value)
    return []


def _claim_matrix_entries(root: Path) -> list[dict[str, Any]]:
    return _parse_yamlish_list_maps(paper_dir(root) / "CLAIM_MATRIX.yaml", "claims")


def _claim_matrix_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for entry in _claim_matrix_entries(root):
        claim_id = str(entry.get("id", "")).strip()
        if claim_id:
            ids.add(claim_id)
    return ids


def _validate_claim_matrix_entry(root: Path, conn, entry: dict[str, Any]) -> tuple[str | None, list[str]]:
    blockers: list[str] = []
    claim_id = str(entry.get("id", "")).strip()
    if not claim_id:
        return None, ["CLAIM_MATRIX.yaml entry missing id"]
    claim = one(conn, "SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not claim:
        return claim_id, [f"claim in CLAIM_MATRIX.yaml is unknown: {claim_id}"]
    text = str(entry.get("text", "")).strip()
    if not _is_meaningful_value(text):
        blockers.append(f"CLAIM_MATRIX.yaml entry missing text: {claim_id}")
    verdict = str(entry.get("verdict", "")).strip()
    if verdict != claim["verdict"]:
        blockers.append(f"CLAIM_MATRIX.yaml verdict does not match DB for {claim_id}: {verdict} != {claim['verdict']}")
    if verdict not in MERGEABLE_VERDICTS:
        blockers.append(f"CLAIM_MATRIX.yaml entry is not mergeable: {claim_id} verdict={verdict}")
    support_runs = _entry_values(entry, "support_runs")
    if not any(_is_meaningful_value(value) for value in support_runs):
        blockers.append(f"CLAIM_MATRIX.yaml entry missing support_runs: {claim_id}")
    run_ids = set(_json_loads(claim["run_ids_json"], []))
    support_run_rows: list[Any] = []
    for run_id in support_runs:
        if not _is_meaningful_value(run_id):
            continue
        run = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            blockers.append(f"CLAIM_MATRIX.yaml support_run not found for {claim_id}: {run_id}")
        elif run["claim_id"] != claim_id:
            blockers.append(f"CLAIM_MATRIX.yaml support_run belongs to another claim: {run_id}")
        elif run["status"] not in TERMINAL_RUN_STATUSES:
            blockers.append(f"CLAIM_MATRIX.yaml support_run is not terminal: {run_id}")
        else:
            support_run_rows.append(run)
        if run_id not in run_ids:
            blockers.append(f"CLAIM_MATRIX.yaml support_run is not registered on claim {claim_id}: {run_id}")
    figure_refs = _entry_values(entry, "figure_table_refs")
    if not any(_is_meaningful_value(value) for value in figure_refs):
        blockers.append(f"CLAIM_MATRIX.yaml entry missing figure_table_refs: {claim_id}")
    else:
        for figure_ref in figure_refs:
            if _is_meaningful_value(figure_ref) and not any(_run_has_figure_provenance(root, conn, run, figure_ref) for run in support_run_rows):
                blockers.append(f"CLAIM_MATRIX.yaml figure_table_ref has no run provenance for {claim_id}: {figure_ref}")
    allowed_scope = str(entry.get("allowed_scope", "")).strip()
    if not _is_meaningful_value(allowed_scope):
        blockers.append(f"CLAIM_MATRIX.yaml entry missing allowed_scope: {claim_id}")
    return claim_id, blockers


def _validate_claim_matrix_claim(root: Path, conn, claim_id: str) -> list[str]:
    matches = [entry for entry in _claim_matrix_entries(root) if str(entry.get("id", "")).strip() == claim_id]
    if not matches:
        return ["CLAIM_MATRIX.yaml does not include this mergeable claim"]
    blockers: list[str] = []
    if len(matches) > 1:
        blockers.append(f"CLAIM_MATRIX.yaml has duplicate entries for claim: {claim_id}")
    for entry in matches:
        _, entry_blockers = _validate_claim_matrix_entry(root, conn, entry)
        blockers.extend(entry_blockers)
    return blockers


def audit_paper_build(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    blockers: list[str] = []
    merged_path = paper_dir(root) / "merged_claims.yaml"
    matrix_path = paper_dir(root) / "CLAIM_MATRIX.yaml"
    citation_path = paper_dir(root) / "CITATION_LEDGER.json"
    merged_ids = set(_extract_claim_ids_from_yamlish(merged_path))
    matrix_ids = _claim_matrix_ids(root)
    ids = merged_ids | matrix_ids
    if not merged_path.is_file():
        blockers.append("merged_claims.yaml is missing")
    elif not merged_ids:
        blockers.append("merged_claims.yaml has no merged claims")
    if not matrix_path.is_file():
        blockers.append("CLAIM_MATRIX.yaml is missing")
    elif not matrix_ids:
        blockers.append("CLAIM_MATRIX.yaml has no claims")
    if not citation_path.is_file():
        blockers.append("CITATION_LEDGER.json is missing")
    else:
        citation_audit = audit_citation(root)
        blockers.extend(citation_audit["blockers"])
    for claim_id in sorted(merged_ids - matrix_ids):
        blockers.append(f"merged claim missing from CLAIM_MATRIX.yaml: {claim_id}")
    with connect(root) as conn:
        for entry in _claim_matrix_entries(root):
            claim_id, entry_blockers = _validate_claim_matrix_entry(root, conn, entry)
            blockers.extend(entry_blockers)
            if claim_id:
                ids.add(claim_id)
        for claim_id in sorted(ids):
            claim = one(conn, "SELECT * FROM claims WHERE id = ?", (claim_id,))
            if not claim:
                blockers.append(f"claim in paper files is unknown: {claim_id}")
                continue
            if claim["paper_merge_status"] != "merged":
                blockers.append(f"claim is not merged but appears in paper files: {claim_id}")
            if claim["verdict"] not in MERGEABLE_VERDICTS:
                blockers.append(f"unsupported claim appears in paper files: {claim_id} verdict={claim['verdict']}")
    return {"ok": not blockers, "blockers": blockers, "claims_checked": sorted(ids)}


def _paper_build_manifest_path(root: Path) -> Path:
    return paper_dir(root) / "BUILD_MANIFEST.json"


def _paper_build_manifest(root: Path, build_id: str, created_at: str, audit: dict) -> dict:
    matrix_path = paper_dir(root) / "CLAIM_MATRIX.yaml"
    citation_path = paper_dir(root) / "CITATION_LEDGER.json"
    merged_path = paper_dir(root) / "merged_claims.yaml"
    return {
        "build_id": build_id,
        "created_at": created_at,
        "claims_checked": audit["claims_checked"],
        "claim_matrix_path": _rel(root, matrix_path),
        "citation_ledger_path": _rel(root, citation_path),
        "merged_claims_path": _rel(root, merged_path),
        "claim_matrix_hash": _file_hash(matrix_path),
        "citation_ledger_hash": _file_hash(citation_path),
        "merged_claims_hash": _file_hash(merged_path),
    }


def _load_paper_build_manifest(root: Path) -> tuple[dict | None, list[str]]:
    path = _paper_build_manifest_path(root)
    if not path.is_file():
        return None, ["BUILD_MANIFEST.json is missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"BUILD_MANIFEST.json is invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["BUILD_MANIFEST.json is not an object"]
    return data, []


def _audit_paper_build_manifest(root: Path, conn, paper_audit: dict) -> tuple[dict | None, list[str]]:
    blockers: list[str] = []
    latest = one(
        conn,
        "SELECT * FROM paper_builds WHERE status = 'success' ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    if not latest:
        blockers.append("submission missing successful paper build")
    manifest, manifest_blockers = _load_paper_build_manifest(root)
    blockers.extend(manifest_blockers)
    if not manifest:
        return None, blockers
    if latest and manifest.get("build_id") != latest["id"]:
        blockers.append("BUILD_MANIFEST.json does not match latest successful paper build")
    if latest:
        row_claims = sorted(str(claim_id) for claim_id in _json_loads(latest["merged_claims_json"], []))
        manifest_claims = sorted(str(claim_id) for claim_id in manifest.get("claims_checked", []))
        if manifest_claims != row_claims:
            blockers.append("BUILD_MANIFEST.json claims_checked does not match paper_builds row")
    current_claims = sorted(str(claim_id) for claim_id in paper_audit["claims_checked"])
    manifest_claims = sorted(str(claim_id) for claim_id in manifest.get("claims_checked", []))
    if manifest_claims != current_claims:
        blockers.append("BUILD_MANIFEST.json claims_checked does not match current paper audit")
    hash_checks = (
        ("claim_matrix_hash", paper_dir(root) / "CLAIM_MATRIX.yaml", "CLAIM_MATRIX.yaml"),
        ("citation_ledger_hash", paper_dir(root) / "CITATION_LEDGER.json", "CITATION_LEDGER.json"),
        ("merged_claims_hash", paper_dir(root) / "merged_claims.yaml", "merged_claims.yaml"),
    )
    for key, path, label in hash_checks:
        if manifest.get(key) != _file_hash(path):
            blockers.append(f"BUILD_MANIFEST.json {key} does not match current {label}")
    return manifest, blockers


def paper_build(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    with file_lock(root, "paper-build"):
        audit = audit_paper_build(root)
        with connect(root) as conn:
            build_id = next_id(conn, "paper_builds", "PB")
            status = "success" if audit["ok"] else "blocked"
            reason = None if audit["ok"] else "; ".join(audit["blockers"])
            now = utc_now()
            manifest = _paper_build_manifest(root, build_id, now, audit)
            conn.execute(
                "INSERT INTO paper_builds(id, status, merged_claims_json, block_reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (build_id, status, json.dumps(audit["claims_checked"]), reason, now),
            )
            if audit["ok"]:
                _paper_build_manifest_path(root).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            record_event(conn, root, "paper", build_id, "paper_build_" + status, {"blockers": audit["blockers"], "manifest": manifest})
            return {"id": build_id, "status": status, "manifest": manifest, **audit}


def audit_citation(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    path = paper_dir(root) / "CITATION_LEDGER.json"
    if not path.is_file():
        return {"ok": False, "blockers": ["CITATION_LEDGER.json is missing"]}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "blockers": [f"CITATION_LEDGER.json is invalid JSON: {exc}"]}
    blockers: list[str] = []
    citations = data.get("citations") if isinstance(data, dict) else None
    if not isinstance(citations, list) or not citations:
        blockers.append("CITATION_LEDGER.json has no citations")
    else:
        for index, citation in enumerate(citations, start=1):
            if not isinstance(citation, dict):
                blockers.append(f"CITATION_LEDGER.json citation {index} is not an object")
                continue
            for key in ("key", "title", "source_url"):
                if not _is_meaningful_value(str(citation.get(key, ""))):
                    blockers.append(f"CITATION_LEDGER.json citation {index} missing {key}")
            if citation.get("metadata_verified") is not True:
                blockers.append(f"CITATION_LEDGER.json citation {index} metadata_verified is not true")
            sections = citation.get("used_in_sections")
            if not isinstance(sections, list) or not any(_is_meaningful_value(str(section)) for section in sections):
                blockers.append(f"CITATION_LEDGER.json citation {index} missing used_in_sections")
    return {"ok": not blockers, "blockers": blockers, "citations_checked": len(citations) if isinstance(citations, list) else 0}


def _check_submission_value(data: dict[str, Any], key: str, label: str, blockers: list[str]) -> None:
    value = data.get(key)
    if value is True:
        return
    if isinstance(value, str) and _is_meaningful_value(value):
        return
    blockers.append(f"SUBMISSION_CHECKLIST.json missing {label}")


def audit_submission_checklist(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    path = paper_dir(root) / "SUBMISSION_CHECKLIST.json"
    if not path.is_file():
        return {"ok": False, "blockers": ["SUBMISSION_CHECKLIST.json is missing"]}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "blockers": [f"SUBMISSION_CHECKLIST.json is invalid JSON: {exc}"]}
    if not isinstance(data, dict):
        return {"ok": False, "blockers": ["SUBMISSION_CHECKLIST.json is not an object"]}
    blockers: list[str] = []
    repro = data.get("repro_manifest")
    if not isinstance(repro, dict):
        blockers.append("SUBMISSION_CHECKLIST.json missing repro_manifest")
    else:
        for key, label in (
            ("code", "repro_manifest.code"),
            ("data", "repro_manifest.data"),
            ("hardware", "repro_manifest.hardware"),
            ("seed_policy", "repro_manifest.seed_policy"),
        ):
            _check_submission_value(repro, key, label, blockers)

    disclosure = data.get("authorship_ai_disclosure")
    if isinstance(disclosure, dict):
        if disclosure.get("verified") is not True and disclosure.get("ai_use_disclosed") is not True:
            blockers.append("SUBMISSION_CHECKLIST.json missing authorship_ai_disclosure")
    elif not _is_meaningful_value(str(disclosure or "")):
        blockers.append("SUBMISSION_CHECKLIST.json missing authorship_ai_disclosure")

    venue = data.get("venue_format")
    if not isinstance(venue, dict):
        blockers.append("SUBMISSION_CHECKLIST.json missing venue_format")
    else:
        if not _is_meaningful_value(str(venue.get("venue", ""))):
            blockers.append("SUBMISSION_CHECKLIST.json missing venue_format.venue")
        if venue.get("verified") is not True:
            blockers.append("SUBMISSION_CHECKLIST.json venue_format.verified is not true")

    if data.get("external_artifacts_checked") is not True:
        blockers.append("SUBMISSION_CHECKLIST.json external_artifacts_checked is not true")
    return {"ok": not blockers, "blockers": blockers}


def audit_submission(root_arg: str | os.PathLike[str] | None = None) -> dict:
    root = repo_root(root_arg)
    blockers: list[str] = []
    paper_audit = audit_paper_build(root)
    blockers.extend(paper_audit["blockers"])
    citation_audit = audit_citation(root)
    for blocker in citation_audit["blockers"]:
        if blocker not in blockers:
            blockers.append(blocker)
    submission_checklist = audit_submission_checklist(root)
    for blocker in submission_checklist["blockers"]:
        if blocker not in blockers:
            blockers.append(blocker)
    reviews_checked = 0
    reviews_by_claim: dict[str, list[dict]] = {}
    build_manifest: dict | None = None
    with connect(root) as conn:
        build_manifest, manifest_blockers = _audit_paper_build_manifest(root, conn, paper_audit)
        blockers.extend(manifest_blockers)
        for claim_id in paper_audit["claims_checked"]:
            claim_audit = finish_claim(claim_id, root, record=False)
            blockers.extend(f"claim {claim_id}: {blocker}" for blocker in claim_audit["blockers"])
            reviews = conn.execute("SELECT * FROM reviews WHERE claim_id = ? ORDER BY id", (claim_id,)).fetchall()
            review_dicts = [dict(review) for review in reviews]
            reviews_by_claim[claim_id] = review_dicts
            reviews_checked += len(review_dicts)
            if not reviews:
                blockers.append(f"submission missing review for merged claim: {claim_id}")
            else:
                decisions = {str(review["decision"]) for review in reviews}
                blocking = sorted(decisions & BLOCKING_REVIEW_DECISIONS)
                if blocking:
                    blockers.append(f"submission has blocking review for merged claim: {claim_id} decision={','.join(blocking)}")
                if not decisions & PASSING_REVIEW_DECISIONS:
                    blockers.append(f"submission missing passing review for merged claim: {claim_id}")
    blockers = list(dict.fromkeys(blockers))
    return {
        "ok": not blockers,
        "blockers": blockers,
        "claims_checked": paper_audit["claims_checked"],
        "reviews_checked": reviews_checked,
        "reviews_by_claim": reviews_by_claim,
        "paper_build_ok": paper_audit["ok"],
        "paper_build_manifest": build_manifest,
        "citation_ok": citation_audit["ok"],
        "submission_checklist_ok": submission_checklist["ok"],
    }
