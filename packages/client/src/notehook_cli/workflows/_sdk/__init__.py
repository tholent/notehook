"""Packaging marker only — do not import from here.

`notehook_workflow.py` in this directory is the whole SDK and is designed to
be imported standalone (`import notehook_workflow`) with this directory on
`sys.path` (decision D4, docs/workflow-implementation-plan.md) — that's how
the runner's harness (a later PR) invokes it inside end-user workflow venvs.
This `__init__.py` exists only so `_sdk` packages cleanly as part of the
`notehook_cli` wheel; nothing in `notehook_cli` should import through it.
"""
