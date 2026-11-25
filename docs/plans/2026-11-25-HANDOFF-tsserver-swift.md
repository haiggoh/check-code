# HANDOFF — check-code 0.5.3: private TS6 lib, Xcode gate, swiftformat (free session, autopilot)

Read this whole file first and follow it exactly: adopt its standing rules for the whole session,
then continue autonomously from **Next action**.

## 1. Why this session exists

A paid coordinator session (Claude Opus 5.5) diagnosed the problems and wrote the plan. The user then
ordered (verbatim): **"yes and through /ship release. run it on /autopilot on
/free-agents:free-session-handover max effort with the qc playbook"**. You are that free session. Nothing
ended badly: this is a planned handover, so do not re-diagnose. The facts below were measured by the
coordinator on 2026-11-25.

## 2. State (at handover)

- Worktree: `~/ClaudeWorkspace/.wt/cc-tsserver-swift`, branch `fix/tsserver-lib-and-swift`, pushed.
  - `9d32c89` adds the plan `docs/plans/2026-11-25-private-tsserver-and-swift-xcode-gate.md`.
  - The next commit adds this handoff.
- Base: `origin/main` = `a5ae319` = released **v0.5.2**. The next version is expected to be **0.5.3**;
  re-check it with `git ls-remote --tags origin` right before shipping.
