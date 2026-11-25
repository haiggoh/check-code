# Private TypeScript 6 lib + Swift Xcode gate + swiftformat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (native: the executor does every task, then one final whole-branch review). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix check-code's TS linter for every user now that `typescript` is 7.x, stop the default tier from depending on a multi-GB Xcode, and add `swiftformat` as an Xcode-free Swift linter in the default tier.

**Architecture:** A catalog entry may carry `extra` install commands (run after `cmd`, `{home}` = `config_home()`) and a `requires` gate (a named predicate plus a human reason). typescript-lsp installs `typescript@6` privately under `$CHECK_CODE_HOME/tsserver`; `tslsp-lint.py` looks there first. Linters whose `requires` is unmet are never installed by `setup`/`sync`. They are reported once, with the reason, and never retried daily.

**Tech Stack:** Python 3 stdlib, pytest (`pytest -n auto` for the full suite), brew, npm.

**Spec:** this conversation, 2026-11-25 (user decisions): (1) "Private TS6 for check-code", global TS stays untouched; (2) Swift: "Ask only if Xcode missing", so swiftlint installs by default only when full Xcode is already present; (3) "add swiftformat either way (unless it's recommended to be mutually exclusive with swiftlint)". The two are not mutually exclusive and are commonly used together.

## Global Constraints

- Measured facts (2026-11-25): npm `typescript` latest = `7.0.2` (no `lib/tsserver.js`), brew `typescript` = `7.0.2`, last JS line = `typescript@6` (`6.0.3`). brew `swiftlint` requires `xcode >= 8.0` (full Xcode.app, not the Command Line Tools). brew `swiftformat` 0.64.7 has no requirements and no dependencies.
- Private lib path: `config_home() / "tsserver" / "node_modules" / "typescript" / "lib"`. Install: `npm install --prefix {home}/tsserver typescript@6`.
- Never touch the user's global `typescript`.
- Xcode is "present" iff `xcode-select -p` prints a path containing `.app/Contents/Developer`. A `/Library/Developer/CommandLineTools` path does NOT count.
- Notice copy for the unmet gate (exact): `swiftlint skipped: it needs the full Xcode app (a multi-GB App Store download), which is not installed. Install Xcode yourself if you want swiftlint; check-code then installs it automatically.`
- `portable/` is generated: after code changes run `scripts/build-portable.py`, never hand-edit it.
- Every version surface is bumped together (`.claude-plugin/plugin.json`, CHANGELOG heading); the version is picked at ship time from `git ls-remote --tags origin` (expected 0.5.3).
- Step routing: zero `local:` steps. Each task is a small edit interleaved with red/green runs against the real 1300-line file in this session, so a dispatch would cost more than it saves. Exception: the final review in Task 4 is a free cross-check dispatch (`local:`/free reviewer), backed by one paid review only if the free route is exhausted.

## Review Focus

1. **User with Xcode installed** must see no change for swiftlint: it still installs and runs by default. (Task 2 test `test_requires_met_installs_normally`.)
2. **User who installs Xcode later**: the next sync must pick swiftlint up automatically, and the "skipped" notice must not block that. (Task 2 test `test_gate_lifts_when_xcode_appears`.)
3. **Already-broken TS users** (TS7 global, probe failing today): the next sync must install the private lib and the probe must pass, without them running setup. (Task 1 test `test_worker_runs_extra_commands_then_probes`.)
4. **npm missing** (node not installed yet): a failed `extra` step must report a reason naming `npm`, not crash the worker or loop. (Task 1 test `test_extra_command_failure_is_reported`.)
5. **swiftlint + swiftformat contradicting each other** on the same line (e.g. trailing commas, brace wrapping). Measure on a fixture in Task 3; if both fire opposite verdicts on default rules, disable the conflicting swiftformat rule(s) in its command and pin that with `test_swift_linters_do_not_contradict`.

---

### Task 1: Private TypeScript 6 lib for typescript-lsp

**Files:**
- Modify: `bin/check-code.py` (catalog entry `typescript-lsp` ~line 261; `_run_install` ~969; `setup_plan`/`cmd_setup` ~981–1105; `_sync_worker_run` ~1246)
- Modify: `scripts/tslsp-lint.py` (`_tsserver_lib` ~69, `main` ~95)
- Test: `tests/test_check_code.py`, `tests/test_tslsp_linter.py`

**Interfaces:**
- Produces: `install_cmds(linter) -> list[list[str]]`, which returns `[cmd] + extra` with `{home}` expanded to `str(config_home())`. Every installer call site (`cmd_setup`, `_sync_worker_run`) runs these in order, stopping at the first failure. `install_hint(linter)` joins them with ` && `.
- Produces: `_run_installs(cmds, run_install) -> (ok, reason)`.
- tslsp-lint: `_tsserver_lib()` candidates = env `CHECK_CODE_TSSERVER_LIB`, then `$CHECK_CODE_HOME` (default `~/.claude/check-code`) `/tsserver/node_modules/typescript/lib`, then the existing list.

