#!/usr/bin/env python3
"""
Bootstrap a Claim-PR Auto Paper Loop workspace from an ARIS fork.

This script copies files. It never creates symlinks, junctions, or hard links.

Typical Windows usage:
  python bootstrap_claim_pr_project.py ^
    --aris D:/repos/Auto-claude-code-research-in-sleep ^
    --project D:/auto-paper-projects/paper-1 ^
    --platform both

After bootstrap:
  cd D:/auto-paper-projects/paper-1
  claude    # or codex
  /claim-pr-start "your research direction"
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


EXCLUDE_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "target",
}

EXCLUDE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
}

MANAGED_BLOCK_BEGIN = "<!-- CLAIM-PR-BOOTSTRAP:BEGIN -->"
MANAGED_BLOCK_END = "<!-- CLAIM-PR-BOOTSTRAP:END -->"


@dataclass
class CopyStats:
    copied_files: int = 0
    copied_dirs: int = 0
    skipped: int = 0
    backed_up: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "CopyStats") -> None:
        self.copied_files += other.copied_files
        self.copied_dirs += other.copied_dirs
        self.skipped += other.skipped
        self.backed_up += other.backed_up
        self.errors.extend(other.errors)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def norm(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_DIR_NAMES:
        return True
    if path.is_file() and path.suffix in EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def same_file(a: Path, b: Path) -> bool:
    if not a.is_file() or not b.is_file():
        return False
    try:
        return a.stat().st_size == b.stat().st_size and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def backup_path(path: Path, stamp: str) -> Path:
    return path.with_name(f"{path.name}.bak-{stamp}")


def backup_existing(path: Path, stamp: str) -> None:
    dest = backup_path(path, stamp)
    counter = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    path.rename(dest)


def copy_file(src: Path, dst: Path, *, force: bool, backup: bool, stamp: str, stats: CopyStats) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if same_file(src, dst):
            stats.skipped += 1
            return
        if force:
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        elif backup:
            backup_existing(dst, stamp)
            stats.backed_up += 1
        else:
            stats.skipped += 1
            return
    shutil.copy2(src, dst)
    stats.copied_files += 1


def copy_tree_contents(src_dir: Path, dst_dir: Path, *, force: bool, backup: bool, stamp: str) -> CopyStats:
    stats = CopyStats()
    if not src_dir.exists():
        stats.errors.append(f"source does not exist: {src_dir}")
        return stats
    dst_dir.mkdir(parents=True, exist_ok=True)
    stats.copied_dirs += 1

    for src in src_dir.iterdir():
        if should_skip(src):
            stats.skipped += 1
            continue
        dst = dst_dir / src.name
        try:
            if src.is_dir():
                child_stats = copy_tree_contents(src, dst, force=force, backup=backup, stamp=stamp)
                stats.merge(child_stats)
            elif src.is_file():
                copy_file(src, dst, force=force, backup=backup, stamp=stamp, stats=stats)
            else:
                stats.skipped += 1
        except Exception as exc:  # noqa: BLE001 - bootstrap should collect all copy failures
            stats.errors.append(f"failed to copy {src} -> {dst}: {exc}")
    return stats


def copy_dir(src_dir: Path, dst_dir: Path, *, force: bool, backup: bool, stamp: str) -> CopyStats:
    """Copy a directory into an exact target directory, merging children."""
    return copy_tree_contents(src_dir, dst_dir, force=force, backup=backup, stamp=stamp)


def validate_aris_repo(aris: Path) -> None:
    required = [
        aris / "researchctl",
        aris / "skills",
        aris / "templates" / "claim-pr-control-plane",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(
            "ARIS repo path does not look like the claim-pr-control-plane branch. Missing:\n"
            + "\n".join(f"  - {item}" for item in missing)
        )


def remove_demo_anchor(project: Path) -> None:
    anchors = project / ".aris" / "anchors"
    if not anchors.exists():
        return
    for item in anchors.iterdir():
        if item.is_dir() and "demo" in item.name.lower():
            shutil.rmtree(item)


def ensure_managed_block(path: Path, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    managed = f"{MANAGED_BLOCK_BEGIN}\n{block.rstrip()}\n{MANAGED_BLOCK_END}\n"
    if MANAGED_BLOCK_BEGIN in text and MANAGED_BLOCK_END in text:
        before, rest = text.split(MANAGED_BLOCK_BEGIN, 1)
        _, after = rest.split(MANAGED_BLOCK_END, 1)
        new_text = before.rstrip() + "\n\n" + managed + after.lstrip()
    else:
        sep = "\n\n" if text.strip() else ""
        new_text = text.rstrip() + sep + managed
    path.write_text(new_text, encoding="utf-8")


def write_agent_guides(project: Path, platform: str) -> None:
    block = f"""
