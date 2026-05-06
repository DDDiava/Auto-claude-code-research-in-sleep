from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .core import (
    ResearchCtlError,
    evaluate_pre_tool_policy,
    finish_claim,
    run_event,
    snapshot_session,
    start_run,
)
from .db import connect, one
from .paths import aris_dir, repo_root


TAG_RE = re.compile(
    r"\[workflow-state:([A-Za-z0-9_-]+)\]\s*\n(.*?)\n\s*\[/workflow-state:\1\]",
    re.DOTALL,
)


def load_workflow_blocks(root: Path) -> dict[str, str]:
    path = aris_dir(root) / "workflow.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {match.group(1): match.group(2).strip() for match in TAG_RE.finditer(text)}


def status_to_workflow_state(status: str | None, stage: str | None) -> str:
    if not status:
        return "no_claim"
    if status == "draft":
        return "draft"
    if stage == "contract_frozen":
        return "contract_frozen"
    if status == "gated":
        return "gated"
    if status == "running":
        return "running"
    if stage == "judging" or status in {"supported", "partial_supported", "invalidated", "inconclusive", "killed"}:
        return "judging"
    if status == "merged":
        return "merged"
    if status == "archived":
        return "archived"
    return status


def resolve_session_key(input_data: dict | None = None, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    input_data = input_data or {}
    for key in ("session", "session_id", "sessionId", "conversation_id", "conversationId"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    for key in ("RESEARCHCTL_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = os.environ.get(key)
        if value:
            return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return None


def workflow_state_context(root: Path, session_key: str | None) -> str:
    blocks = load_workflow_blocks(root)
    claim_id = None
    status = None
    stage = None
    if session_key:
        with connect(root) as conn:
            session = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,))
            if session and session["claim_id"]:
                claim = one(conn, "SELECT * FROM claims WHERE id = ?", (session["claim_id"],))
                if claim:
                    claim_id = claim["id"]
                    status = claim["status"]
                    stage = claim["stage"]
    state = status_to_workflow_state(status, stage)
    body = blocks.get(state, "Refer to .aris/workflow.md for current step.")
    header = f"Claim: {claim_id} ({state})" if claim_id else f"Status: {state}"
    return f"<workflow-state>\n{header}\n{body}\n</workflow-state>"


def workflow_state_hook(root: Path, input_data: dict | None = None, session_key: str | None = None) -> dict:
    resolved = resolve_session_key(input_data, session_key)
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": workflow_state_context(root, resolved),
        }
    }


def session_start_hook(root: Path, input_data: dict | None = None, session_key: str | None = None) -> dict:
    resolved = resolve_session_key(input_data, session_key) or "unknown"
    workflow = aris_dir(root) / "workflow.md"
    workflow_text = workflow.read_text(encoding="utf-8", errors="replace") if workflow.is_file() else "No .aris/workflow.md found."
    payload = {
        "session": resolved,
        "snapshot": snapshot_session(resolved, root),
        "workflow_excerpt": workflow_text[:12000],
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "<researchctl-session>\n" + json.dumps(payload, indent=2, ensure_ascii=False) + "\n</researchctl-session>",
        }
    }


def stop_hook(root: Path, input_data: dict | None = None, session_key: str | None = None) -> tuple[int, dict]:
    resolved = resolve_session_key(input_data, session_key)
    if not resolved:
        return 0, {"ok": True, "message": "no session key"}
    snap = snapshot_session(resolved, root)
    claim_id = snap.get("claim")
    if not claim_id:
        return 0, {"ok": True, "message": "no active claim"}
    result = finish_claim(str(claim_id), root)
    return (0 if result["ok"] else 2), result


