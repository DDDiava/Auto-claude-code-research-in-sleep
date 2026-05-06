# Claim-PR Control Plane Template

This template is installed by the repo-level Claim-PR bootstrap command. Prefer the bootstrap path so `.aris`, `.codex`, `.claude`, skills, hooks, and the bootstrap manifest are installed together:

```bash
python bootstrap_claim_pr_project.py --aris /path/to/AutoPaperLoop --project /path/to/paper-project --platform both --git-init
```

After bootstrap, initialize or reconcile local state:

```bash
python -m researchctl init
```

Runtime truth is kept outside the model:

- `.aris/state.db`
- `.aris/events/*.jsonl`
- `.aris/.runtime/sessions/*.json`

Those paths are ignored by `.aris/.gitignore`.