# Claim-PR Control Plane

This project uses the ARIS Claim-PR control plane.

Core rules:
- Use `/claim-pr-start "research direction"` as the recommended entry point.
- Keep truth outside the model: `.aris/state.db`, `.aris/events/*.jsonl`, `.aris/.runtime/sessions/*.json`.
- Treat each Claim-PR as the review unit.
- Do not start experiments until `CONTRACT.yaml` is frozen through `python -m researchctl claim freeze-contract`.
- Register runs through `python -m researchctl run start/finish/event`; do not leave run state only in logs.
- Paper writing must read merged claim inputs only, especially `.aris/paper/CLAIM_MATRIX.yaml` and `.aris/paper/CITATION_LEDGER.json`.

Local platform: {platform}
Local skills are copied, not symlinked.
""".strip()
    ensure_managed_block(project / "CLAUDE.md", block)
    ensure_managed_block(project / "AGENTS.md", block)


def write_launchers(project: Path) -> None:
    ps1 = r"""
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m researchctl init
Write-Host ""
Write-Host "Claim-PR workspace is ready." -ForegroundColor Green
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  claude    # then run /claim-pr-start \"your research direction\""
Write-Host "  codex     # then run /claim-pr-start \"your research direction\""
""".lstrip()
    (project / "claim_pr_ready.ps1").write_text(ps1, encoding="utf-8")

    bat = r"""
