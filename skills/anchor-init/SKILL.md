---
name: anchor-init
description: "Create or attach a Claim-PR research anchor. Use before drafting claims when starting a new evidence loop."
argument-hint: [anchor-title]
allowed-tools: Bash(*), Read, Skill(research-refine), Skill(research-lit)
---

# Anchor Init

Initialize a research anchor for: **$ARGUMENTS**

## Workflow

1. If `.aris/state.db` is absent, run:
   ```bash
   python -m researchctl init
   ```
2. Create the anchor:
   ```bash
   python -m researchctl anchor create "$ARGUMENTS"
   ```
3. Use `/research-lit` or `/research-refine` only to refine `ANCHOR.md` and `BASELINE.md`.
4. Do not start experiments from this skill. The next wrapper is `/claim-batch`.