- [ ] **Step 1: Write failing tests**
  - `test_install_cmds_expands_home_in_extra`: for the real `typescript-lsp` entry, `cc.install_cmds(entry)[-1] == ["npm","install","--prefix", str(home/"tsserver"), "typescript@6"]` and `install_cmds(entry)[0][:2] == ["brew","install"]`.
  - `test_install_hint_shows_every_step`: `" && npm install --prefix " in cc.install_hint(entry)`.
  - `test_worker_runs_extra_commands_then_probes`: fake linter with `extra`; the recorded run_install calls equal both cmds in order; the result is `installed` when the injected `ready` turns True after the second call.
  - `test_extra_command_failure_is_reported`: second cmd returns `(False, "npm: command not found")` → state result `failed`, reason contains `npm`, no exception.
  - `test_tsserver_lib_prefers_private_home_lib` (test_tslsp_linter.py): create `tmp/home/tsserver/node_modules/typescript/lib/{tsserver.js,typescript.js}`, set `CHECK_CODE_HOME`, unset `CHECK_CODE_TSSERVER_LIB` → returns that dir.
  - `test_tsserver_lib_skips_ts7_layout`: a dir with only `tsc.js` + `getExePath.js` is never returned.
  - `test_runner_names_the_ts7_cause_when_no_lib`: with no candidate valid (monkeypatch the candidate list to tmp dirs), `main()` writes to stderr a line containing `TypeScript 7` and `typescript@6`, and exits 2.

- [ ] **Step 2: Run and confirm RED**
  Run: `pytest tests/test_check_code.py tests/test_tslsp_linter.py -k "install_cmds or install_hint_shows or extra_command or tsserver_lib or ts7" -v`
  Expected: FAIL (`install_cmds` undefined, etc.). Also record that `test_default_probe_runs_the_real_driver_on_the_fixture` FAILS on this machine today (global TS 7.0.2). That is the real-world red.

- [ ] **Step 3: Implement**
  - Add `"extra": [["npm", "install", "--prefix", "{home}/tsserver", "typescript@6"]]` to the typescript-lsp `install` dict. Update its comment: TS 7 is native and ships no tsserver.js, so the private 6.x lib is what the probe needs.
  - `install_cmds`, `_run_installs`; switch `install_hint`, `cmd_setup` and `_sync_worker_run` to them.
  - `_default_probe`: append the last stderr line to the failing detail.
  - tslsp-lint: new candidate first after env. In `main()`, when `_tsserver_lib()` is None, print to stderr `check-code: no TypeScript with tsserver.js found (TypeScript 7 ships none) — run: npm install --prefix <home>/tsserver typescript@6` and `sys.exit(2)`.

- [ ] **Step 4: GREEN + real-world proof**
  Run the Step 2 command → PASS. Then for real on this machine: `CHECK_CODE_HOME=$(mktemp -d) sh -c 'npm install --prefix "$CHECK_CODE_HOME/tsserver" typescript@6 && pytest tests/test_check_code.py -k default_probe_runs_the_real_driver -v'` → PASS (it failed in Step 2).

- [ ] **Step 5: Commit** (stage by path): `bin/check-code.py scripts/tslsp-lint.py tests/test_check_code.py tests/test_tslsp_linter.py`, message `fix: typescript-lsp uses a private typescript@6 lib (TS 7 ships no tsserver)`.

### Task 2: `requires` gate — swiftlint only when full Xcode is present

**Files:**
- Modify: `bin/check-code.py` (swiftlint entry ~250, `setup_plan`, `_print_plan`, `cmd_setup`, `_sync_main_run`, `_sync_worker_run`, setup help/menu text generated from the catalog ~295–380)
- Test: `tests/test_check_code.py`

**Interfaces:**
- Produces: catalog key `"requires": {"check": "xcode", "reason": "<exact notice copy from Global Constraints>"}`. `REQUIREMENT_CHECKS = {"xcode": xcode_present}`. `requirement_unmet(linter) -> str | None` returns the reason or None. Tests monkeypatch `cc.xcode_present`, so the module-level function is looked up at call time.
- Produces: `setup_plan` status `"skipped"` (detail = reason). `_print_plan` label `"skipped (needs Xcode)"`. `cmd_setup` never installs `skipped`.
- Sync: an unmet linter is excluded from `missing`/`due`/worker work. The main sync says the reason once per linter (state key `requires_notice[<id>] = True`) and clears that flag when the requirement becomes met.