@echo off
cd /d %~dp0
python -m researchctl init
if errorlevel 1 exit /b %errorlevel%
echo.
echo Claim-PR workspace is ready.
echo Next:
echo   claude    ^# then run /claim-pr-start "your research direction"
echo   codex     ^# then run /claim-pr-start "your research direction"
""".lstrip()
    (project / "claim_pr_ready.bat").write_text(bat, encoding="utf-8")


def copy_skills(aris: Path, project: Path, platform: str, *, force: bool, backup: bool, stamp: str) -> CopyStats:
    stats = CopyStats()
    skills_root = aris / "skills"

    # Keep a full project-local skills tree so relative references such as
    # skills/shared-references/... remain available without symlinks.
    stats.merge(copy_dir(skills_root, project / "skills", force=force, backup=backup, stamp=stamp))

    if platform in {"claude", "both"}:
        claude_skills = project / ".claude" / "skills"
        claude_skills.mkdir(parents=True, exist_ok=True)
        for item in skills_root.iterdir():
            if item.name == "skills-codex" or should_skip(item):
                continue
            if item.is_dir():
                stats.merge(copy_dir(item, claude_skills / item.name, force=force, backup=backup, stamp=stamp))

    if platform in {"codex", "both"}:
        codex_src = skills_root / "skills-codex"
        codex_dst = project / ".agents" / "skills"
        if codex_src.exists():
            stats.merge(copy_dir(codex_src, codex_dst, force=force, backup=backup, stamp=stamp))
        else:
            stats.errors.append(f"Codex skill mirror missing: {codex_src}")

    return stats


def copy_optional_tools(aris: Path, project: Path, *, force: bool, backup: bool, stamp: str) -> CopyStats:
    stats = CopyStats()
    for dirname in ("tools",):
        src = aris / dirname
        if src.exists():
            stats.merge(copy_dir(src, project / dirname, force=force, backup=backup, stamp=stamp))
    return stats


def run_researchctl_init(project: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "researchctl", "--root", str(project), "init"],
        cwd=str(project),
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def maybe_git_init(project: Path) -> tuple[int, str, str] | None:
    if (project / ".git").exists():
        return None
    git = shutil.which("git")
    if not git:
        return 127, "", "git not found"
    proc = subprocess.run([git, "init"], cwd=str(project), text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def write_manifest(project: Path, manifest: dict) -> None:
    path = project / ".aris" / "bootstrap_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap a Claim-PR paper project by copying ARIS files.")
    parser.add_argument("--aris", required=True, help="Path to the ARIS fork repository")
    parser.add_argument("--project", required=True, help="Path to the new paper project directory")
    parser.add_argument(
        "--platform",
        choices=["claude", "codex", "both"],
        default="both",
        help="Which local agent integration to install. Default: both",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite conflicting files without creating backups. Default creates backups for conflicts.",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip conflicting files instead of backing them up. Ignored when --force is set.",
    )
    parser.add_argument(
        "--keep-demo",
        action="store_true",
        help="Keep the demo A001 anchor from the template. Default removes demo anchors.",
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Do not copy skills into .claude/.agents/project skills folders.",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Do not copy ARIS tools/ into the project.",
    )
    parser.add_argument(
        "--git-init",
        action="store_true",
        help="Run git init in the project if it is not already a git repo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    aris = norm(args.aris)
    project = norm(args.project)
    validate_aris_repo(aris)

    stamp = now_stamp()
    backup = (not args.force) and (not args.skip_backup)
    project.mkdir(parents=True, exist_ok=True)

    overall = CopyStats()
    template = aris / "templates" / "claim-pr-control-plane"
    overall.merge(copy_dir(template, project, force=args.force, backup=backup, stamp=stamp))

    if not args.keep_demo:
        remove_demo_anchor(project)

    # Copy the researchctl package into the paper project so `python -m researchctl`
    # works from the project directory without PYTHONPATH or symlinks.
    overall.merge(copy_dir(aris / "researchctl", project / "researchctl", force=args.force, backup=backup, stamp=stamp))

    if not args.no_tools:
        overall.merge(copy_optional_tools(aris, project, force=args.force, backup=backup, stamp=stamp))

    if not args.no_skills:
        overall.merge(copy_skills(aris, project, args.platform, force=args.force, backup=backup, stamp=stamp))

    write_agent_guides(project, args.platform)
    write_launchers(project)

    init_code, init_out, init_err = run_researchctl_init(project)

    git_result = maybe_git_init(project) if args.git_init else None

    manifest = {
        "bootstrapped_at": datetime.now().isoformat(timespec="seconds"),
        "aris_repo": str(aris),
        "project": str(project),
        "platform": args.platform,
        "copied_files": overall.copied_files,
        "copied_dirs": overall.copied_dirs,
        "skipped": overall.skipped,
        "backed_up": overall.backed_up,
        "errors": overall.errors,
        "researchctl_init": {
            "returncode": init_code,
            "stdout": init_out,
            "stderr": init_err,
        },
        "git_init": None
        if git_result is None
        else {"returncode": git_result[0], "stdout": git_result[1], "stderr": git_result[2]},
    }
    write_manifest(project, manifest)

    print(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))
    print("\nNext steps:")
    print(f"  cd {project}")
    print("  python -m researchctl init")
    if args.platform in {"claude", "both"}:
        print("  claude    # then run: /claim-pr-start \"your research direction\"")
    if args.platform in {"codex", "both"}:
        print("  codex     # then run: /claim-pr-start \"your research direction\"")

    if overall.errors or init_code != 0:
        print("\nBootstrap completed with warnings/errors. Check .aris/bootstrap_manifest.json.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
