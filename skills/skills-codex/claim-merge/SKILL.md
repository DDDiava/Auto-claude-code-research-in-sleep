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
2. Run the finish gate:
   ```bash
   python -m researchctl claim finish "$ARGUMENTS"
   ```
3. Merge only if the verdict is `supported` or `partial_supported`:
   ```bash
   python -m researchctl claim merge "$ARGUMENTS"
   ```
4. Fill `CLAIM_MATRIX.yaml` provenance fields, including `support_runs`, `figure_table_refs`, and `allowed_scope`.
5. Confirm paper inputs:
   ```bash
   python -m researchctl audit paper-build
   ```
