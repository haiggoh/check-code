---
name: check-code
description: "Use when a check-code line appears after an edit (\"check-code: N issue(s) in the lines you just edited ...\"), when the user asks why lint feedback appears, wants to add a linter for another language, silence a rule, or check which linters are installed."
---

# check-code

After every Edit, Write or MultiEdit, check-code lints the file with the linters already
installed and reports findings **on the lines the edit touched** (the whole file for a Write).

## Acting on a finding

- **Fix it in the same turn** before moving on. That is the point: the mistake is cheapest to
  fix while the change is still in view.
- **If it's intentional**, say so in one clause and move on. Only change the linter config to
  silence it (a `# noqa: CODE`, a `# shellcheck disable=SCxxxx`, or the repo's config) when the
  user agrees the rule doesn't fit.
- Never rewrite unrelated lines to satisfy the linter. Old debt elsewhere in the file is out of
  scope unless the user asks.
- `check-code: <language> edits are not being checked -- <tool> is not installed` means the
  file type has a linter but it's missing. Tell the user once. Installing it is their call.

## Configuration

The repo's own config always wins (`ruff.toml`, `pyproject.toml [tool.ruff]`, `.shellcheckrc`).
Without one, check-code uses a pinned conservative set: for ruff, errors, pyflakes, bugbear and bare
except; for shellcheck, everything except SC1091, SC2016 and SC2034.

Extra linters go in `~/.claude/check-code/linters.local.json`:

```json
{"linters": [{"id": "eslint", "language": "TypeScript", "ext": ["ts", "tsx", "js"],
              "command": ["eslint", "--format", "unix", "{file}"],
              "install": "npm install -g eslint"}],
 "disabled": []}
```

The command must print `file:line[:col]: message [(rule)]` lines (most linters have a
`unix`/`gcc`/`compact` format). Check the setup with
`python3 "${CLAUDE_PLUGIN_ROOT}/bin/check-code.py" doctor`, and try a file with `check <file>`.
