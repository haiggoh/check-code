# Changelog

## 0.1.2 — 2026-10-16

- Skill: a "During a bulk fix-sweep" section. Keep check-code on for the whole sweep, and read a
  new file's findings before moving on: the error class being removed is the one most likely to
  be written again (observed: a sweep fixing a too-shallow import wrote the same path into a new
  file; check-code caught it the same turn).

## 0.1.1 — 2026-10-14

- Findings are labeled "as of THIS edit — a sibling edit in the same batch may already fix
  them": per-edit findings on multi-edit batches stop reading as settled defects (observed:
  F821 "undefined name" bursts fired while the import Edit was a later call in the same batch).

## 0.1.0 — 2026-09-26

- First release. A PostToolUse hook on Edit/Write/MultiEdit lints the edited file and reports
  findings on the touched lines only (the whole file for Write), capped at 10.
- Built-in linters: ruff (Python) and shellcheck (sh/bash/ksh, found by extension or shebang).
  The repo's config wins. Without one, a pinned conservative rule set applies.
- Missing-linter notice once per session; fails open; `CHECK_CODE_OFF` switch.
- User linters via `~/.claude/check-code/linters.local.json` (gcc-style output).
- CLI: `hook`, `check FILE`, `doctor`, with `--help`.
