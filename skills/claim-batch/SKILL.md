---
name: claim-batch
description: "Draft one or more falsifiable Claim-PR objects under an existing anchor."
argument-hint: [anchor-id-and-claim-direction]
allowed-tools: Bash(*), Read, Write, Edit, Skill(novelty-check), Skill(research-refine)
---

# Claim Batch

Draft Claim-PR objects for: **$ARGUMENTS**

## Workflow

1. List anchors if needed:
   ```bash
   python -m researchctl anchor list
   ```
2. Create each concrete claim:
   ```bash
   python -m researchctl claim create A001 "claim title"
   ```
3. Fill `CLAIM.md`, `NOVELTY.md`, and `CONTRACT.yaml` for each claim.
4. Keep every claim falsifiable and narrow. Do not run experiments here.
