# Changelog

## [0.4.6] — 2026-11-19

### Added — `typescript-lsp` built-in linter (polyglot tier)
- New catalog entry driving `typescript-language-server` over stdio via `scripts/tslsp-lint.py`: one `publishDiagnostics` JSON per file, parsed by `_tslsp_parse` (0-based LSP lines → 1-based, ts code as rule name). Covers ts/tsx/js/jsx/mts/cts/mjs/cjs; install hint `brew install typescript-language-server`.
- Measured gotcha baked in: the brew typescript-language-server package ships only `cli.mjs`; its `typescript` dependency can be a stub (lib/ without `typescript.js`), which makes tsserver never start and every `tsserverRequest` answer `{"type":"noServer"}`. The runner therefore (a) locates a COMPLETE typescript lib (env `CHECK_CODE_TSSERVER_LIB`, then intelephense's bundled copy, then global) and sets `tsserver.path`, and (b) uses `semanticDiagnosticsSync` via `workspace/executeCommand` as the reliable path (publishDiagnostics stayed silent in probes). Verified live: a real type error is reported end-to-end (rc 0, code 2322).
- Tests: parser fixture, catalog dispatch (ts/tsx/js/jsx now also match typescript-lsp), tier/install data, end-to-end runner test (skipped when the server is absent). Suite: 137 passed.

## [0.4.5] — 2026-11-14

### Added — Phase 2: committed portable package (COORDINATION C1/C2)

- New `scripts/build-portable.py` generates the committed `portable/` package: same-name
  manifest, the skill, the hook, and the CLI moved `bin/check-code.py` ->
  `scripts/check-code.py` (top-level `bin/` blocks Chat/Cowork installs) with every
  `${CLAUDE_PLUGIN_ROOT}/bin/check-code.py` reference in hooks.json and SKILL.md rewritten
  to `scripts/`. Emits `portable/PORTABLE-BUILD.json` (haiggoh-portable-build/1) and
  `dist/check-code-portable.zip` with the manifest at the zip root.
- `tests/test_portable.py` pins freshness, same-name manifest, no `bin/`, no stale
  references, closure completeness, the C2 schema, and a deterministic side-effect-free zip.
- The strict-YAML glob covers `portable/skills/*/SKILL.md` too.

## [0.4.4] — 2026-11-14

### Fixed (desktop-migration FLEET Phase 1 P0: setup/check argv and result consistency)

- **`check` on a path that does not exist fails cleanly.** It used to die with a raw
  `FileNotFoundError` traceback whose unhandled-exception exit code (1) is indistinguishable
  from "findings found" — a script could not tell a typo from a lint failure. Now: one stderr
  line naming the resolved path, exit 2 (the usage-error code). The hook path was already
  safe by design (`check_edit`'s `is_file()` guard, fail-open); the CLI's explicit ask now
  says so loudly instead of crashing.
- **`check` output is ONE line-sorted report.** Builtin-check findings printed in discovery
  order while linter findings were sorted — and even sorted, the two halves printed in two
  separate loops that never interleaved (a builtin on line 9 always preceded a linter finding
  on line 3). Findings are now collected from builtins and all installed linters into one
  list and printed sorted together; a test drives a builtin at line 9 against a linter
  finding at line 3 and demands 3 first.
- **Error paths name what you typed.** A missing file: `no such file: <path>`, exit 2. An
  existing directory: `not a regular file` (it is not missing). A dangling symlink: the
  error names the symlink as given — the path is validated before `resolve()`, which used
  to map the error to a target the user never typed (review round, Gemini 3.8 Flash:
  blocker = the interleaving claim above was false and the first test mocked the linters
  away, proving only half the contract; minors = the directory/symlink messages and a test
  pinning the hook's silent fail-open on a missing file).
- `check --help` states the CLI side of the hook/CLI split ("a missing file: exit 2, unlike
  the hook"); a new test pins the hook's silent fail-open so the two contracts cannot be
  silently unified.
- Probed and left as designed: the non-TTY `setup` gate (RC 2 + the `--yes is required`
  stderr line — an earlier probe hidden by a pipe had suggested otherwise), and the
  hook/CLI split on missing files (silent for the hook, loud for the explicit CLI ask).

## [0.4.3] — 2026-11-03

### Added — 🩻🔬 branded setup menu (menu-restyle batch, user spec 2026-11-03)

- The interactive setup menu opens with a csl-style header — 🩻🔬 Check Code /
  subtitle / `=` rule — before its caption, matching the csl picker and get-haiggoh-menu.
  Pinned by `tests/test_check_code.py` (exact block + ordering), mutation-checked,
  live-fired through `setup` under a pty.

## [0.4.2] — 2026-11-03

### Fixed — HARDCODED-PATH no longer fires on plan/doc prose

- The portability rule is scoped to code: `.md`/`.txt`/`.rst` files outside `skills/` are
  exempt (plan/docs prose legitimately names this machine's local state — measured
  2026-11-03, the hook flagged a `~/.claude/plans/*.md` line; waypoint
  check-code-hardcoded-path). `skills/*.md` stays checked — it is published content.
  New `_portability_checked()` gate; changelog/fixture exemptions unchanged.
- Tests: plan/doc `.md` in a git repo exempt (non-vacuous: the fixture line carries no
  fixture marker), `skills/*/SKILL.md` still flagged, git-repo scope test moved to a `.py`
  file. Mutation-checked (exemption disabled -> the new test red). Suite: 118 passed.

## [0.4.1] — 2026-10-31

### Changed
- The plugin description (plugin.json, and the marketplace listing it feeds) is rewritten to describe
  the plugin's full current capabilities.

## [0.4.0] - 2026-10-31

- **A setup route, preferred to on-demand nagging**: `check-code.py setup` (the menu on a TTY;
  `--tier basic|polyglot|none [--yes] [--dry-run]` for scripts) installs a coverage tier up front
  and records it in `$CHECK_CODE_HOME/config.json`. **polyglot is the default, opt-out** (owner
  decision 2026-10-29): the menu's Enter and a bare `setup --yes` both choose it, and `basic` /
  `none` are opt-ins for users who know they will not touch those languages — the installs are
  small, and being prepared beats discovering a missing linter mid-edit. The plan is always shown
  before acting — per linter `already installed` / `will install` (the exact command) /
  `cannot install here` (why, plus the command to run yourself) — installs run through the package
  manager the catalog names (`brew`, `pipx`) and never `sudo`, and each install is verified **by
  outcome** (the binary is re-resolved: `installed` / `FAILED`), not by exit code. `--dry-run`
  parses arguments before any side effect and changes nothing; a missing package manager prints
  the command and continues.
- **Four new catalog linters**, each verified against the real tool before entering (real outputs
  committed as fixtures; `docs/plans/2026-10-28-setup-tiers-and-coverage-notice.md` holds the
  verification record): biome (TS/JS, `biome lint --reporter=checkstyle`), swiftlint (Swift,
  default rules), rubocop (Ruby, `--lint`: the Lint department, the pinned conservative set),
  actionlint (`.github/workflows/*.ya?ml`, matched by **path** — extension matching would lint any
  YAML as a broken workflow — with a `-format` template, because the default output interleaves
  code-snippet lines a gcc-style parser would misread). Rust is deliberately absent: clippy lints
  a crate, not a file. Tiers: `basic` = ruff + shellcheck, `polyglot` = basic + these four.
- **The quiet coverage notice** replaces the per-linter "not installed (install cmd)" line.
  Rules, first match wins: uncovered file types and languages outside the recorded tier are always
  silent; with no setup ever recorded, ONE line per session **in total** points at `setup` (or
  `--tier none` to silence); a linter the tier includes that went missing gets one line per session
  per linter, pointing at `setup` and never at a per-tool install command. A clean, covered edit
  prints nothing; tier `none` stops external linters running at all (built-in checks stay on).
- **doctor** shows the recorded tier (or "setup never run") and marks a missing out-of-tier linter
  `not in your tier` instead of `MISSING` — a basic-tier user no longer reads four false problems.
- `check-code.py --help` lists the catalog with install commands and the `setup` route; `setup
  --help` documents tiers, flags and `CHECK_CODE_HOME`.

## [0.3.3] - 2026-10-30

- NO-HELP no longer fires on files nobody runs by hand: pytest-collected tests (a `test_*.py` /
  `*_test.py` name, or any file under a `tests/` directory) and sourced libraries (a header in the
  first 15 lines saying "sourced by" / "not executable on its own"). The sourced-library claim is
  read from the header only, so a runnable script that mentions it deep in the body is still flagged.

## [0.3.2] - 2026-10-29 — 🩻 review follow-up: exact pin + scoped skill line

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