def platform_hook_output(event_name: str, code: int, payload: dict, platform: str = "claude") -> dict:
    """Translate internal researchctl hook results to agent hook JSON.

    The CLI keeps the raw `ok`/`decision` payload for tests and direct
    debugging. Codex and Claude hook runners are stricter, so template hook
    adapters request this platform output instead of printing raw state.
    """
    if event_name == "PreToolUse":
        blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
        reason = "; ".join(str(item) for item in blockers if str(item).strip())
        decision = "allow" if code == 0 and payload.get("ok", True) else "deny"
        output = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
            },
        }
        # Codex currently rejects an explicit permissionDecision="allow".
        # Omit the field on allow so the default permission flow proceeds;
        # keep deny explicit so guardrail blockers still stop the tool.
        if not (platform == "codex" and decision == "allow"):
            output["hookSpecificOutput"]["permissionDecision"] = decision
        if reason:
            output["hookSpecificOutput"]["permissionDecisionReason"] = reason
        return output

    if event_name == "PostToolUse":
        output = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
            },
        }
        if payload.get("error"):
            output["systemMessage"] = f"researchctl post-tool warning: {payload['error']}"
        events = payload.get("events")
        if events:
            output["hookSpecificOutput"]["additionalContext"] = (
                "<researchctl-post-tool>\n"
                + json.dumps({"events": events}, ensure_ascii=False, sort_keys=True)
                + "\n</researchctl-post-tool>"
            )
        return output

    if event_name == "Stop":
        if code == 0 and payload.get("ok", True):
            return {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": "<researchctl-stop>\n"
                    + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    + "\n</researchctl-stop>",
                },
            }
        blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
        reason = "; ".join(str(item) for item in blockers if str(item).strip()) or str(payload.get("error") or "researchctl stop gate failed")
        return {
            "continue": False,
            "stopReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "<researchctl-stop>\n"
                + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                + "\n</researchctl-stop>",
            },
        }

    return payload


def platform_hook_error(event_name: str, error: Exception) -> dict:
    message = str(error)
    if event_name == "PreToolUse":
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"researchctl hook error: {message}",
            },
        }
    if event_name == "Stop":
        return {
            "continue": False,
            "stopReason": f"researchctl stop hook error: {message}",
            "hookSpecificOutput": {"hookEventName": "Stop"},
        }
    return {
        "continue": True,
        "systemMessage": f"researchctl {event_name} hook warning: {message}",
        "hookSpecificOutput": {"hookEventName": event_name},
    }


def _input_value(input_data: dict, *keys: str) -> str | None:
    for key in keys:
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_name(input_data: dict) -> str:
    return (_input_value(input_data, "tool_name", "toolName", "name", "tool") or "").lower()


def _tool_payload(input_data: dict) -> dict:
    for key in ("tool_input", "toolInput", "input", "parameters", "args"):
        value = input_data.get(key)
        if isinstance(value, dict):
            return value
    return input_data


