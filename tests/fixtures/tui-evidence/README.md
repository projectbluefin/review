# TUI evidence fixtures

`tests/tui_evidence_capture.py` owns its safe local fixtures and drives the
real dashboard with Textual Pilot. It writes native SVG captures, converted
PNG captures, and redacted provenance to `.cache/tui-evidence` by default.

Examples:

```bash
uv run --with-requirements image/tui/requirements.lock \
  python tests/tui_evidence_capture.py --scenario queue --size compact
uv run --with-requirements image/tui/requirements.lock \
  python tests/tui_evidence_capture.py --self-check
```

`--pty-image` is optional. When provided, the shipped image is run in the
foreground with no network and a bounded smoke input; without it, PTY evidence
is reported as skipped. Prompt bodies, tokens, and secrets are rejected before
any artifact is written.
