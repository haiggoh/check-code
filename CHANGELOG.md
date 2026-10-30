# Changelog

## [0.3.3] - 2026-10-30

- NO-HELP no longer fires on files nobody runs by hand: pytest-collected tests (a `test_*.py` /
  `*_test.py` name, or any file under a `tests/` directory) and sourced libraries (a header in the
  first 15 lines saying "sourced by" / "not executable on its own"). The sourced-library claim is
  read from the header only, so a runnable script that mentions it deep in the body is still flagged.

## [0.3.2] - 2026-10-29

- Tightened from the independent final review (DeepSeek V4.1 Flash dispatch): the findings
  header now has an exact-string pin on the formatter seam — mutation-checked, the reworded
  body fails it while the earlier prefix assertion let a rewording through — and the SKILL.md
  branding line is scoped to turns where the findings report actually fired, not every reply
  under the skill (no mark on an all-clear turn).

## [0.3.1] - 2026-10-28

- Branding: the findings report now leads with the 🩻 check-code identity mark — its credit
  moment, the instant it just caught something for you. Static surfaces carry the About-section
  pair 🩻🔬 (plugin.json description, README, skill attribution); the marketplace catalog sync
  rides the get-haiggoh batch ship (plan Task 22). Emit format otherwise unchanged; the
  missing-linter notice and clean-edit silence stay unbranded, per the credit-moments rule.

## [0.3.0] - 2026-10-26

- Three new built-in checks (touched lines only, never blocking, no linter needed):
  - `HARDCODED-PATH`: a touched line in a file inside a git repo carrying a literal
    `/Users/<name>/` home path, a `ClaudeWorkspace/<name>` checkout path, or a pinned
    plugin-cache VERSION path (`plugins/cache/<owner>/<name>/1.2.3`) — the machine-specific
    shapes the portable-repos rule bans from committed content. `/Users/Shared/` is fine;
    `$TMP`/`tmp_path`-built test-fixture lines and CHANGELOG files are exempt.
  - `SKILL-BUDGET`: a Write/Edit of a `SKILL.md` whose body after frontmatter exceeds 1500
    words (`CHECK_CODE_SKILL_BUDGET_WORDS` overrides; an override that is set but invalid is
    reported, never silently ignored). Fires once per file per session, and file-level: the
    touched-lines filter does not silence it.
  - `MENU-NAV`: a touched menu row that gives NAVIGATION a number (`echo "5) Exit"`) — house
    menu grammar is numbers = actions, letters = navigation (`q` Quit, `b` Back).
- Scope decided by replay over 217 real Edit/Write events (4,270 added lines) from the ten
  most recent transcripts: hits on /tmp scripts and ~/.claude notes (no repo, no reader to
  be wrong for) motivated the git-repo guard; hits in the check's own test fixtures are
  intentional literals; the remaining catches included real expanded-home and
  checkout-coupled paths in committed docs from concurrent sessions.

## 0.2.0 — 2026-10-25

- Two built-in checks that need no external linter, following the same touched-lines and
  never-blocks contracts:
- `STDERR-PIPE` flags a Python `Popen`/`call`/`check_call` on a touched line with
  `stderr=PIPE` that nothing in the file drains — an unread pipe deadlocks the child once
  ~64KB of output fills the OS buffer, and pacing/retry do not help (`subprocess.run` and
  `check_output` drain internally and are never flagged).
- `NO-HELP` flags a `Write` of a new script file with no visible argument parsing; its
  message says to verify by RUNNING `<script> --help`, because a source grep is wrong in
  both directions: argparse supplies `-h/--help` with the string nowhere in the source,
  while a script that ignores `--help` runs its real work on the probe. Fires once per
  file per session; a plain `.py` module (no shebang, no `__main__`) is not a script and
  stays quiet.
- Built-ins also run when no external linter matches the file, so a new zsh script gets the
  `NO-HELP` check even though shellcheck does not support zsh. `doctor` lists the built-ins.

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
