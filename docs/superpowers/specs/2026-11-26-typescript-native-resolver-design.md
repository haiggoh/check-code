# TypeScript linting that follows the user's TypeScript (TS 7 native first) — design

Date: 2026-11-26 · Branch: `feat/ts7-native-resolver` · Base: `cb6c4b3` (0.5.3)

## Intent

check-code lints the lines Claude just edited. For TypeScript it is the ONLY TS linter the
user should need: the separate official `typescript-lsp` plugin (Claude Code's code-navigation
server) is being removed by the user because it breaks under TypeScript 7 and check-code is
meant to cover TS linting on its own. This is a product fix for every check-code user, not a
fix for one machine.

TypeScript is the one language where the "linter" IS the language: the compiler is the
checker. So the right checker is whichever TypeScript the user (or the project) actually
uses, and check-code must never force a TypeScript upgrade the user did not choose.

## Facts this design rests on (verified 2026-11-26 on the author's machine)

- TS 7 (7.0.2) is the native Go port. Its npm package ships no `lib/tsserver.js`, so
  `typescript-language-server` (tls) cannot drive it (the root cause of the plugin error).
- TS 7 has its own LSP server: `tsc --lsp --stdio`. It answers PULL diagnostics
  (`textDocument/diagnostic`; `initialize` advertises `diagnosticProvider`) and sends NO push
  `publishDiagnostics` for an opened file. On `scripts/probe/bad.ts` it returns exactly the
  planted `2322` on line 5, in ~0.5 s total.
- npm's TS 7 `bin/tsc` is a `#!/usr/bin/env node` shim launching a native binary under
  `node_modules/@typescript/typescript-<platform>-<arch>/`. brew's `typescript` formula is
  7.0.2 with no dependencies (native, no node).
- brew's `typescript-language-server` (6.1.0) DEPENDS on brew `typescript`, i.e. installing it
  drags a global TS 7 onto PATH.
- Today (0.5.3) check-code installs brew tls + brew typescript + a private `typescript@6`
  under `$CHECK_CODE_HOME/tsserver`; `scripts/tslsp-lint.py` drives tls with that lib.
- Every other language: check-code installs only the LINTER (ruff via pipx; shellcheck,
  biome, rubocop, swiftformat, actionlint via brew), never the language runtime; each catalog
  entry names exactly ONE manager, and a missing manager means a "run it yourself" notice. No
  install ever uses sudo.
- Latent bug found while probing: `tslsp-lint.py` waits with `selectors` on a BUFFERED
  `Popen` stdout and reads with `readline()`. Data already pulled into Python's buffer is
  invisible to `select()`, so a second message that arrived in the same chunk can stall the
  loop until timeout. A probe written the same way hung 20 s against TS 7; `bufsize=0` fixed it.

## Goals

1. Per edited file, lint with the right TypeScript, resolved in this order:
   1. **The project's own TypeScript** — nearest `node_modules/typescript` walking up from the
      file. Its major version picks the driver (≥ 7 native, ≤ 6 tls driving THAT lib).
   2. **TS 7 anywhere on the machine** — `tsgo` on PATH, `tsc` on PATH reporting 7.x, the npm
      global root's `typescript` at 7.x, then check-code's private TS 7. Always invoked by
      ABSOLUTE path; nobody's PATH is ever edited.
   3. **A global TS ≤ 6 with a complete lib** (`tsserver.js` + `typescript.js`) → tls.
   4. **Nothing usable** → exit 2 with a hint naming the fix (loud, never silent).
2. Two drivers behind one runner: the existing tls driver (push or `tsserverRequest`) and a
   new native driver (`tsc --lsp --stdio`, pull diagnostics). Both print the same
   `publishDiagnostics`-shaped JSON line, so `_tslsp_parse` stays the single parser.
