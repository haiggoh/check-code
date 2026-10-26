# check-code

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
- **Use what's installed, and say when something is missing.** Linters that aren't installed are
  skipped. If a file type has a linter but it's missing, check-code says so once per session with
  the install command, rather than doing nothing without a word.
- **Never block, never cost.** Findings are context for Claude, not a gate. A clean edit produces
  no output and no tokens. Any error (timeout, crash, bad input) means no output and exit 0.

## Linters

| Language | Linter | Picked by | Default rules (no repo config) |
|---|---|---|---|
| Python | `ruff check` | `.py`, `.pyi`, python shebang | `E4,E7,E9,F,B,E722`: errors, pyflakes, bugbear, bare except |
| Shell | `shellcheck` | `.sh`, `.bash`, `.ksh`, sh/bash shebang | all except `SC1091,SC2016,SC2034` |
| anything | yours | `linters.local.json` | whatever the command does |

zsh isn't checked, because shellcheck doesn't support it.

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
  doctor        which linters are installed
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

Requires `python3` 3.9+, plus the linters you want: `pipx install ruff`, `brew install shellcheck`.

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
