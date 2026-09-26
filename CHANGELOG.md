# Changelog

## 0.1.0 — 2026-09-26

- First release. A PostToolUse hook on Edit/Write/MultiEdit lints the edited file and reports
  findings on the touched lines only (the whole file for Write), capped at 10.
- Built-in linters: ruff (Python) and shellcheck (sh/bash/ksh, found by extension or shebang).
  The repo's config wins. Without one, a pinned conservative rule set applies.
- Missing-linter notice once per session; fails open; `CHECK_CODE_OFF` switch.
- User linters via `~/.claude/check-code/linters.local.json` (gcc-style output).
- CLI: `hook`, `check FILE`, `doctor`, with `--help`.