3. Install routes become a LIST tried in order, generic for the whole catalog: a linter may
   declare several routes; the first whose manager exists is used. No route ever uses sudo
   or needs admin. (Today's single-`manager` entries are a list of one; behaviour unchanged.)
4. Default install for a user with NO usable TypeScript: **TS 7**.
   - brew available AND no `tsc` on PATH → `brew install typescript` (nothing to clash with).
   - otherwise → `npm install --prefix $CHECK_CODE_HOME/ts typescript@7` (private; the user's
     `tsc` stays exactly as it is).
5. tls is installed ONLY when a TS ≤ 6 setup is actually met, and ALWAYS privately via npm
   (`npm install --prefix $CHECK_CODE_HOME/ts typescript-language-server`), so it can never
   drag brew's TS 7 onto PATH. A one-line notice says why it is being installed.
6. The private `typescript@6` (`$CHECK_CODE_HOME/tsserver`) is no longer installed: tls drives
   the user's/project's own TS ≤ 6. Existing copies are left in place, unused unless the
   `CHECK_CODE_TSSERVER_LIB` override points at them (no silent deletion of user-visible state).
7. Fix the buffered-pipe stall in the shared LSP framing (unbuffered stdout or `os.read`-based
   framing) for both drivers.

## Non-goals

- Registering an LSP server with Claude Code for code navigation (the `LSP` tool,
  go-to-definition). The user needs linting only; the typescript-lsp plugin is removed, not
  replaced.
- Installing Node. Node is a language runtime; like Python for ruff, check-code does not
  install it. Without node, the npm routes are unavailable and the brew route (native TS 7, no
  node) is the only automatic one.
- Changing biome (already a separate TS/JS linter, unaffected).
- Editing anyone's PATH or shell config, or adding a PATH shim.

## Anti-goals (would be failure even if it "works")

- Upgrading or shadowing the user's own `tsc` (e.g. a brew TS 7 linking over an npm-global
  TS 6 `tsc`).
- Linting a TS ≤ 6 project with TS 7 *silently* when the project pins an older compiler —
  results would disagree with the project's own build.
- A "ready" verdict that is only "a binary exists" (the 0.5.3 present-but-broken lesson): the
  functional probe on `scripts/probe/bad.ts` (expect `2322`) stays the readiness test, run
  through whatever the resolver picks.
- Any install requiring sudo/admin.

## Approaches considered

- **A. Two drivers + per-file resolver (CHOSEN by the user).** Right compiler for every
  setup; zero downloads when TS 7 exists. Cost: two drivers to maintain and test.
- **B. Always a private TS 7, drop tls.** Simplest, but lints TS ≤ 6 projects with a
  different compiler and pushes TS 7 onto machines that never chose it. Rejected.
- **C. A PATH shim exposing TS 7 beside TS 6.** Both ship a `tsc` binary, so one must shadow
  the other, and check-code would be silently changing users' shells; absolute-path
  invocation already solves "installed but not on PATH". Rejected.

## Decisions made with the user

- Approach A.
- Lint only; the official `typescript-lsp` plugin will be uninstalled (user action, after
  this ships).
- Default for no-TypeScript users: TS 7.
- brew preferred when available, npm otherwise; every route must work without admin/sudo.
- brew for TS 7 only when no `tsc` is on PATH; otherwise private npm. tls always private npm.

## Builder's calls (decided by the author, open to correction)

- **TS ≤ 6 project but tls not yet installed:** lint with TS 7 if one resolves, prefix the
  findings with a one-time note "linted with TS 7; project pins TS N — installing the TS N
  driver", and queue the private tls install in the existing background `sync --worker`.
  Rationale: some checking now beats none; the note keeps it non-silent (anti-goal above).
  Without any TS 7: exit 2 with the hint, as today.
- **Private TS 7 vs user's global TS 7:** the user's wins (goal 1.2 order); the private copy
  is the last TS 7 candidate.
- **Native binary vs npm shim:** prefer the native platform binary under
  `@typescript/typescript-<platform>-<arch>/lib/` when present (no node needed at lint time);
  fall back to the package's `bin/tsc`. The exact native file name is confirmed during
  implementation, not assumed.
- **Catalog id:** keep `typescript-lsp` as the id (users' config/state reference it); its
  `binary` check becomes "the resolver finds a usable TypeScript", since there is no longer a
  single binary.
- **LSP timeout:** keep `LSP_TIMEOUT = 60` for the tls path; the native path measured 0.5 s
  and needs no change.

## Left to the builder (the plan)

- Where the resolver lives (`tslsp-lint.py` vs a shared module imported by both it and
  `check-code.py`'s readiness check) — one implementation, both callers.
- The exact catalog schema for multi-route installs (`install: [{manager, cmd, extra,
  when}]`) and its back-compat with `linters.local.json` single-manager entries.
- How `setup` / `sync` describe the chosen route and the "why tls" notice.
- Tests: resolver order with fake trees (project TS 5/6/7, PATH tsgo/tsc 6/7, npm-global,
  private, none), both drivers against fake servers, the stall regression, multi-route
  selection (brew present/absent, npm present/absent, `tsc` on PATH or not), and the
  real-machine probe per driver. Mutation-check the resolver order.
- `portable/` regeneration (`scripts/build-portable.py`), README/CHANGELOG/`--help` texts,
  version bump to 0.6.0 (new behaviour + catalog schema change).
