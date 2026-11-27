# TypeScript linting that follows the user's TypeScript — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (native execution: the executor implements every task, then ONE fresh whole-branch review on the most capable model). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** check-code lints TypeScript with whichever TypeScript the project or machine actually has (TS 7 natively via `tsc --lsp --stdio`, TS ≤ 6 via `typescript-language-server`), installs TS 7 only when nothing is usable, and never touches the user's own `tsc`.

**Architecture:** A pure resolver module (`scripts/ts_resolve.py`) answers "which TypeScript, which driver" per file. The runner (`scripts/tslsp-lint.py`) dispatches to one of two LSP drivers that both emit the same `publishDiagnostics`-shaped JSON lines. `bin/check-code.py` gains generic multi-route installs, a per-file presence check through the resolver, a note channel, and a background install of the TS ≤ 6 driver when a project needs it.

**Tech Stack:** Python 3 stdlib only (matches the repo), pytest (+ xdist), LSP over stdio, brew / npm (no sudo).

**Spec:** `docs/superpowers/specs/2026-11-26-typescript-native-resolver-design.md` (read it first; this plan argues from it). Builder-check record: `…-builder-check.md` beside it.

## Global Constraints

- Never `sudo`, never admin; never edit PATH or shell config; never upgrade or shadow the user's `tsc`.
- Resolver order (spec Goals 1): `CHECK_CODE_TSSERVER_LIB` → project `node_modules/typescript` → machine TS 7 (`tsgo` on PATH, `tsc` on PATH at 7.x, npm-global `typescript` at 7.x, private `$CHECK_CODE_HOME/ts`) → global TS ≤ 6 with complete lib → error naming the fix.
- Every TS 7 invocation uses an ABSOLUTE path.
- Native binary location: `<dir>/node_modules/@typescript/typescript-<platform>-<arch>/lib/tsc` where `<dir>` is the typescript package dir (nested) or the `node_modules` that holds it (hoisted). `<platform>`: `darwin` / `linux` / `win32`; `<arch>`: `arm64` / `x64` (`platform.machine()` `x86_64`/`AMD64` → `x64`, `aarch64` → `arm64`). Fallback: the package's `bin/tsc` (node shim).
- A complete TS ≤ 6 lib = a `lib/` holding BOTH `tsserver.js` and `typescript.js`.
- Install routes for "no usable TypeScript": brew `brew install typescript` ONLY when no `tsc` is on PATH; else `npm install --prefix {home}/ts typescript@7`.
- tls (`typescript-language-server`) installs ONLY when a TS ≤ 6 setup needs it, ALWAYS `npm install --prefix {home}/ts typescript-language-server`.
- The private `typescript@6` (`{home}/tsserver`) is no longer installed; existing copies are left on disk, used only via `CHECK_CODE_TSSERVER_LIB`.
- Reported diagnostics: severity 1 and 2 only (3/4 dropped), filtering in ONE place (`_tslsp_parse`). Syntax errors kept; tls driver also requests `syntacticDiagnosticsSync`.
- `CHECK_CODE_TSSERVER_LIB` set but invalid → exit 2 naming the variable; never a fall-through.
- `LSP_TIMEOUT = 60` unchanged for both drivers.
- Catalog id stays `typescript-lsp`.
- Version 0.6.0 in `bin/check-code.py` `VERSION`, `.claude-plugin/plugin.json`, `CHANGELOG.md` (and whatever `build-portable.py` regenerates).
- No PEP 604 (`X | None`) evaluated at runtime: use `from __future__ import annotations` in new modules or no annotations, matching file style.

## Review Focus

