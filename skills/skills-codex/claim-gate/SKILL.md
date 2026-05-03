---
name: claim-gate
description: "Approve, revise, or kill a draft Claim-PR before experiments begin."
argument-hint: [claim-id]
allowed-tools: Bash(*), Read, Edit, Skill(novelty-check), Skill(research-review)
---

# Claim Gate

Gate claim: **$ARGUMENTS**

## Workflow

1. Load the current claim snapshot first:
   ```bash
   python -m researchctl snapshot claim "$ARGUMENTS"
   ```
2. Inspect the claim object:
   ```bash
   python -m researchctl claim show "$ARGUMENTS"
   ```
3. Run `/novelty-check` or `/research-review` against `CLAIM.md`, `NOVELTY.md`, and `CONTRACT.yaml`.
4. Write the gate decision back through `researchctl`; never edit `.aris/state.db` directly.
5. If the claim is clear, falsifiable, and worth running:
   ```bash
   python -m researchctl claim gate "$ARGUMENTS" --decision approve
   python -m researchctl claim freeze-contract "$ARGUMENTS"
   ```
6. If it is not worth running:
   ```bash
   python -m researchctl claim gate "$ARGUMENTS" --decision reject
   ```
