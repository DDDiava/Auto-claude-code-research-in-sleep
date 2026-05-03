---
name: finish-claim
description: "Run the Claim-PR lifecycle gate before merge, archive, or handoff."
argument-hint: [claim-id]
allowed-tools: Bash(*), Read
---

# Finish Claim

Finish claim: **$ARGUMENTS**

## Workflow

Load the claim snapshot, then run the lifecycle gate:

```bash
python -m researchctl snapshot claim "$ARGUMENTS"
python -m researchctl claim finish "$ARGUMENTS"
```

This gate checks:

- CONTRACT.yaml is frozen and unchanged.
- Runs are registered and no run is still active.
- EVIDENCE.md is populated.
- VERDICT.yaml matches the recorded verdict and contains non-empty risk notes, narrative scope, and next action.
- Unsupported claims cannot enter paper build inputs.
- Mergeable claims have paper input registration when required.

If the gate fails, fix the listed blockers instead of writing a narrative summary.
