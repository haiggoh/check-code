# 🩻🔬 check-code

**Lint the lines Claude just edited, and hand the findings straight back.**

Linters catch real bugs, such as unquoted shell variables, swallowed exit codes and mutable default
arguments, but only if something runs them. In an agent loop nothing does: Claude edits, moves
on, and the mistake surfaces later. check-code is a `PostToolUse` hook that lints each edited
file right away and gives the results to Claude while the change is still in view:

```
check-code: 1 issue(s) in the lines you just edited in deploy.sh -- fix them now, or say why a finding is intentional:
  L14 SC2086 Double quote to prevent globbing and word splitting.
```

## Working principles

- **Only the lines you touched.** An edit is judged on what it changed, not on the file's history.
  Old debt elsewhere stays quiet, so the feedback is always actionable. A `Write` (a new or
  replaced file) is judged whole. If the edited text can't be found again, the whole file is judged.
- **The repo's config wins.** If the project has `ruff.toml`, `[tool.ruff]` in `pyproject.toml`, or
  `.shellcheckrc`, those rules apply unchanged. Without one, a pinned, conservative rule set is used.
  It is pinned because a linter's own default set can change between versions and suddenly flood you.
- **Get the linters up front.** `check-code.py setup` installs a coverage tier and records the
  choice, so a missing dependency is discovered on purpose, never mid-edit. Until setup has run
  (or when a linter recorded for coverage goes missing), check-code prints at most one quiet line
  per session pointing at `setup` — never an install command, never a repeat.
- **Never block, never cost.** Findings are context for Claude, not a gate. A clean edit produces
  no output and no tokens. Any error (timeout, crash, bad input) means no output and exit 0.

## Linters

Tier `basic` (installed by default by `setup`):

| Language | Linter | Picked by | Default rules (no repo config) |
|---|---|---|---|
| Python | `ruff check` | `.py`, `.pyi`, python shebang | `E4,E7,E9,F,B,E722`: errors, pyflakes, bugbear, bare except |
| Shell | `shellcheck` | `.sh`, `.bash`, `.ksh`, sh/bash shebang | all except `SC1091,SC2016,SC2034` |

Tier `polyglot` (add these; each verified single-file, zero-config, machine-parseable):

| Language | Linter | Picked by | Default rules (no repo config) |
|---|---|---|---|
| TS/JS | `biome lint` | `.ts`, `.tsx`, `.js`, `.jsx` | biome's recommended set (a repo `biome.json` still wins) |
| Swift | `swiftlint lint` | `.swift` | swiftlint's default rules (a repo `.swiftlint.yml` still wins) |
| Ruby | `rubocop --lint` | `.rb` | the Lint department only: error-class cops, as ruff's set is pinned |
| Actions | `actionlint` | `.github/workflows/*.ya?ml` | every check (schema, expressions, shell names, …) |

| Language | Linter | Picked by | Default rules |
|---|---|---|---|
| anything | yours | `linters.local.json` | whatever the command does |

zsh isn't checked, because shellcheck doesn't support it. **Rust isn't checked either**: clippy
lints a crate, not a file, so it can't honour "only the lines you touched" — and a fake per-file
Rust check is worse than none.

