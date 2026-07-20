"""notehook workflows: event log, manifest parsing, SDK, install config,
harness/executor, and (later) runner for the automation system.

Phase 2: the event log (`events.py`) and its wiring into the sync engine.
Phase 3a/3b: the workflow-author SDK (`_sdk/notehook_workflow.py` — pure
stdlib, ships standalone, see its module docstring) and manifest parsing
(`manifest.py`). Phase 3c: install config + workflow pairing (`installs.py`).
Phase 3d: the job harness (`harness.py`) and subprocess executor
(`executor.py`). See docs/workflow-spec.md and
docs/workflow-implementation-plan.md.
"""
