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
