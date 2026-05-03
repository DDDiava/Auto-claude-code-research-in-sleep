from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import (
    ResearchCtlError,
    archive_anchor,
    archive_claim,
    attach_session,
    audit_citation,
    audit_paper_build,
    audit_submission,
    create_anchor,
    create_claim,
    detach_session,
    finish_claim,
    finish_run,
    freeze_contract,
    gate_claim,
    init_project,
    kill_claim,
    add_review,
    list_anchors,
    list_claims,
    list_reviews,
    list_runs,
    list_sessions,
    merge_claim,
    paper_block,
    paper_build,
    paper_enqueue,
    run_event,
    show_claim,
    show_session,
    snapshot_claim,
    snapshot_paper,
    snapshot_session,
    start_run,
    write_verdict,
)
from .hooks import post_tool_hook, pre_tool_hook, read_hook_input, session_start_hook, stop_hook, workflow_state_hook
from .paths import repo_root


def emit(payload, json_mode: bool = True) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchctl")
    parser.add_argument("--root", default=None, help="project root (default: current directory)")
    parser.add_argument("--version", action="version", version=f"researchctl {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    sub.add_parser("init")

    anchor = sub.add_parser("anchor")
    anchor_sub = anchor.add_subparsers(dest="command", required=True)
    p = anchor_sub.add_parser("create")
    p.add_argument("title")
    p.add_argument("--slug")
    anchor_sub.add_parser("list")
    p = anchor_sub.add_parser("show")
    p.add_argument("anchor_id")
    p = anchor_sub.add_parser("archive")
    p.add_argument("anchor_id")

    claim = sub.add_parser("claim")
    claim_sub = claim.add_subparsers(dest="command", required=True)
    p = claim_sub.add_parser("create")
    p.add_argument("anchor_id")
    p.add_argument("title")
    p.add_argument("--slug")
    p = claim_sub.add_parser("show")
    p.add_argument("claim_id")
    p = claim_sub.add_parser("list")
    p.add_argument("--status")
    p = claim_sub.add_parser("gate")
    p.add_argument("claim_id")
    p.add_argument("--decision", required=True, choices=["approve", "reject"])
    p.add_argument("--by", default="human")
    p = claim_sub.add_parser("freeze-contract")
    p.add_argument("claim_id")
    p = claim_sub.add_parser("verdict")
    p.add_argument("claim_id")
    p.add_argument("--status", required=True, choices=["supported", "partial_supported", "invalidated", "inconclusive", "killed"])
    p.add_argument("--confidence", default="medium")
    p = claim_sub.add_parser("merge")
    p.add_argument("claim_id")
    p = claim_sub.add_parser("kill")
    p.add_argument("claim_id")
    p.add_argument("--reason", required=True)
    p = claim_sub.add_parser("finish")
    p.add_argument("claim_id")
    p = claim_sub.add_parser("archive")
    p.add_argument("claim_id")

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="command", required=True)
    p = session_sub.add_parser("attach")
    p.add_argument("--session", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--role", default="builder")
    p.add_argument("--platform", default="codex")
    p.add_argument("--worktree")
    p = session_sub.add_parser("detach")
    p.add_argument("--session", required=True)
    p = session_sub.add_parser("show")
    p.add_argument("--session", required=True)
    session_sub.add_parser("list")

    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="command", required=True)
    p = run_sub.add_parser("start")
    p.add_argument("--claim", required=True)
    p.add_argument("--cmd", required=True)
    p.add_argument("--host", default="local")
    p.add_argument("--artifact-root")
    p = run_sub.add_parser("event")
    p.add_argument("--run", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--payload", default="{}")
    p = run_sub.add_parser("finish")
    p.add_argument("--run", required=True)
    p.add_argument("--status", default="success", choices=["success", "failed", "crashed"])
    p = run_sub.add_parser("fail")
    p.add_argument("--run", required=True)
    p.add_argument("--status", default="failed", choices=["failed", "crashed"])
    run_sub.add_parser("list").add_argument("--claim")

    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="command", required=True)
    p = review_sub.add_parser("add")
    p.add_argument("--claim", required=True)
    p.add_argument("--role", default="reviewer")
    p.add_argument("--decision", required=True)
    p.add_argument("--source", default="human")
    p = review_sub.add_parser("list")
    p.add_argument("--claim")

    snapshot = sub.add_parser("snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="command", required=True)
    p = snapshot_sub.add_parser("session")
    p.add_argument("--session", required=True)
    p = snapshot_sub.add_parser("claim")
    p.add_argument("--claim", required=True)
    snapshot_sub.add_parser("paper")

    paper = sub.add_parser("paper")
    paper_sub = paper.add_subparsers(dest="command", required=True)
    p = paper_sub.add_parser("enqueue")
    p.add_argument("claim_id")
    paper_sub.add_parser("build")
    paper_sub.add_parser("status")
    p = paper_sub.add_parser("block")
    p.add_argument("--reason", required=True)

    audit = sub.add_parser("audit")
    audit_sub = audit.add_subparsers(dest="command", required=True)
    p = audit_sub.add_parser("claim")
    p.add_argument("claim_id")
    audit_sub.add_parser("citation")
    audit_sub.add_parser("paper-build")
    audit_sub.add_parser("submission")

    hook = sub.add_parser("hook")
    hook_sub = hook.add_subparsers(dest="command", required=True)
    p = hook_sub.add_parser("workflow-state")
    p.add_argument("--session")
    p = hook_sub.add_parser("session-start")
    p.add_argument("--session")
    p = hook_sub.add_parser("stop")
    p.add_argument("--session")
    p = hook_sub.add_parser("pre-tool")
    p.add_argument("--session")
    p = hook_sub.add_parser("post-tool")
    p.add_argument("--session")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root(args.root)
    try:
        if args.group == "init":
            emit(init_project(root))
        elif args.group == "anchor":
            if args.command == "create":
                emit(create_anchor(args.title, args.slug, root))
            elif args.command == "list":
                emit(list_anchors(root))
            elif args.command == "show":
                rows = [row for row in list_anchors(root) if row["id"] == args.anchor_id]
                if not rows:
                    raise ResearchCtlError(f"anchor not found: {args.anchor_id}")
                emit(rows[0])
            elif args.command == "archive":
                emit(archive_anchor(args.anchor_id, root))
        elif args.group == "claim":
            if args.command == "create":
                emit(create_claim(args.anchor_id, args.title, args.slug, root))
            elif args.command == "show":
                emit(show_claim(args.claim_id, root))
            elif args.command == "list":
                emit(list_claims(args.status, root))
            elif args.command == "gate":
                emit(gate_claim(args.claim_id, args.decision, args.by, root))
            elif args.command == "freeze-contract":
                emit(freeze_contract(args.claim_id, root))
            elif args.command == "verdict":
                emit(write_verdict(args.claim_id, args.status, args.confidence, root))
            elif args.command == "merge":
                emit(merge_claim(args.claim_id, root))
            elif args.command == "kill":
                emit(kill_claim(args.claim_id, args.reason, root))
            elif args.command == "finish":
                result = finish_claim(args.claim_id, root)
                emit(result)
                return 0 if result["ok"] else 2
            elif args.command == "archive":
                emit(archive_claim(args.claim_id, root))
        elif args.group == "session":
            if args.command == "attach":
                emit(attach_session(args.session, args.claim, args.role, args.platform, args.worktree, root))
            elif args.command == "detach":
                emit(detach_session(args.session, root))
            elif args.command == "show":
                emit(show_session(args.session, root))
            elif args.command == "list":
                emit(list_sessions(root))
        elif args.group == "run":
            if args.command == "start":
                emit(start_run(args.claim, args.cmd, args.host, args.artifact_root, root))
            elif args.command == "event":
                emit(run_event(args.run, args.type, json.loads(args.payload), root))
            elif args.command == "finish":
                emit(finish_run(args.run, args.status, root))
            elif args.command == "fail":
                emit(finish_run(args.run, args.status, root))
            elif args.command == "list":
                emit(list_runs(args.claim, root))
        elif args.group == "review":
            if args.command == "add":
                emit(add_review(args.claim, args.role, args.decision, args.source, root))
            elif args.command == "list":
                emit(list_reviews(args.claim, root))
        elif args.group == "snapshot":
            if args.command == "session":
                emit(snapshot_session(args.session, root))
            elif args.command == "claim":
                emit(snapshot_claim(args.claim, root))
            elif args.command == "paper":
                emit(snapshot_paper(root))
        elif args.group == "paper":
            if args.command == "enqueue":
                emit(paper_enqueue(args.claim_id, root))
            elif args.command == "build":
                result = paper_build(root)
                emit(result)
                return 0 if result["ok"] else 2
            elif args.command == "status":
                emit(snapshot_paper(root))
            elif args.command == "block":
                emit(paper_block(args.reason, root))
        elif args.group == "audit":
            if args.command == "claim":
                result = finish_claim(args.claim_id, root, record=False)
                emit(result)
                return 0 if result["ok"] else 2
            elif args.command == "citation":
                result = audit_citation(root)
                emit(result)
                return 0 if result["ok"] else 2
            elif args.command == "paper-build":
                result = audit_paper_build(root)
                emit(result)
                return 0 if result["ok"] else 2
            elif args.command == "submission":
                result = audit_submission(root)
                emit(result)
                return 0 if result["ok"] else 2
        elif args.group == "hook":
            hook_input = read_hook_input()
            if args.command == "workflow-state":
                emit(workflow_state_hook(root, hook_input, args.session))
            elif args.command == "session-start":
                emit(session_start_hook(root, hook_input, args.session))
            elif args.command == "stop":
                code, result = stop_hook(root, hook_input, args.session)
                emit(result)
                return code
            elif args.command == "pre-tool":
                code, result = pre_tool_hook(root, hook_input, args.session)
                emit(result)
                return code
            elif args.command == "post-tool":
                code, result = post_tool_hook(root, hook_input, args.session)
                emit(result)
                return code
        return 0
    except (ResearchCtlError, json.JSONDecodeError) as exc:
        print(f"researchctl: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
