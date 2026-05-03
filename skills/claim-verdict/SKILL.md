---
name: claim-verdict
description: "Judge whether registered evidence supports a Claim-PR."
argument-hint: [claim-id]
allowed-tools: Bash(*), Read, Write, Edit, Skill(experiment-audit), Skill(result-to-claim)
---

# Claim Verdict

Judge claim: **$ARGUMENTS**

## Workflow

1. Load the claim snapshot:
   ```bash
   python -m researchctl snapshot claim "$ARGUMENTS"
   ```
2. Run `/experiment-audit` if evaluation code or metrics changed.
3. Run `/result-to-claim` using `CONTRACT.yaml`, `EVIDENCE.md`, and registered run artifacts.
4. Record the verdict through `researchctl`:
   ```bash
   python -m researchctl claim verdict "$ARGUMENTS" --status supported
   ```
   Valid statuses: `supported`, `partial_supported`, `invalidated`, `inconclusive`, `killed`.
5. Fill `VERDICT.yaml` with non-empty `risk_notes`, `allowed_scope`, and `next_action`; placeholder scope will be blocked.
6. Run:
   ```bash
   python -m researchctl claim finish "$ARGUMENTS"
   ```
