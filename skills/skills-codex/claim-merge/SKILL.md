---
name: claim-merge
description: "Merge a supported or partially supported Claim-PR into paper build inputs."
argument-hint: [claim-id]
allowed-tools: Bash(*), Read, Edit
---

# Claim Merge

Merge claim: **$ARGUMENTS**

## Workflow

1. Load the claim snapshot:
   ```bash
   python -m researchctl snapshot claim "$ARGUMENTS"
   ```
2. Confirm the recorded verdict is `supported` or `partial_supported`; invalidated, inconclusive, killed, draft, or running claims must not enter paper inputs.
3. Fill `.aris/paper/CLAIM_MATRIX.yaml` before merge. The entry for `$ARGUMENTS` must include:
   - `text`
   - matching `verdict`
   - terminal registered `support_runs`
   - provenance-backed `figure_table_refs`
   - meaningful `allowed_scope`
4. Run the finish gate:
   ```bash
   python -m researchctl claim finish "$ARGUMENTS"
   ```
5. Merge only after finish passes:
   ```bash
   python -m researchctl claim merge "$ARGUMENTS"
   ```
6. Confirm paper inputs:
   ```bash
   python -m researchctl audit paper-build
   ```