- [ ] **Step 1: Write failing tests**
  - `test_xcode_present_rejects_command_line_tools`: monkeypatch the `xcode-select -p` runner → `/Library/Developer/CommandLineTools` → False; `/Applications/Xcode.app/Contents/Developer` → True; runner raises OSError → False.
  - `test_setup_plan_skips_unmet_requirement`: `xcode_present=False` → swiftlint `("swiftlint","skipped")`, and the detail starts with `swiftlint skipped: it needs the full Xcode app`.
  - `test_requires_met_installs_normally`: `xcode_present=True`, swiftlint not on PATH → status `install`.
  - `test_sync_says_gate_reason_once_and_spawns_nothing_for_it`: unmet gate, swiftlint the only missing linter → one `say` with the reason, `spawn` not called; a second run → no `say`.
  - `test_gate_lifts_when_xcode_appears`: after the notice, flip `xcode_present` to True → the next sync spawns the worker and says `installing swiftlint`.
  - `test_worker_never_installs_unmet`: `run_install` is never called with the swiftlint cmd.

- [ ] **Step 2: RED** — `pytest tests/test_check_code.py -k "xcode or requirement or gate or unmet" -v` → FAIL.
- [ ] **Step 3: Implement** per Interfaces. `xcode_present()` runs `["xcode-select", "-p"]` (timeout 5 s, via an injectable `_xcode_select` helper). Add a `requires` hint to the catalog-generated setup text so `--help` names it.
- [ ] **Step 4: GREEN** — same command → PASS. Then the real run: `./bin/check-code.py setup --dry-run --tier polyglot` on this machine (full Xcode is installed) → swiftlint shows `already installed`, not `skipped`.
- [ ] **Step 5: Commit**: `feat: swiftlint installs by default only when full Xcode is present`.

### Task 3: swiftformat in the default (polyglot) tier

**Files:**
- Modify: `bin/check-code.py` (new BUILTIN entry after swiftlint; new `_swiftformat_parse`)
- Create: `tests/fixtures/swiftformat-lint.txt` (REAL captured output)
- Test: `tests/test_check_code.py`

**Interfaces:**
- Produces: entry `{"id": "swiftformat", "language": "Swift", "binary": "swiftformat", "tier": "polyglot", "install": {"manager": "brew", "cmd": ["brew","install","swiftformat"]}, "ext": ["swift"], "shebang": None, "command": <lint cmd>, "parse": _swiftformat_parse}`. `_swiftformat_parse(stdout) -> list[(line:int, code:str, msg:str)]`, the same tuple shape as the other parsers.

- [ ] **Step 1: Measure first** — `brew install swiftformat`. Run `swiftformat --lint <a bad .swift fixture>` with and without `--reporter json`, and save the real output as the fixture. Decide the command from what it actually prints (the findings stream: stdout or stderr?). Run swiftlint on the same fixture and note any line where the two give opposite verdicts (Review Focus 5).
- [ ] **Step 2: Write failing tests**: `test_swiftformat_in_catalog_polyglot_swift_ext`, `test_swiftformat_parser_reads_real_fixture` (line numbers 1-based, rule name as code), `test_swiftformat_parser_empty_is_no_findings`, and, if Step 1 found a conflict, `test_swift_linters_do_not_contradict`, pinning the disabled rule(s) in the command.
- [ ] **Step 3: RED → implement → GREEN** — `pytest tests/test_check_code.py -k swiftformat -v`.
- [ ] **Step 4: Commit**: `feat: swiftformat (no Xcode needed) joins the polyglot tier`.

### Task 4: Portable rebuild, docs, full gate, review

**Files:** `portable/` (generated), `README.md` (linter table / tiers), `CHANGELOG.md`, `.claude-plugin/plugin.json`

- [ ] **Step 1:** `scripts/build-portable.py`, then `pytest tests/test_portable.py -v` → PASS.
- [ ] **Step 2:** README: add the swiftformat row, the Xcode note on swiftlint and the private TS lib. CHANGELOG `[0.5.3] — <date>` entry (version re-checked via `git ls-remote --tags origin`), and bump `plugin.json`.
- [ ] **Step 3: Full gate** — `pytest -n auto tests/ > /tmp/cc-gate.log 2>&1; echo EXIT:$?` → read `EXIT:0` and the `N passed` line; `-rs` to list skips. A skip of a real-driver test is not a pass.
- [ ] **Step 4: Review** — free-lane cross-check dispatch of the branch diff (`git diff origin/main...`) against this plan; fix the findings that hold up.
- [ ] **Step 5: Commit**, then ship per the user's authorised depth via `run-to-completion:ship` (rtc-ship from this worktree). After it ships, dogfood on this machine: the next session's sync installs the private lib; `check-code.py doctor` shows typescript-lsp ready; a planted TS error on edit is reported.