1. **A path with spaces / non-ASCII in the project root** → URI encoding and resolver walk still work (the 0.5.0 `_file_uri` lesson) — test in Task 2 (resolver walk on a `my proj/` tree) and Task 3 (native driver URI).
2. **A TS 7 `tsc` on PATH whose package has NO platform binary** (optional dependency skipped by `npm --no-optional`) → falls back to `bin/tsc`, never "no TypeScript" — test in Task 2.
3. **A symlinked `tsc` on PATH (brew/npm link)** → resolved through `realpath` to the package, version read from its `package.json`, not from the link's dir — test in Task 2.
4. **The server dies mid-session or answers garbage** → exit 2 with the stderr tail, never an empty "clean" — test in Task 3 (fake server exits after `initialize`).
5. **Two edits racing on `tls-needed.json`** → atomic write (temp + `os.replace`), a corrupt file reads as empty, never crashes the hook — test in Task 4.

---

## File Structure

- Create `scripts/ts_resolve.py` — the resolver: pure functions over the filesystem + injectable `which`. No LSP, no installs.
- Modify `scripts/tslsp-lint.py` — CLI runner: unbuffered framing, two drivers (`_run_native`, `_run_tls`), note line, `tls-needed.json` write. Drops `_tsserver_lib` / `_FALLBACK_LIB_DIRS` (moved into the resolver).
- Modify `bin/check-code.py` — multi-route install schema, TS catalog entry, `present` hook, note surfacing, tls-needed sync/worker.
- Create `tests/fixtures/fake_lsp.py` — scripted fake LSP server (modes via argv) for driver tests.
- Create `tests/test_ts_resolve.py`; modify `tests/test_tslsp_linter.py`, `tests/test_check_code.py`.
- Modify `README.md`, `skills/check-code/SKILL.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`; regenerate `portable/`.

---

### Task 1: Unbuffered LSP framing (the latent stall)

**Files:**
- Modify: `scripts/tslsp-lint.py` (`main`: the `Popen` + `read_msg` closure)
- Create: `tests/fixtures/fake_lsp.py`
- Test: `tests/test_tslsp_linter.py`

**Interfaces:**
- Produces: `_LspConn(argv: list[str])` class in `scripts/tslsp-lint.py` with `send(obj: dict) -> None`, `read(timeout_s: float) -> dict | None` (answers server→client requests itself: `workspace/configuration` → list of `None` sized to `params.items`, anything else → `null` result), `close() -> None` (shutdown/exit, kill after 10 s), attribute `proc`. `Popen(..., bufsize=0)`; body read loops until `Content-Length` bytes are in.
- Produces: `tests/fixtures/fake_lsp.py <mode>` — modes used by later tasks: `burst` (writes `initialize` reply AND a second message in ONE `write`), `native-ok` (pull: answers `textDocument/diagnostic` with the items in env `FAKE_ITEMS` JSON), `die-after-init`, `tls-ok` (answers `typescript.tsserverRequest` for `semanticDiagnosticsSync` and `syntacticDiagnosticsSync` from env `FAKE_SEM` / `FAKE_SYN`). It records every request method it receives to the file in env `FAKE_LOG`, one per line.

- [ ] **Step 1: Write the failing test** `test_conn_reads_two_messages_from_one_chunk`: start `_LspConn([sys.executable, FAKE, "burst"])`, send `initialize`, assert `read(5)` returns the reply with `id == 1` and a second `read(2)` returns the burst's second message (method `window/logMessage`) — within 2 s total (`time.monotonic()` delta `< 2`).
- [ ] **Step 2: Run** `pytest tests/test_tslsp_linter.py::test_conn_reads_two_messages_from_one_chunk -v` → FAIL (`_LspConn` undefined).
- [ ] **Step 3: Implement** `_LspConn` and `fake_lsp.py` (`burst` mode first; add the other modes as their tests arrive). Rewire `main` to use `_LspConn`, behaviour otherwise unchanged.
- [ ] **Step 4: Mutation check** — temporarily set `bufsize=-1`: the test must go RED (timeout). Restore. Note the RED in the commit message.
- [ ] **Step 5: Run** `pytest tests/test_tslsp_linter.py -v` → all PASS (existing tests untouched).
- [ ] **Step 6: Commit** `fix: unbuffered LSP framing — select() could not see buffered messages`.

### Task 2: The resolver

**Files:**
- Create: `scripts/ts_resolve.py`
- Test: `tests/test_ts_resolve.py`

