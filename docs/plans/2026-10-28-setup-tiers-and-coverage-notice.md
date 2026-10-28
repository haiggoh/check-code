# check-code 0.4.0: a setup route with coverage tiers, and a quiet coverage notice

Owner decision, 2026-10-28 (verbatim): *"plan it and use /free-session-handover to a max effort glm
session for building it. keep in mind that your design shouldn't be noisy and annoying to the user.
we can also add an additional setup route that would automatically install missing dependencies
maybe even choose interactively how many languages are installed (basic=shellcheck&ruff,
polyglot=several more languages for broader coverage)? this setup route should be preferred to being
nagged to install something on demand when trying to check code and only then realizing the package
is missing, that's a bad ux"*

## Problem (measured 2026-10-28)

1. **check-code ships no linters.** ruff and shellcheck are external tools. A public user who installs
   the plugin without them gets a once-per-session "X is not installed (install cmd)" line, but only
   for languages that have a built-in linter, and only *after* they edit such a file.
2. **Languages with no linter configured are silently unchecked**: TS/JS, Swift, Ruby, Rust (our
   repos: echoprism, claude-turn-speak, mac-iphone-backup-cloudsync, homebrew-tap). No notice at all.
3. Discovering a missing dependency mid-edit is the bad UX the owner names. The fix is a deliberate,
   up-front **setup route**, not more on-demand messages.

## Design

### A. `check-code.py setup` — the preferred route

```
check-code.py setup                    interactive when stdin is a TTY: choose a tier, see the plan, confirm
check-code.py setup --tier basic|polyglot|none [--yes] [--dry-run]
```

