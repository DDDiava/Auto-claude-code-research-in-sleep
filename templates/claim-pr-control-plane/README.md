# Claim-PR Control Plane Template

This template seeds a Trellis-inspired, file-based research control plane for AutoPaperLoop.

Copy the template files into a research project, then initialize local state:

```bash
python -m researchctl init
```

Runtime truth is kept outside the model:

- `.aris/state.db`
- `.aris/events/*.jsonl`
- `.aris/.runtime/sessions/*.json`

Those paths are ignored by `.aris/.gitignore`.
