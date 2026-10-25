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

## The built-in codes

- `STDERR-PIPE` — the edited Python line spawns a subprocess with `stderr=subprocess.PIPE`
  and nothing in the file reads that pipe. The child deadlocks once ~64KB of stderr fills
  the OS pipe buffer; pacing and retry do not help, it is output volume. Fix in the same
  turn: `proc.communicate()`, a stderr reader, or sink it (`stderr=subprocess.DEVNULL`, a
  log file). `subprocess.run` is safe — it drains internally.
- `NO-HELP` — the Write created a script file with no visible argument parsing. If it takes
  arguments, parse them before any work, exit non-zero on an unknown flag, and support
  `--help`. Then verify by **running** `<script> --help`: a source grep proves nothing in
  either direction (argparse supplies `-h/--help` with the string nowhere in the source;
  a script that ignores `--help` runs its real work on the probe). If the script
  deliberately takes no arguments, say so in one clause and move on.

## During a bulk fix-sweep

When you are fixing one error class across many files, the class you are removing is the one you
are most likely to write again — in a new file especially, which gets less scrutiny than the
broken files around it. (Observed: a sweep fixing a too-shallow import path wrote that same path
into a newly created file; check-code caught it the same turn.)

- **Keep check-code on for the whole sweep.** Don't set `CHECK_CODE_OFF`, batch the findings for
  later, or skim past them to go faster. During a sweep it also catches your own new instances.
- **After writing a new file, read its findings before moving to the next file.** A Write is
  checked in full, so a new file is where a repeat of the class shows up.

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
