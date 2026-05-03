---
name: claim-pr-start
description: "Start the evidence-governed Claim-PR research control-plane flow."
argument-hint: [research-direction-or-brief]
allowed-tools: Bash(*), Read, Write, Edit, Skill(anchor-init), Skill(claim-batch), Skill(claim-gate), Skill(claim-run), Skill(claim-verdict), Skill(claim-merge), Skill(paper-build)
---

# Claim-PR Start

Start an evidence-governed research thread: **$ARGUMENTS**

## Workflow

1. Initialize the local control plane if needed:
   ```bash
   python -m researchctl init
   ```
2. Create or select the problem anchor with `/anchor-init`.
3. Draft candidate Claim-PRs with `/claim-batch`; keep each claim small enough to gate, run, judge, and merge independently.
4. For each claim, follow the hard gate sequence:
   - `/claim-gate`
   - `/claim-run`
   - `/claim-verdict`
   - `/claim-merge`
5. Build manuscript inputs only after supported or partially supported claims are merged:
   - `/paper-build`

## Compatibility

`/research-pipeline`, `/idea-discovery`, and `/auto-review-loop` remain available as legacy full-pipeline paths. Use this Claim-PR path when the work needs explicit evidence, provenance, review, and paper-input gates.