**Interfaces:**
- Produces (exact names, used by Tasks 3–6):
  - `Resolution` — frozen dataclass: `kind: str` (`"native"`|`"tls"`), `source: str` (`"env"`|`"project"`|`"tsgo"`|`"path"`|`"npm-global"`|`"private"`|`"global6"`), `version: str`, `exe: Optional[str]` (native), `lib: Optional[str]` (tls), `tls: Optional[str]` (tls binary, absolute), `root: Optional[str]` (project root that pinned the version, or `None`), `note: Optional[str]`, `tls_needed: Optional[tuple]` (`(root, version)`).
  - `class ResolveError(Exception)` — `str(e)` is the user-facing fix line.
  - `resolve(path: Optional[str], env: Mapping = os.environ, which: Callable = shutil.which, home: Optional[str] = None) -> Resolution` — raises `ResolveError`. `path=None` = machine-level only (skip project step); used by setup/sync presence.
  - `package_version(pkg_dir: str) -> Optional[str]`, `major(version: str) -> int` (`0` when unparseable), `complete_lib(pkg_dir: str) -> Optional[str]`, `native_exe(pkg_dir: str) -> Optional[str]` (platform binary, else `bin/tsc`, else `None`), `find_tls(env=os.environ, which=shutil.which, home=None) -> Optional[str]` (private `{home}/ts/node_modules/.bin/typescript-language-server` first, then `which`), `NPM_GLOBAL_DIRS` (list: `/opt/homebrew/lib/node_modules`, `/usr/local/lib/node_modules`, plus `$NPM_CONFIG_PREFIX/lib/node_modules` when set), `GLOBAL6_LIB_DIRS` (today's `_FALLBACK_LIB_DIRS`, verbatim from `tslsp-lint.py`).
  - `home` default: `env.get("CHECK_CODE_HOME", "~/.claude/check-code")`, expanded.
- Algorithm (the order the tests pin; spec Goals 1 + Builder's calls):
  0. `CHECK_CODE_TSSERVER_LIB` set → must hold `tsserver.js` + `typescript.js`, else `ResolveError` naming the variable; tls binary required (`find_tls`) else `ResolveError("typescript-language-server not found — …")`. → `Resolution("tls", "env", …)`.
  1. (only when `path`) walk up from the file's dir to the filesystem root; first `node_modules/typescript/package.json` wins. `major ≥ 7` with `native_exe` → `("native","project")`. `major ≤ 6`: complete lib AND `find_tls` → `("tls","project")`; otherwise take the machine TS 7 from step 2 and attach `note` + `root`; `tls_needed=(root, version)` ONLY when the lib is complete (tls is what is missing). No machine TS 7 → `ResolveError` naming the project root and the problem (tls missing → `npm install --prefix {home}/ts typescript-language-server`; lib incomplete → `npm install` in the root). A project pin never falls to step 3.
  2. machine TS 7, first hit: `which("tsgo")` (realpath; version from the enclosing package's `package.json`, else assume 7); `which("tsc")` realpath → package dir = parent of `bin/`, `major ≥ 7`; each `NPM_GLOBAL_DIRS/typescript` at `major ≥ 7`; `{home}/ts/node_modules/typescript` at `major ≥ 7`. Source names in that order: `tsgo`, `path`, `npm-global`, `private`.
  3. global TS ≤ 6: `which("tsc")` package with a complete lib, then `GLOBAL6_LIB_DIRS`; requires `find_tls` → `("tls","global6")`.
  4. `ResolveError("no usable TypeScript found — run: check-code setup")`.
- Note text (exact): `"linted with TypeScript {v7} — this project pins TypeScript {v} ({root}); installing its checker in the background"` (tls missing) / `"linted with TypeScript {v7} — this project's TypeScript {v} install is incomplete (no tsserver.js); run npm install in {root}"` (lib incomplete).

- [ ] **Step 1: Write the failing tests** in `tests/test_ts_resolve.py`, using a `make_ts(dir, version, layout)` helper that writes `package.json` (`{"name":"typescript","version":…}`) and, by `layout`: `"ts7"` → `bin/tsc` + `node_modules/@typescript/typescript-<plat>-<arch>/lib/tsc` (executable stub); `"ts7-noplat"` → `bin/tsc` only; `"ts6"` → `lib/tsserver.js` + `lib/typescript.js`; `"ts6-stub"` → `lib/tsc.js` only. `which` is a dict-backed fake; `env` a dict with `CHECK_CODE_HOME` in `tmp_path`. One test per row, names as listed, each asserting `(kind, source)` and the path fields:
  - `test_env_override_wins_over_project_ts7` · `test_env_override_invalid_names_the_variable` (`"CHECK_CODE_TSSERVER_LIB" in str(err)`)
  - `test_project_ts7_uses_native_platform_binary` · `test_project_ts7_hoisted_platform_binary` (platform pkg in the `node_modules` beside `typescript`)
  - `test_project_ts6_with_tls_uses_tls_with_project_lib` · `test_project_ts6_without_tls_falls_back_to_machine_ts7_with_note_and_tls_needed` · `test_project_ts6_stub_lib_falls_back_with_incomplete_note_and_no_tls_needed` · `test_project_ts6_without_any_ts7_raises_naming_the_root` · `test_project_pin_never_uses_global_ts6`
  - `test_tsgo_on_path_wins_over_tsc_on_path` · `test_tsc_on_path_ts7_resolved_through_symlink` (Review Focus 3) · `test_ts7_without_platform_binary_falls_back_to_bin_tsc` (Review Focus 2) · `test_npm_global_ts7` · `test_private_ts7_is_last_ts7_candidate`
  - `test_global_ts6_on_path_uses_tls` · `test_global_ts6_without_tls_is_no_typescript` · `test_nothing_raises_setup_hint`
  - `test_path_none_skips_project_step` · `test_walk_handles_spaces_and_non_ascii` (`tmp_path / "my proj ü"`, Review Focus 1)
- [ ] **Step 2: Run** `pytest tests/test_ts_resolve.py -v` → all FAIL (module missing).
- [ ] **Step 3: Implement** `scripts/ts_resolve.py` per the Interfaces + Algorithm above; `--help` is not needed (it is a module, not a script; no `__main__`).
- [ ] **Step 4: Run** `pytest tests/test_ts_resolve.py -v` → all PASS.
- [ ] **Step 5: Mutation check** — swap steps 2 and 3 in `resolve`; `test_project_pin_never_uses_global_ts6` or the global-ts6 tests must go RED. Swap `tsgo`/`tsc` order; `test_tsgo_on_path_wins_over_tsc_on_path` must go RED. Restore.
- [ ] **Step 6: Commit** `feat: ts_resolve — pick the user's TypeScript per file (TS 7 native first)`.

### Task 3: Two drivers in the runner

**Files:**
- Modify: `scripts/tslsp-lint.py`
- Test: `tests/test_tslsp_linter.py` (rewrite the three 0.5.3 `_tsserver_lib` tests; see Step 1)

**Interfaces:**
- Consumes: `_LspConn` (Task 1); `ts_resolve.resolve`, `Resolution`, `ResolveError` (Task 2) — imported via `sys.path.insert(0, <dir of this file>)`.
- Produces: runner contract (unchanged for `check-code.py`): stdout = JSON lines; each findings line is `{"jsonrpc":"2.0","method":"textDocument/publishDiagnostics","params":{"uri":…,"diagnostics":[…]}}` with LSP 0-based ranges, `severity`, `code`, `message`; exit 0 with output, exit 2 on failure with ONE stderr line naming the cause. New line kind: `{"method":"check-code/note","params":{"text": <str>}}` (Task 4 emits it).
- Produces: `_run_native(res, path, uri, root_uri) -> Optional[list]` (items or `None` on no answer) and `_run_tls(res, path, uri, root_uri) -> Optional[list]`; `main()` resolves, dispatches, prints ONE findings line.
- Native: `initialize` with `capabilities.textDocument.diagnostic = {}`, `didOpen` (languageId from `_language_id`), `textDocument/diagnostic`, items → findings line as-is.
- tls: `initializationOptions.tsserver.path = res.lib`, server argv `[res.tls, "--stdio"]`; request `syntacticDiagnosticsSync` AND `semanticDiagnosticsSync` via `typescript.tsserverRequest`; merge both bodies (same tsserver→LSP mapping as today) into one findings line; a push `publishDiagnostics` is still accepted as today.

- [ ] **Step 1: Write the failing tests:**
  - `test_native_driver_prints_pull_items` — resolver monkeypatched to `Resolution("native", …, exe=<fake_lsp.py wrapper>)` (wrapper: a tmp shell script `exec python3 FAKE native-ok "$@"`), `FAKE_ITEMS` with one `2322` at line 4; assert one findings line with that diagnostic, exit 0, and `FAKE_LOG` contains `textDocument/diagnostic`.
  - `test_native_driver_exit2_when_server_dies` (Review Focus 4) — mode `die-after-init` → exit 2, stderr non-empty, NO findings line.
  - `test_tls_driver_requests_syntactic_and_semantic` — mode `tls-ok`, `FAKE_SYN` one `1109`, `FAKE_SEM` one `2322` → both in the one findings line; `FAKE_LOG` shows both commands.
  - `test_runner_resolve_error_is_exit2_with_the_fix` — resolver raises `ResolveError("X-FIX")` → exit 2, stderr contains `X-FIX`.
  - Rewrite (0.5.3 → resolver): `test_tsserver_lib_prefers_private_home_lib` → DELETE (behaviour removed; covered by Task 2 order tests). `test_tsserver_lib_skips_ts7_layout` → becomes `test_env_override_ts7_layout_is_exit2_naming_variable` (runner level). `test_runner_names_the_ts7_cause_when_no_lib` → becomes `test_runner_no_typescript_names_setup`.
- [ ] **Step 2: Run** `pytest tests/test_tslsp_linter.py -v` → new tests FAIL.
- [ ] **Step 3: Implement** `_run_native`, `_run_tls`, the dispatch in `main`; delete `_tsserver_lib` / `_FALLBACK_LIB_DIRS` from the runner (they live in `ts_resolve`). Update the module docstring's Usage/Environment (env: `CHECK_CODE_HOME`, `CHECK_CODE_TSSERVER_LIB`).
- [ ] **Step 4: Run** `pytest tests/test_tslsp_linter.py tests/test_ts_resolve.py -v` → PASS; `python3 scripts/tslsp-lint.py --help` prints usage (exit 0).
- [ ] **Step 5: Real-machine check (this machine: global TS 7.0.2)** — `python3 scripts/tslsp-lint.py scripts/probe/bad.ts` → exit 0, exactly ONE diagnostic, code `2322`, LSP line `4`. Record the output in the commit body.
- [ ] **Step 6: Commit** `feat: native TS 7 driver (tsc --lsp pull diagnostics) beside the tls driver`.

### Task 4: Severity filter, note line, `tls-needed.json`

**Files:**
- Modify: `bin/check-code.py` (`_tslsp_parse`, new `_tslsp_notes`), `scripts/tslsp-lint.py`
- Test: `tests/test_tslsp_linter.py`

**Interfaces:**
- Produces: `_tslsp_parse(stdout)` drops diagnostics whose `severity` is 3 or 4 (missing severity = keep). `_tslsp_notes(stdout) -> list[str]` returns every `check-code/note` line's `params.text`.
- Produces: `TLS_NEEDED_FILE = "tls-needed.json"` in BOTH `scripts/ts_resolve.py` (source of truth) and `bin/check-code.py` (imports or duplicates the constant — the plan's call: `check-code.py` imports `ts_resolve` in Task 6, so define it once in `ts_resolve`). File shape: `{"<root>": "<pinned version>"}`.
- Produces: `record_tls_needed(home: str, root: str, version: str) -> bool` in `ts_resolve` — atomic (`tempfile` in `home` + `os.replace`), returns `True` only when `root` was NOT already present; a corrupt/unreadable file reads as `{}`. `load_tls_needed(home) -> dict`, `clear_tls_needed(home) -> None`.
- Runner: when `res.note` is set, print the note line only the FIRST time per project: call `record_tls_needed(home, key, version)` and print only when it returns `True`. `key` = `res.tls_needed[0]` (the root) when tls is what is missing; `"incomplete:" + res.root` when the project's lib is incomplete (such keys never trigger an install, Task 7).

- [ ] **Step 1: Write the failing tests:** `test_parse_drops_hint_and_info_severities` (a line with severities 1,2,3,4 → only 1 and 2 survive); `test_notes_are_extracted_and_ignored_by_parse`; `test_record_tls_needed_true_once_per_root`; `test_record_tls_needed_corrupt_file_is_empty` (Review Focus 5: write `"{garbage"`, call → `True`, file now valid JSON); `test_record_tls_needed_is_atomic` (monkeypatch `os.replace` to raise → original file unchanged); `test_runner_prints_note_once_per_project` (two runner calls on a TS 6-without-tls fixture with a fake TS 7 → note line in the first output only).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_tslsp_linter.py tests/test_ts_resolve.py -v` → PASS. Existing `test_tslsp_parser_reads_real_fixture` still PASS (its fixture is severities 1/2).
- [ ] **Step 5: Commit** `feat: report errors+warnings only; once-per-project TS fallback note`.

### Task 5: Multi-route installs (generic) + the TypeScript routes

**Files:**
- Modify: `bin/check-code.py` (`install_cmds`, `install_hint`, `setup_plan`, `_sync_main_run` spawnable/manual split, the `typescript-lsp` catalog entry)
- Test: `tests/test_check_code.py`

**Interfaces:**
- Produces: catalog `install` may be the legacy single form `{"manager","cmd","extra"?}` OR `{"routes": [ {"manager","cmd","extra"?,"when"?}, … ]}`; `linters.local.json` string form unchanged.
  - `ROUTE_CONDITIONS = {"no_tsc_on_path": lambda find_bin: find_bin("tsc") is None}`.
  - `install_routes(linter) -> list[dict]` (normalized; legacy → list of one).
  - `select_route(linter, find_bin=find_binary) -> Optional[dict]` — first route whose `manager` resolves and whose `when` (if any) holds.
  - `install_cmds(linter, home=None, find_bin=find_binary)` and `install_hint(linter, find_bin=find_binary)` use `select_route`, falling back to the FIRST route for display when none is selectable.
  - `route_managers(linter) -> list[str]` — every route's manager, for the "no brew or npm here" notice.
- TS catalog entry `install`: `{"routes": [{"manager": "brew", "cmd": ["brew", "install", "typescript"], "when": "no_tsc_on_path"}, {"manager": "npm", "cmd": ["npm", "install", "--prefix", "{home}/ts", "typescript@7"]}]}`. Remove the `typescript-language-server` brew install and the `typescript@6` extra. Rewrite the entry's comment to the new story (resolver, routes, why tls is not installed here).

- [ ] **Step 1: Write the failing tests:** `test_legacy_install_is_one_route` (every other BUILTIN entry: `install_cmds` output identical to before — snapshot all ids); `test_ts_route_brew_when_no_tsc_on_path`; `test_ts_route_npm_private_when_tsc_on_path` (even with brew present); `test_ts_route_npm_when_no_brew`; `test_ts_no_route_without_brew_or_npm_names_both_in_the_notice` (sync `manual` notice text contains `brew` and `npm`); `test_npm_route_expands_home` (`{home}` → `CHECK_CODE_HOME`); `test_no_install_cmd_uses_sudo` (no BUILTIN route's argv contains `sudo`).
- [ ] **Step 2: Run** `pytest tests/test_check_code.py -k "route or sudo or legacy" -v` → FAIL.
- [ ] **Step 3: Implement**; update every `linter["install"]["manager"]` call site (`setup_plan`, `_sync_main_run`) to `select_route` / `route_managers`.
- [ ] **Step 4: Run** `pytest -n auto` (full suite) → PASS except tests Task 6 will rewrite; list any such failures by name in the commit body rather than editing them here.
- [ ] **Step 5: Commit** `feat: install routes tried in order (brew, then npm) — TS 7 when nothing is usable`.

### Task 6: Presence through the resolver + notes in the hook

**Files:**
- Modify: `bin/check-code.py` (`check_edit`, `run_linter`, `linter_ready`, `setup_plan`, `_sync_main_run` missing/due, `_tslsp_command`, TS catalog entry)
- Test: `tests/test_check_code.py`

**Interfaces:**
- Consumes: `ts_resolve.resolve` (loaded with `importlib` from the same two candidate dirs `_tslsp_command` searches: `<bin>/../scripts`, `<bin>/scripts`; missing module → TS treated as absent, never a crash).
- Produces: catalog field `"present": callable(path: Optional[Path]) -> bool` (TS entry: `resolve(str(path) if path else None)` succeeds). Helper `linter_present(linter, path=None, find_bin=find_binary) -> bool` = `present(path)` when defined, else `find_bin(binary)`. Replace the `find_binary(lin["binary"])` presence checks in `check_edit` (per file), `linter_ready`, `setup_plan`, `_sync_main_run` (`missing`, `due`) with `linter_present`.
- Produces: catalog field `"notes": callable(stdout) -> list[str]` (TS: `_tslsp_notes`); `run_linter_output(linter, path) -> (findings, notes)`; `run_linter` keeps its signature (returns findings). `check_edit` appends notes after findings as lines `note: <text>`; notes alone (no findings) are still returned.
- TS catalog `binary` becomes `"tsc"` (display only; for TS, presence always goes through `present`). Update `test_tslsp_in_builtin_catalog_with_ts_exts` to assert `binary == "tsc"` and that `present` is callable.

- [ ] **Step 1: Write the failing tests:** `test_ts_present_via_project_typescript_even_without_global` (a tmp project with fake TS 7 in `node_modules`, no `tsc` on PATH → `check_edit` runs the TS linter for that file); `test_ts_absent_names_coverage_notice` (nothing resolvable → the existing coverage notice); `test_notes_reach_the_hook_output_without_findings`; `test_setup_plan_ts_installed_when_machine_ts7` (fake `tsc` 7 on PATH → status `installed`); `test_missing_resolver_module_means_absent_not_crash`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -n auto` → PASS (fix the tests Task 5 listed, by name).
- [ ] **Step 5: Commit** `feat: TS presence via the resolver; linter notes reach the hook`.

### Task 7: Background install of the TS ≤ 6 driver

**Files:**
- Modify: `bin/check-code.py` (`_sync_main_run`, `_sync_worker_run`)
- Test: `tests/test_check_code.py`

**Interfaces:**
- Consumes: `load_tls_needed`, `clear_tls_needed`, `find_tls`, `TLS_NEEDED_FILE` (Tasks 2/4).
- Produces: `TLS_INSTALL = ["npm", "install", "--prefix", "{home}/ts", "typescript-language-server"]`; results id `"typescript-tls"` in `sync-state.json` `results` (same record shape as catalog linters, so the existing "installed: …" / "could not install …" report covers it). Sync: tls-needed entries present AND `find_tls` is `None` → work item (npm present → spawn worker; npm absent → manual notice `run: npm install --prefix … typescript-language-server`). Worker: run it via `run_install`, verify by OUTCOME (`find_tls` not `None`), record, and on success `clear_tls_needed` (entries whose key starts with `incomplete:` are kept). Retry gate: `SYNC_RETRY_AFTER`.

- [ ] **Step 1: Write the failing tests** (injected `run_install`/`find_bin`/`clock`, no real installs): `test_sync_spawns_worker_when_tls_needed`; `test_sync_no_work_when_tls_present`; `test_worker_installs_tls_and_clears_file_on_success`; `test_worker_records_failed_when_tls_still_missing` (install exit 0 but `find_tls` stays `None` → `failed`, reason mentions the probe/outcome); `test_worker_respects_retry_gate`; `test_incomplete_entries_never_trigger_install`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `pytest -n auto` → PASS.
- [ ] **Step 5: Commit** `feat: install the TS ≤ 6 checker in the background when a project needs it`.

### Task 8: Docs, version 0.6.0, portable, full gate

**Files:**
- Modify: `README.md` (TS/JS row :46, tier table :69, install text :94, :171), `skills/check-code/SKILL.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `bin/check-code.py` (`VERSION`, help epilogs that name `typescript-lsp` installs); regenerate `portable/` via `python3 scripts/build-portable.py`.

- [ ] **Step 1:** CHANGELOG `## [0.6.0] — <date>`: Added (TS 7 native driver; per-file resolver; install routes), Changed (no private `typescript@6`; tls only on demand, private npm; severities 1/2 only; tls reports syntax errors), Fixed (buffered-pipe stall), Notes (existing `{home}/tsserver` left in place; how to remove it by hand; users may uninstall the official `typescript-lsp` plugin if they only wanted linting).
- [ ] **Step 2:** bump all version surfaces; `grep -rn "0\.5\.3" --exclude-dir=.git --exclude=CHANGELOG.md .` → no hits.
- [ ] **Step 3:** `python3 scripts/build-portable.py` then `python3 scripts/build-portable.py --check` → exit 0; confirm `scripts/ts_resolve.py` is in `portable/`.
- [ ] **Step 4:** `python3 bin/check-code.py --help` and `python3 bin/check-code.py setup --help` → mention the TS routes and `CHECK_CODE_TSSERVER_LIB`; `python3 scripts/tslsp-lint.py --help` exit 0.
- [ ] **Step 5:** full gate `pytest -n auto -rs` → all PASS; list every SKIP with its reason in the commit body (a skip is not a pass).
- [ ] **Step 6: Commit** `chore(0.6.0): docs, version, portable`. Push the branch.

### Task 9: Outcome verification on real setups (by outcome, not exit code)

No code; each check records command + output in the final report.

- [ ] **A. Machine TS 7 (this machine):** `CHECK_CODE_HOME=$(mktemp -d)` → `python3 bin/check-code.py setup --dry-run` (or the repo's plan-only form) reports `typescript-lsp` already installed (resolver: `path`); a PostToolUse payload editing a tmp `.ts` with a planted `2322` → hook output contains `2322`.
- [ ] **B. TS 6 project without tls:** tmp project, `npm install --prefix <proj> typescript@6` (local download, no global change); fresh `CHECK_CODE_HOME` → first hook run reports `2322` AND the note; second run: no note. Run the worker (`python3 bin/check-code.py sync --worker` with that home) → `{home}/ts/node_modules/.bin/typescript-language-server` exists; next hook run reports `2322` with NO note, via tls (verify with `CHECK_CODE_DEBUG=1` or a driver marker in the runner's stderr).
- [ ] **C. Nothing usable:** `PATH=/usr/bin:/bin` + fresh home + a dir with no `node_modules` → the coverage notice names `check-code setup`; `select_route` picks brew only if `tsc` absent (it is, under that PATH).
- [ ] **D. Global `tsc` untouched:** `tsc --version` before and after A–C → identical (`Version 7.0.2` here).
- [ ] **E. Whole-branch review:** one fresh reviewer on the most capable model over `git diff origin/main...HEAD` + the spec; fix findings; re-run the gate.

### Ship (GATED — needs the user's explicit go and depth)

Not part of executing this plan. When the user says go: ship from the WORKTREE with run-to-completion's `rtc-ship.sh <worktree> 0.6.0 "<title>"` (never the canonical checkout), verify with `git ls-remote` / `get-haiggoh plan`, refresh the installed plugin, dogfood Task 9-A through the INSTALLED copy. After it is live, the user uninstalls the official `typescript-lsp` plugin themselves (their stated plan). Close waypoint `check-code-ts-7-native` with the evidence.