- **Tiers** (data, not code — the catalog in B):
  - `basic` = ruff + shellcheck (today's built-ins).
  - `polyglot` = basic + every catalog linter that lints a SINGLE FILE with no per-repo config.
    Candidates, each to be VERIFIED by running the real tool on a scratch file (format + exit code)
    before it enters the catalog: **biome** (TS/JS/JSON, zero-config, one binary, `brew install biome`),
    **swiftlint** (Swift, `brew install swiftlint`), **rubocop** (Ruby, `gem install rubocop`, run with a
    pinned conservative cop set, as ruff is), **actionlint** (GitHub workflow YAML, `brew install actionlint`).
    **Rust is deliberately absent**: clippy lints a crate, not a file, so it cannot honour
    "only the lines you touched". Say so in the README instead of shipping a fake per-file Rust check.
  - `none` = an explicit "I don't want external linters": built-in checks still run, and every
    missing-linter notice is silenced permanently.
- **Interactive UX** follows the house rules: the question first and at full emphasis, selectable
  choices, numbers = actions, letters = navigation (`q` quit), Enter = the shown default (`basic`).
  Show the PLAN before acting: for each linter, already-installed / will-install (exact command) /
  cannot-install-here (why, plus the command to run yourself).
- **Install**, only after confirmation (`--yes` for scripts), using the package manager the entry names
  (brew, pipx, npm, gem). Never `sudo`, never `pip install --break-system-packages`. A missing package
  manager is not a failure of setup: print the command and continue. **Verify by outcome**: after each
  install re-resolve the binary; report installed / already present / failed with the reason.
- **Record** the chosen tier in `$CHECK_CODE_HOME/config.json` (`{"tier": "polyglot"}`); re-running
  setup is idempotent and may change the tier.
- `--dry-run` prints the plan and changes nothing (parse arguments BEFORE any side effect; an unknown
  flag or tier exits 2 with a usage line on stderr).

### B. The linter catalog

One table (the existing `BUILTIN` list, extended) where each entry has: id, language, extensions /
shebangs, binary, command template, parser, `tier`, and `install` per package manager. The existing
`linters.local.json` keeps working and wins over the catalog (user linters are always "in tier").

### C. The coverage notice — quiet by construction

Rules, in order; the first that applies decides:

1. A file type no catalog entry covers (`.md`, `.json`, `.txt`, ...) → **silent, always**.
2. Tier `none`, or the language is outside the recorded tier → **silent** (the user chose).
3. **No setup ever recorded** and the file's linter is missing → ONE line, at most **once per session in
   total** (not per language, not per file):
   `check-code: .ts edits are not linted — run "check-code.py setup" once to choose coverage (or "setup --tier none" to silence this).`
4. The tier includes the linter but it is missing (uninstalled since setup) → one line, once per session
   per linter, pointing at `setup`, not at a per-tool install command.

Never a second line in the same hook output for the notice; never a notice on a clean, covered edit.
This REPLACES today's per-linter "not installed (install cmd)" message.

### D. doctor

Shows the recorded tier (or "setup never run"), then per catalog linter: installed path / missing /
not in tier. Exit status unchanged in meaning.

### E. Docs

README "Install" leads with `check-code.py setup` and a table of tiers and what each installs; the
Linters table gains the polyglot rows and the Rust note. CHANGELOG 0.4.0. Version bumped on every
surface (`.claude-plugin/plugin.json`, CHANGELOG; grep for any other).

## Tests (TDD; hermetic)

- Every test pins `CHECK_CODE_HOME` to a tmp dir and builds a PATH of stub binaries; **no test ever runs a
  real installer**: install commands go through one injectable runner that the tests replace.
- Notice rules C1–C4 each get a test, plus: once-per-session-total across two languages; `tier none`
  silences; a clean covered edit prints nothing. Mutation-check the once-per-session guard (delete it ⇒ red).
- setup: `--dry-run` writes nothing (assert the config file is absent and the runner was never called);
  unknown flag/tier ⇒ exit 2; post-install verification reports a stub install that "succeeds" but leaves
  no binary as FAILED (planted positive).
- Each new catalog parser is tested against REAL output captured from the real tool (installing the tool
  locally to capture it is fine), committed as a fixture.

## Out of scope

Rust per-file linting; new hooks (no SessionStart notice, nothing beyond the existing PostToolUse);
installing anything without explicit confirmation; changing built-in checks.

## Verification record (2026-10-28, build session)

All four candidates were installed and run on scratch files; **all four PASS** and enter the catalog.
Real outputs are committed under `tests/fixtures/`. Verified commands and the decisions behind them:

| linter | verified command | output |
|---|---|---|
| biome 2.6.3 | `biome lint --reporter=checkstyle FILE` | checkstyle XML on stdout (pure — the run summary is stderr). `lint`, not `check`: `check` adds a line-0 "Formatter would have printed…" complaint that cannot honour touched-lines filtering. Zero-config; a repo `biome.json` wins. |
| swiftlint 0.65.1 | `swiftlint lint --no-cache FILE` | gcc-style `path:line:col: severity: message (rule)` on stdout (preamble is stderr); the existing `_gcc_parse` reads it natively. 0.65 removed `--path` — paths are positional. `--no-cache` keeps the repo clean. |
| rubocop 1.92.1 | `rubocop --lint --format json FILE` | pure JSON on stdout (the pending-cops advisory is stderr-only). `--lint` = the Lint department: the pinned conservative set of error-class cops, the ruff analogy. Install is **`brew install rubocop`, NOT `gem install`**: Homebrew ruby's gem bin dir is off PATH, so setup's verify-by-outcome would report FAILED forever; the brew formula lands in `/opt/homebrew/bin`. |
| actionlint 1.7.12 | `actionlint -format '<printf/println template>' FILE` | pure `file:line:col: message [rule]` lines. The template is required: default output interleaves code-snippet blocks **on stdout**, and a snippet source line containing `foo:123:` would itself match `_gcc_parse` and fake a finding. Rules are in `[square brackets]`, so it gets a small dedicated parser. |

Decisions taken during verification:

- **biome covers TS/JS only** (`ts,tsx,js,jsx`), not JSON: notice rule C1 names `.json` as always-silent,
  so no catalog entry may claim it.
- **No shebang matchers for the new entries** — verified biome silently lints nothing on a
  shebang-only file (empty XML despite a `debugger` on line 2); ext-only matches what was verified.
- **actionlint matches by path** (`\.github/workflows/.*\.ya?ml$`), never by extension — verified it
  "lints" a docker-compose file as a broken workflow (3 false errors), so `yml`/`yaml` extension
  matching would misfire on every non-workflow YAML edit.
- Rust stays absent (design §A): clippy lints a crate, not a file — README says so instead of
  shipping a fake per-file check.