## Setup: choose the linters up front

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/check-code.py" setup                  # interactive menu
python3 "${CLAUDE_PLUGIN_ROOT}/bin/check-code.py" setup --yes            # scripted: polyglot
```

| Tier | Installs | Covers |
|---|---|---|
| `polyglot` — the default, recommended | ruff, shellcheck, biome, swiftlint, rubocop, actionlint | Python, shell, TS/JS, Swift, Ruby, GitHub Actions workflows |
| `basic` — opt in | ruff, shellcheck | Python and shell |
| `none` — opt in | nothing | built-in checks only; coverage notices stay silent |

**polyglot is the default and opting out is yours.** The installs are small and being prepared
beats discovering a missing linter mid-edit; choose `basic` or `none` only if you know you will
not touch those languages. When stdin is a TTY the menu asks first (numbers = choices,
`Enter` = the shown default `polyglot`, `q` = quit), then shows the **plan** before touching
anything: per linter, `already installed` / `will install` (the exact command) /
`cannot install here` (why, plus the command to run yourself). Installs run only after
confirmation — `--yes` for scripts (with no `--tier`, that means the polyglot default),
`--dry-run` to look without changing anything — through the package manager the catalog names
(`brew`, `pipx`); never `sudo`, never a system-package override. A missing package manager prints
the command and continues. Each install is then verified **by outcome**: the binary is
re-resolved, so the report says `installed` / `FAILED`, not whatever the installer's exit code
claimed. The chosen tier is recorded in `$CHECK_CODE_HOME/config.json`; re-running `setup` is
idempotent and may change it — `doctor` shows the current one.

## Built-in checks

Five checks ship inside the hook itself — no linter needed, nothing to install:

| Code | Fires on | What it tells you |
|---|---|---|
| `STDERR-PIPE` | a Python `Popen`/`call`/`check_call` on a touched line with `stderr=PIPE` that nothing in the file drains | an unread stderr pipe deadlocks the child once ~64KB of output accumulates: read it (`communicate()`, a stderr reader) or sink it (`DEVNULL`, a log file) |
| `NO-HELP` | a `Write` of a new script file (a shebang, or `.py` with a `__main__` guard) that shows no argument-parsing signal | if it takes arguments, parse them, support `--help`, and verify by *running* `<script> --help` — a source grep proves nothing in either direction |
| `HARDCODED-PATH` | a touched line in a file inside a git repo carrying a literal `/Users/<name>/` home path, a `ClaudeWorkspace/<name>` checkout path, or a pinned plugin-cache VERSION path | machine-specific paths in committed content are wrong for every other reader: use `~/…`/`$HOME/…`, an env override, or a self-discovery helper. `$TMP`-built fixture lines and CHANGELOGs are exempt |
| `SKILL-BUDGET` | a `SKILL.md` whose body after frontmatter exceeds 1500 words (`CHECK_CODE_SKILL_BUDGET_WORDS` overrides) | skills stay lean: imperative rules and steps in the body; incidents, rationale and worked examples move to `references/` or `docs/`. Once per file per session |
| `MENU-NAV` | a touched menu row giving NAVIGATION a number (`echo "5) Exit"`) | house menu grammar: numbers = actions, letters = navigation (`q` Quit, `b` Back), Enter = the shown default |

They follow the same rules as the linters: touched lines only, never blocking, and a clean
file costs one quick scan. `NO-HELP` fires once per file per session. Built-ins also run when
no external linter matches the file, so a new zsh script still gets the `NO-HELP` check.

### Adding a linter

`~/.claude/check-code/linters.local.json`:

```json
{"linters": [{"id": "eslint", "language": "TypeScript", "ext": ["ts", "tsx", "js"],
              "command": ["eslint", "--format", "unix", "{file}"],
              "install": "npm install -g eslint"}],
 "disabled": []}
```

The command must print `file:line[:col]: message [(rule)]` lines. Most linters offer a
`unix`, `gcc` or `compact` format. Use `disabled` to turn off a built-in by id.

## CLI

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/check-code.py" --help
  hook          PostToolUse entry point (hook JSON on stdin)
  check FILE    lint a whole file the way the hook would
  doctor        which linters are installed, and the recorded coverage tier
  setup         choose a coverage tier and install its linters (--tier/--yes/--dry-run)
```

| Env var | Effect |
|---|---|
| `CHECK_CODE_OFF=1` | disable the hook |
| `CHECK_CODE_MAX` | max findings per edit (default 10) |
| `CHECK_CODE_HOME` | config dir (default `~/.claude/check-code`) |
| `CHECK_CODE_DEBUG=1` | log hook exceptions to `$CHECK_CODE_HOME/debug.log` |
| `CHECK_CODE_SKILL_BUDGET_WORDS` | SKILL.md body word budget for `SKILL-BUDGET` (default 1500) |

## Install

Through the haiggoh marketplace (get-haiggoh), or:

```
/plugin marketplace add haiggoh/get-haiggoh
/plugin install check-code@haiggoh
```

Requires `python3` 3.9+. Then pick your coverage — one run, not per-package installs:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/check-code.py" setup --yes   # polyglot: the default
```

`polyglot` (ruff + shellcheck + biome, swiftlint, rubocop, actionlint) is the recommended
default — Python, shell, TS/JS, Swift, Ruby and GitHub Actions covered, installs kept small on
purpose. `basic` (ruff + shellcheck) is the opt-in if you know you will not touch the other
languages, and `none` deliberately runs no external linter. The hook works before this too —
see Setup for what each tier covers.

## How it relates to other review tools

check-code is the cheap, instant, deterministic layer. It runs on every edit, finds pattern-level
mistakes, makes no model calls, and so costs nothing. It doesn't replace an LLM security review, a
PR review, or a human review. Those look at the whole change and its intent, run less often, and
cost tokens.

## Tests

```
pytest tests
```

Uses the real `ruff`/`shellcheck` when they're installed and skips those cases when they aren't.

Sibling plugins: summon-skills (use the skills you have; also flags plugins whose dependencies are
missing), measure-twice, brief-agents, audit-loose-ends.