- No product code changed yet. Gate not run yet.
- Measured facts (also in the plan's Global Constraints): global and brew `typescript` = 7.0.2, with no
  `lib/tsserver.js`; `typescript@6` = 6.0.3 on npm. brew `swiftlint` requires full Xcode ≥ 8.0;
  `swiftformat` 0.64.7 has no requirements. This machine HAS full Xcode
  (`/Applications/Xcode.app/Contents/Developer`), and swiftlint is installed; swiftformat is not.
- Expected real-world RED today: `tests/test_check_code.py::test_default_probe_runs_the_real_driver_on_the_fixture`
  fails here, because no complete TS lib exists.

## 3. The goal (verbatim user decisions)

1. "I don't only want to fix this problem locally but for all users of check-code" — TS fix choice:
   **"Private TS6 for check-code"** (global TypeScript stays untouched).
2. Swift: **"Ask only if Xcode missing"**, i.e. swiftlint installs by default when full Xcode is present,
   otherwise it is skipped with a one-time notice naming the multi-GB cost. Confirmed reading:
   check-code cannot install Xcode, so "ask" = skip + tell once.
3. **"add swiftformat either way (unless it's recommended to be mutually exclusive with swift lint)"**.
   They are not mutually exclusive; add it to the polyglot tier. Measure rule conflicts (plan Task 3).
4. Ship depth: **through release** via `/ship` (`run-to-completion:ship`): gate, merge to main, tag,
   GitHub release, plugin refresh, dogfood.

## 4. Standing rules (numbered, blunt)

1. Autonomous: decide and record instead of asking. Stop only for a fork that cannot be weighed with
   pros and cons, and write it to §6 when you do.
2. Work ONLY in `~/ClaudeWorkspace/.wt/cc-tsserver-swift`. Never `cd` into or switch the branch of
   `~/ClaudeWorkspace/check-code` (the canonical checkout; tooling resolves through it).
3. Commit every task, staged **by path** (never `git add -A` / `.`), and push the branch after each
   commit. Every commit message ends with BOTH lines, verbatim, in this order:
   ```
   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
   Co-Authored-By: Mistral Large 4 "Le Chonk" 1T-A52B <noreply@mistral.ai>
   ```
   (If you are not Le Chonk, open `~/.claude/projects/-Users-bra0002h/memory/commit-attribution-by-model.md`
   and use your own row; never derive one.)
   **Merge, tag and release ARE authorized here, but ONLY through `run-to-completion:ship`**
   (it runs rtc-ship from THIS worktree with `<worktree-dir> <version> "<title>"`). Never ship from
   the canonical checkout. Never force-push, never move a published tag: fix forward as 0.5.4.
   A successful ship REAPS the worktree, so afterwards use `git -C ~/ClaudeWorkspace/check-code`
   for read-only checks only.
4. TDD every change: watch it fail for the right reason, then pass. Never weaken a test to get green.
   Regression tests get the full red-green cycle: pass → revert fix → MUST FAIL → restore → pass.
5. **QC playbook is mandatory:** `~/.claude/plans/_PLAYBOOKS/Playbook-milestone-qc-offload-test-proof.md`.
   Read it fully before Task 1. Milestones are listed in §5. At each one: run `superpowers:verification-before-completion`,
   AND send a `/offload` stateless dispatch (`~/ClaudeWorkspace/free-agents/bin/free-agent-tool.py`,
   with the playbook's brief verbatim, files attached via `"files"`) to an **out-of-family** reviewer
   (`avoid_family` = your own family, e.g. `mistral`). Treat findings as hypotheses: apply every
   proposed mutation in isolation and confirm it goes RED, check every claim against the real file
   (reviewers hallucinate), and fix or rebut each finding with evidence. Do not start the next task
   with an open finding.
6. **Declared ≠ delivered (playbook rule 6 applies to you: you are a free-lane executor).** Every
   milestone receipt holds ≥1 delivery-shaped probe on REAL state, with its raw output pasted into
   the receipt and attached to the dispatch so a different model judges it. Examples: the real
   `npm install --prefix` plus the real probe passing; `setup --dry-run` printed output; real
   `swiftformat` output on a real .swift file. "Fixed, verified" without quoted output = unverified.
7. Full-suite runs are parallel and redirected, never piped:
   `pytest -n auto -rs tests/ > /tmp/cc-gate.log 2>&1; echo EXIT:$?`. Read `EXIT:` and the summary
   line. A skip of a real-driver test is NOT a pass.
8. Resolve model/roster facts from the catalog when you need them (`free-agent-tool.py --list`),
   never from memory.
9. Never run `remote-session.sh --stop`, never start another remote session, never kill anything
   listening on 4141–4151 or 8000–8010 (one is the proxy you run on). If a coordinator text says
   "hand off to a new session", that successor is YOU: continue this doc.
10. Never edit the installed plugin cache (`~/.claude/plugins/cache/...`); refresh only via the ship.
11. Never type the expanded home path; write `~/` or `$HOME/`.
12. Keep the stream alive: the free lane cuts a request after ~300 s of silence. Run long commands
    (gate, ship) with `run_in_background` and wait for the notification; never sleep-poll.
13. Record progress in §6 at every milestone receipt, in the same commit as the work.

## 5. QC milestones

| M | After | Requirements under review (plan wording) | Delivery probe (raw output into receipt) |
|---|---|---|---|
| M1 | Task 1 | private `typescript@6` under `$CHECK_CODE_HOME/tsserver`; `_tsserver_lib` prefers it and skips the TS7 layout; the runner names the TS7 cause; worker runs `extra` steps and reports an npm failure | real `npm install --prefix <tmp>/tsserver typescript@6` + `test_default_probe_runs_the_real_driver_on_the_fixture` PASS (it failed before) |
| M2 | Task 2 | swiftlint gated on full Xcode (CLT ≠ Xcode); skipped + one notice with the exact copy; no daily retry; the gate lifts when Xcode appears; Xcode users see no change | `bin/check-code.py setup --dry-run --tier polyglot` on this machine (swiftlint = already installed) + a test-run transcript of the notice text |
| M3 | Task 3 | swiftformat in polyglot, parser on REAL captured output, conflict check vs swiftlint | real `swiftformat --lint` output on the fixture + the swiftlint run on the same file |
| M4 | Task 4, pre-ship | portable is a fresh build; README/CHANGELOG/plugin.json agree on 0.5.3; full gate green with 0 unexpected skips | the gate log `EXIT:0` line + `-rs` summary; `git diff origin/main...` attached to the dispatch |
| M5 | post-ship | live: get-haiggoh plan / `hooks/check-tag-visibility.py` shows 0.5.3; installed copy refreshed; dogfood | installed `check-code.py doctor` output showing typescript-lsp ready + a planted TS error reported on edit |

## 6. Progress log (append one line per receipt: date-time, milestone, commit, verdict)

- 2026-11-25 18:0x — handover written by coordinator (plan `9d32c89`).
- 2026-11-25 20:03 — M1/Task 1 (private typescript@6 lib) landed in this commit: `install_cmds`/`_run_installs` + `extra` on typescript-lsp, `_tsserver_lib` prefers `$CHECK_CODE_HOME/tsserver/...` over fallbacks and skips the TS7 layout, runner exits 2 naming `typescript@6` when no complete lib. Tests: 8 new RED→GREEN, both touched files 186/186 (EXIT:0). Mutations A–D each confirmed RED then restored (grep clean). Real-world proof: `npm install --prefix <mktemp>/tsserver typescript@6` exit 0, `_tsserver_lib()` resolved the private lib, `test_default_probe_runs_the_real_driver_on_the_fixture` PASSED under that home. Note: the handoff's expected real-world RED did NOT reproduce — this machine has a complete lib at intelephense's bundled typescript, so the probe passed even before the fix (verified by running it); the private-lib preference is proven by the resolved-lib probe instead.
- 2026-11-25 20:5x — M1 dispatch CLOSED: out-of-family validator (nvidia-nemotron-3-ultra, remote nvidia route) ruled R1–R5 PROVEN with outcome assertions, one Minor gap on R5 (no test for the stderr tail in the failing probe detail) — closed by test_failed_probe_detail_carries_the_last_stderr_line (real /bin/sh stderr) + mutation E (stderr append dropped) RED then restored. Receipt in the task commit.
- 2026-11-25 21:0x — M2/Task 2 (Xcode gate) landed in this commit: `requires` on the swiftlint entry with the exact notice copy, `xcode_present` (CLT ≠ Xcode, fail-closed), `requirement_unmet` (call-time lookup so tests can patch cc.xcode_present), setup_plan "skipped" + _print_plan label + cmd_setup never installs skipped, sync main says the reason once per linter (requires_notice[<id>]) and clears it when met, worker never installs/probes gated linters, --help/setup --help name the gate. 9 new tests RED→GREEN; test_check_code.py 176/176 EXIT:0. Mutations F (gate never unmet) → 5 RED, G (CLT accepted) → 1 RED, both restored (grep clean). Real probes: `setup --dry-run --tier polyglot` with real Xcode → swiftlint "already installed"; with a fake CLT-only xcode-select on PATH → "skipped (needs Xcode): swiftlint skipped: it needs the full Xcode app …" verbatim. Also carries the M1 R5 closure test (same file).
- 2026-11-25 21:5x — M2 dispatch CLOSED (commit 6a310d3 hardening): out-of-family validator (nvidia-glm-5.3-flash, remote nvidia, family zhipu) ruled R1–R3/R5–R7 PROVEN, R4 WEAK on "no daily retry" + fixture thinness. Closed by: notice asserted once per linter across >24 h with two gated linters plus a gated probe+binary-present linter kept out of `due`; mutations H (notice re-said every run) and I (unmet in `due`) each RED then restored (grep MUTATION = 0); gate text asserted on real `cli --help` / `cli setup --help` stdout; xcode-select nonzero/timeout fail-closed tests; `_sync_main` merged single-JSON entry-path test; `xcode_present=True` pinned in the polyglot-default test for host independence.
- 2026-11-25 22:0x — M3/Task 3 (swiftformat) landed in this commit: new BUILTIN entry after swiftlint (polyglot, .swift, brew, no Xcode needed), command `swiftformat --lint --reporter json --disable trailingCommas <file>` (JSON array on stdout; default reporter writes gcc-style to stderr), `_swiftformat_parse` -> (1-based line, rule_id, reason). CONFLICT MEASURED: swiftformat's default trailingCommas ADDS a trailing comma where swiftlint's default trailing_comma FORBIDS it (opposite verdicts, bad.swift line 6) -> rule disabled, pinned by test_swiftformat_output_introduces_no_swiftlint_violations (real binaries; derives its apply command from the catalog entry). Real fixture tests/fixtures/swiftformat-lint.txt captured from the real run. Real probe: `check bad.swift` reports swiftlint (identifier_name, redundant_void_return, control_statement) + swiftformat (redundantVoidReturnType, redundantParens), no trailingCommas, EXIT:1. Tests: 5 new/updated RED->GREEN; mutations J (trailingCommas re-enabled -> catalog+conflict tests RED) and K (parser drops all -> RED) restored, grep clean.
- 2026-11-25 22:3x — M3 dispatch CLOSED: out-of-family validator (nvidia-nemotron-3-ultra, remote nvidia) ruled R1/R2/R4 PROVEN, R3 WEAK (fixture reproducibility not pinned to the declared command). Closed by test_swiftformat_fixture_is_reproducible_by_the_declared_command (runs the catalog command on BAD_SWIFT, parses stdout, asserts == fixture parse); mutation L (drop --reporter json -> findings move to stderr -> parse == [] != fixture) RED then restored, grep MUTATION = 0; 5 swiftformat tests green.
- 2026-11-25 22:3x — M4 pre-ship: version surfaces bumped together to 0.5.3 (VERSION, plugin.json parsed OK, CHANGELOG entry, README rows); `git ls-remote --tags origin` latest = v0.5.2 so 0.5.3 is right; portable rebuilt via scripts/build-portable.py (7 files, EXIT:0), test_portable 10/10; FULL GATE `pytest -n auto -rs tests/` → EXIT:0, "216 passed in 40.09s", ZERO skips.

## 7. Next action

Invoke `run-to-completion:autopilot` with this single plan as the queue
(`docs/plans/2026-11-25-private-tsserver-and-swift-xcode-gate.md`; all tasks are do-now). Then
execute it with `superpowers:executing-plans` (native), starting at **Task 1, Step 1**, applying §4–§5
throughout, and finish with `run-to-completion:ship` (Task 4 Step 5) plus M5. Close out with
`run-to-completion:close-out-the-run`, and write the final wrap into §6.
