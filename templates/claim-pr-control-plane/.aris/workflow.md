# Research Workflow

## Core Principles

1. Evidence loop first.
2. Claim PR is the review unit.
3. State lives outside the model.
4. This file is the workflow constitution.

## Phase Index

```text
Phase 0: no_claim
Phase 1: draft
Phase 2: gated
Phase 3: contract_frozen
Phase 4: running
Phase 5: judging
Phase 6: merged
Phase 7: archived
```

[workflow-state:no_claim]
No active claim is attached to this session.
Default next action: create or select a research anchor, gather orientation context, then draft candidate claims with `/claim-batch`.
Do not edit paper or research code directly before a claim is created, gated, and attached.
[/workflow-state:no_claim]

[workflow-state:draft]
The active claim is still a draft.
Default next action: refine CLAIM.md, run novelty review, then gate the claim.
Do not start runs before the claim is gated and CONTRACT.yaml is frozen.
[/workflow-state:draft]

[workflow-state:gated]
The active claim is approved but not executing yet.
Default next action: freeze CONTRACT.yaml with `python -m researchctl claim freeze-contract`.
Do not change success or failure signals after the contract hash is recorded.
[/workflow-state:gated]

[workflow-state:contract_frozen]
The active claim contract is frozen.
Default next action: start registered runs with `python -m researchctl run start`.
Do not edit CONTRACT.yaml or broaden the claim after this point.
[/workflow-state:contract_frozen]

[workflow-state:running]
The active claim is executing.
Default next action: monitor registered runs, collect artifacts, and update EVIDENCE.md.
Do not rewrite the claim or silently modify CONTRACT.yaml.
[/workflow-state:running]

[workflow-state:judging]
The active claim has enough evidence for a verdict.
Default next action: run claim audit, write VERDICT.yaml, then call `python -m researchctl claim finish`.
Do not write manuscript narrative before verdict.
[/workflow-state:judging]

[workflow-state:merged]
The active claim is merged into the paper input set.
Default next action: update CLAIM_MATRIX.yaml and paper build artifacts from merged claims only.
Do not let invalidated, inconclusive, killed, draft, or running claims enter the manuscript.
[/workflow-state:merged]

[workflow-state:archived]
The active claim is archived.
Default next action: preserve failure memory or handoff notes, then detach the session.
Do not revive archived claims without explicitly creating a new claim or follow-up claim.
[/workflow-state:archived]

## Wrapper Routing

| Intent | Wrapper |
|---|---|
| Start Claim-PR flow | `/claim-pr-start` |
| Start problem anchor | `/anchor-init` |
| Draft claim set | `/claim-batch` |
| Gate claim | `/claim-gate` |
| Plan and run experiments | `/claim-run` |
| Judge evidence | `/claim-verdict` |
| Merge supported claim | `/claim-merge` |
| Finish lifecycle gate | `/finish-claim` |
| Build manuscript from merged claims | `/paper-build` |
