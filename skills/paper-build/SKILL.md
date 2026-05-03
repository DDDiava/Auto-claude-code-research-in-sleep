---
name: paper-build
description: "Build manuscript inputs from merged Claim-PR objects only."
argument-hint: [paper-or-claim-matrix]
allowed-tools: Bash(*), Read, Write, Edit, Skill(paper-plan), Skill(paper-writing), Skill(paper-claim-audit), Skill(citation-audit)
---

# Paper Build

Build paper from merged claims: **$ARGUMENTS**

## Workflow

1. Generate the writing context from paper gate files and merged claim inputs:
   ```bash
   python -m researchctl context writing --write
   ```
2. Load the paper snapshot:
   ```bash
   python -m researchctl snapshot paper
   ```
3. Audit paper inputs:
   ```bash
   python -m researchctl audit paper-build
   ```
4. If clean, run:
   ```bash
   python -m researchctl paper build
   ```
5. Invoke `/paper-plan` and `/paper-writing` only from `.aris/paper/CLAIM_MATRIX.yaml`, `.aris/paper/CITATION_LEDGER.json`, and merged claim inputs in the generated writing context; do not scan running or invalidated claim directories.
6. Run `/paper-claim-audit`, `/citation-audit`, and `python -m researchctl audit submission` before submission.
