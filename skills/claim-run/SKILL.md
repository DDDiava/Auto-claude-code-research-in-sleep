---
name: claim-run
description: "Plan, launch, monitor, and register experiments for a frozen Claim-PR."
argument-hint: [claim-id]
allowed-tools: Bash(*), Read, Write, Edit, Skill(experiment-plan), Skill(experiment-bridge), Skill(experiment-queue), Skill(run-experiment), Skill(monitor-experiment)
---

# Claim Run

Execute claim: **$ARGUMENTS**

## Workflow

1. Generate the experiment context before using donor skills:
   ```bash
   python -m researchctl context experiment --claim "$ARGUMENTS" --write
   ```
2. Confirm snapshot and contract state:
   ```bash
   python -m researchctl snapshot claim "$ARGUMENTS"
   ```
3. Use `/experiment-plan` and `/experiment-bridge` against the frozen `CONTRACT.yaml`.
4. Register every launched run through the truth layer:
   ```bash
   python -m researchctl run start --claim "$ARGUMENTS" --cmd "your command"
   ```
5. Finish or fail runs through `researchctl`; do not leave run state only in logs:
   ```bash
   python -m researchctl run finish --run "$RUN_ID" --status success
   python -m researchctl run fail --run "$RUN_ID" --status crashed
   ```
6. Update `EVIDENCE.md` from registered run artifacts.
7. After donor skills produce artifacts or metrics, write them back with `researchctl run event`; never edit `.aris/state.db` directly.