def _candidate_paths(input_data: dict) -> list[str]:
    payload = _tool_payload(input_data)
    paths: list[str] = []
    for key in ("path", "file_path", "filePath", "target", "cwd"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    for key in ("paths", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            paths.extend(str(item).strip() for item in value if str(item).strip())
    command = payload.get("command") or payload.get("cmd")
    if isinstance(command, str):
        for match in re.findall(r"(?:(?:^|\s)(?:[A-Za-z]:)?[./\\]?[A-Za-z0-9_.\\/-]+\.(?:py|yaml|yml|json|md|tex|txt|csv|log))", command):
            paths.append(match.strip())
        for match in re.findall(r"(?:>|>>)\s*([^\s;&|]+)", command):
            paths.append(match.strip().strip('"').strip("'"))
        for match in re.findall(r"(?<![\w.-])(?:[A-Za-z]:)?(?:\.{1,2}[\\/])?(?:worktrees|\.aris|artifacts|logs)[\\/][^\s;&|><\"']+", command):
            paths.append(match.strip().strip('"').strip("'"))
        for match in re.findall(
            r"(?i)\b(?:set-content|add-content|out-file|new-item|remove-item|copy-item|move-item|rename-item|clear-content)\s+([^\s;&|><\"']+)",
            command,
        ):
            paths.append(match.strip().strip('"').strip("'"))
    return list(dict.fromkeys(paths))


def _is_write_tool(input_data: dict) -> bool:
    name = _tool_name(input_data)
    action = (_input_value(input_data, "action", "researchctl_action") or "").lower()
    return any(token in name for token in ("write", "edit", "multiedit", "apply_patch")) or action in {"write", "edit"}


def _is_shell_tool(input_data: dict) -> bool:
    name = _tool_name(input_data)
    return any(token in name for token in ("bash", "shell", "powershell", "terminal", "exec", "run_command"))


def _shell_command_is_mutating(command: str) -> bool:
    if re.search(r"(^|[^>])>{1,2}[^>]", command):
        return True
    return (
        re.search(r"(?i)(^|[;&|]\s*|\s)(touch|mkdir|rm|mv|cp|tee|sed\s+-i)\b", command) is not None
        or re.search(r"(?i)(^|[;&|]\s*|\s)python\s+(?:-c\b|-\s*(?:<<|\Z)|<<)", command) is not None
        or re.search(
            r"(?i)\b(set-content|add-content|out-file|new-item|remove-item|copy-item|move-item|rename-item|clear-content)\b",
            command,
        )
        is not None
    )


def _is_read_tool(input_data: dict) -> bool:
    name = _tool_name(input_data)
    return any(token in name for token in ("read", "open", "grep", "search"))


def _command_text(input_data: dict) -> str:
    payload = _tool_payload(input_data)
    value = payload.get("command") or payload.get("cmd") or input_data.get("command") or input_data.get("cmd")
    return value if isinstance(value, str) else ""


def _is_run_start_request(input_data: dict) -> bool:
    action = (_input_value(input_data, "action", "researchctl_action") or "").lower()
    command = _command_text(input_data).lower()
    return action in {"run_start", "start_run"} or "train.py" in command or re.search(r"\bpython\b.*\btrain\b", command) is not None


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


def _active_claim_row(root: Path, session_key: str | None):
    if not session_key:
        return None
    with connect(root) as conn:
        session = one(conn, "SELECT * FROM sessions WHERE session_key = ?", (session_key,))
        if not session or not session["claim_id"]:
            return None
        return one(conn, "SELECT * FROM claims WHERE id = ?", (session["claim_id"],))


def pre_tool_hook(root: Path, input_data: dict | None = None, session_key: str | None = None) -> tuple[int, dict]:
    input_data = input_data or {}
    resolved = resolve_session_key(input_data, session_key)
    candidate_paths = _candidate_paths(input_data)
    command = _command_text(input_data)
    shell_mutation = _is_shell_tool(input_data) and _shell_command_is_mutating(command)
    write_paths = candidate_paths if _is_write_tool(input_data) or shell_mutation else []
    if shell_mutation and not write_paths:
        write_paths = ["."]
    policy = evaluate_pre_tool_policy(
        root,
        resolved,
        is_run_start=_is_run_start_request(input_data),
        write_paths=write_paths,
        read_paths=candidate_paths if _is_read_tool(input_data) else [],
    )
    blockers = policy["blockers"]
    ok = policy["ok"]
    return (0 if ok else 2), {
        "ok": ok,
        "decision": "allow" if ok else "deny",
        "session": resolved,
        "claim": policy["snapshot"].get("claim"),
        "blockers": blockers,
    }


def post_tool_hook(root: Path, input_data: dict | None = None, session_key: str | None = None) -> tuple[int, dict]:
    input_data = input_data or {}
    resolved = resolve_session_key(input_data, session_key)
    snapshot = snapshot_session(resolved or "unknown", root)
    claim_id = snapshot.get("claim")
    payload = input_data.get("researchctl") if isinstance(input_data.get("researchctl"), dict) else input_data
    events: list[dict] = []
    try:
        run_id = payload.get("run_id") if isinstance(payload.get("run_id"), str) else None
        run_command = payload.get("run_command") or payload.get("command") or payload.get("cmd")
        artifact_root = payload.get("artifact_root") or payload.get("artifact_path")
        if claim_id and isinstance(run_command, str) and payload.get("register_run"):
            run = start_run(str(claim_id), run_command, payload.get("host", "local"), artifact_root, root)
            run_id = run["id"]
            events.append({"type": "run_started", "run_id": run_id})
        if run_id:
            for key, event_type in (
                ("artifact_path", "artifact_recorded"),
                ("metrics_path", "metrics_recorded"),
                ("error", "tool_error"),
                ("exception", "tool_error"),
            ):
                value = payload.get(key)
                if value:
                    run_event(run_id, event_type, {key: value}, root)
                    events.append({"type": event_type, "run_id": run_id})
    except ResearchCtlError as exc:
        return 1, {"ok": False, "error": str(exc), "events": events}
    return 0, {"ok": True, "session": resolved, "claim": claim_id, "events": events}


def read_hook_input() -> dict:
    try:
        value = json.loads(sys.stdin.read() or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
