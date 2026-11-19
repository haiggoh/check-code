#!/usr/bin/env python3
"""check-code: lint the lines Claude just edited and hand the findings straight back.

A PostToolUse hook for Edit/Write/MultiEdit. It runs only linters that are already
installed, only on the edited file, and reports only findings on the lines that edit
touched, so existing debt elsewhere in the file stays quiet. It never blocks an edit.
Python 3.9 compatible (hooks may run under macOS /usr/bin/python3).
"""
import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

VERSION = "0.4.7"  # also in .claude-plugin/plugin.json and CHANGELOG.md
MAX_FINDINGS = 10
MARGIN = 2              # lines of context around an edit that still count as touched
LINT_TIMEOUT = 10       # seconds per linter run
LSP_TIMEOUT = 60        # LSP servers start a project-wide tsserver first; 10s is too short (2026-11-19: typescript-lsp timed out on a one-line file)
STATE_TTL = 2 * 86400
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin"]

# Noisy-by-default shellcheck codes, used only when the repo has no .shellcheckrc:
# SC1091 can't follow a sourced file; SC2016 is `$` in single quotes (usually on purpose);
# SC2034 is an unused variable (often a variable exported for another script).
SHELLCHECK_DEFAULT_EXCLUDE = "SC1091,SC2016,SC2034"
# Ruff rules used only when the repo has no ruff config. Pinned explicitly, because ruff's
# own default set changes between versions (0.16 enables ~400 rules, mostly style): syntax
# and runtime errors, pyflakes, bugbear, bare except.
RUFF_DEFAULT_SELECT = "E4,E7,E9,F,B,E722"
RUFF_CONFIGS = ("ruff.toml", ".ruff.toml")
CONFIG_FILE = "config.json"       # written by `setup`: {"tier": "basic"|"polyglot"|"none"}
TIERS = ("basic", "polyglot", "none")
# Owner decision 2026-10-29: polyglot is the DEFAULT, opt-out -- installs are small, and being
# prepared beats discovering a missing linter mid-edit. basic/none stay opt-in for users who
# know they will not use those languages.
DEFAULT_TIER = "polyglot"

# HELP_EPILOG is built from BUILTIN further down (after the catalog and install_hint),
# so the linter list in --help cannot drift from the catalog again (0.5.0).


# ---------------------------------------------------------------- config

def config_home():
    return Path(os.environ.get("CHECK_CODE_HOME", "~/.claude/check-code")).expanduser()


def find_binary(name):
    extra = os.pathsep.join(str(Path(p).expanduser()) for p in EXTRA_PATH)
    return shutil.which(name, path=os.environ.get("PATH", "") + os.pathsep + extra)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _find_upward(start, names, predicate=None):
    for folder in [start, *start.parents]:
        for name in names:
            path = folder / name
            if path.is_file() and (predicate is None or predicate(path)):
                return path
    return None


def _has_tool_ruff(path):
    try:
        return "[tool.ruff" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def recorded_tier():
    """The tier setup recorded in $CHECK_CODE_HOME/config.json, or None when setup never
    ran. An invalid recorded value reads as None here (it is not a choice the user made);
    doctor reports it loudly instead."""
    value = _load_json(config_home() / CONFIG_FILE, {}).get("tier")
    return value if value in TIERS else None


def tier_covers(recorded, entry_tier):
    """Does the recorded tier include a linter of entry_tier? User linters ('local') are
    always in tier."""
    if recorded == "none":
        return False
    if entry_tier == "local" or recorded == "polyglot":
        return True
    return entry_tier == "basic"


# ---------------------------------------------------------------- linters

def _ruff_command(path):
    cmd = ["ruff", "check", "--output-format", "json", "--no-cache", "--quiet"]
    if not (_find_upward(path.parent, RUFF_CONFIGS)
            or _find_upward(path.parent, ["pyproject.toml"], _has_tool_ruff)):
        cmd += ["--select", RUFF_DEFAULT_SELECT]
    return cmd + [str(path)]


def _ruff_parse(stdout):
    out = []
    for item in json.loads(stdout or "[]"):
        loc = item.get("location") or {}
        out.append((loc.get("row", 0), item.get("code") or "syntax", item.get("message", "")))
    return out


def _shellcheck_command(path):
    cmd = ["shellcheck", "-f", "json1"]
    if not _find_upward(path.parent, [".shellcheckrc", "shellcheckrc"]):
        cmd += ["-e", SHELLCHECK_DEFAULT_EXCLUDE]
    return cmd + [str(path)]


def _shellcheck_parse(stdout):
    data = json.loads(stdout or "{}")
    return [(c.get("line", 0), "SC%s" % c.get("code"), c.get("message", ""))
            for c in data.get("comments", [])]


_GCC_LINE = re.compile(r"^.*?:(\d+):(?:\d+:)?\s*(?:(?:error|warning|note|info)\s*:)?\s*(.*)$")


def _gcc_parse(stdout):
    """Parse `file:line[:col]: [severity:] message [(rule)]` lines, the de-facto lint format."""
    out = []
    for line in (stdout or "").splitlines():
        m = _GCC_LINE.match(line)
        if m:
            text = m.group(2).strip()
            rule = re.search(r"\(([\w./-]+)\)\s*$", text)
            out.append((int(m.group(1)), rule.group(1) if rule else "lint",
                        text[:rule.start()].strip() if rule else text))
    return out


_ACTIONLINT_RULE = re.compile(r"\[([\w/.-]+)\]\s*$")

# actionlint's default output interleaves code-snippet blocks on stdout, and a snippet source
# line containing `foo:123:` would itself match _gcc_parse; -format emits finding lines only.
ACTIONLINT_FORMAT = ('{{range .}}{{printf "%s:%d:%d: %s [%s]" '
                     '.Filepath .Line .Column .Message .Kind}}{{println}}{{end}}')


def _actionlint_parse(stdout):
    """Parse the -format lines: rules live in trailing [square brackets], not (parens)."""
    out = []
    for line_no, code, message in _gcc_parse(stdout):
        m = _ACTIONLINT_RULE.search(message)
        if m:
            code, message = m.group(1), message[:m.start()].strip()
        out.append((line_no, code, message))
    return out


def _biome_parse(stdout):
    """Parse biome's checkstyle XML; the rule is the last segment of source
    (lint/suspicious/noDebugger -> noDebugger)."""
    out = []
    for err in ET.fromstring(stdout or "<checkstyle/>").iter("error"):
        source = err.get("source") or "biome"
        out.append((int(err.get("line") or 0), source.rsplit("/", 1)[-1],
                    err.get("message") or ""))
    return out


def _tslsp_parse(stdout):
    """Parse typescript-language-server publishDiagnostics JSON lines (one per notification).

    Each diagnostic: range.start.line is 0-based -> +1; severity 1=error 2=warning (3/4 pass
    through as lint); code is the ts error code, used as the rule name."""
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") != "textDocument/publishDiagnostics":
            continue
        for d in (msg.get("params") or {}).get("diagnostics") or []:
            start = ((d.get("range") or {}).get("start") or {})
            out.append((int(start.get("line") or 0) + 1,
                        str(d.get("code") or "ts"),
                        (d.get("message") or "").splitlines()[0] if d.get("message") else ""))
    return out


def _tslsp_command(path):
    """Drive typescript-language-server over stdio for ONE file's diagnostics.

    The server speaks LSP (Content-Length framed); the runner in scripts/tslsp-lint.py does the
    framing and prints one publishDiagnostics JSON per line, which _tslsp_parse reads."""
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "scripts" / "tslsp-lint.py", here / "scripts" / "tslsp-lint.py"):
        if cand.is_file():
            return ["python3", str(cand), str(path)]
    return ["python3", "-c", "import sys; sys.exit(2)", str(path)]  # runner missing: loud, not silent


def _rubocop_parse(stdout):
    """Parse rubocop's --lint --format json: the code is cop_name; the message
    repeats the cop as a prefix, which is stripped."""
    out = []
    for f in json.loads(stdout or "{}").get("files", []):
        for off in f.get("offenses", []):
            code = off.get("cop_name") or "Lint"
            message = off.get("message") or ""
            if message.startswith(code + ": "):
                message = message[len(code) + 2:]
            out.append(((off.get("location") or {}).get("line", 0), code, message))
    return out


# The catalog: every entry is VERIFIED to lint one file with machine-parseable output
# (see docs/plans/2026-10-28-setup-tiers-and-coverage-notice.md, verification record).
# tier: basic = ruff+shellcheck, polyglot = everything; local user linters are always in tier.
# install names the package manager setup drives (never sudo). rubocop via brew, not gem:
# Homebrew ruby's gem bin dir is off PATH, so a gem install could never verify by outcome.
BUILTIN = [
    {"id": "ruff", "language": "Python", "binary": "ruff", "tier": "basic",
     "install": {"manager": "pipx", "cmd": ["pipx", "install", "ruff"]},
     "ext": ["py", "pyi"], "shebang": r"python[0-9.]*\b",
     "command": _ruff_command, "parse": _ruff_parse},
    {"id": "shellcheck", "language": "shell", "binary": "shellcheck", "tier": "basic",
     "install": {"manager": "brew", "cmd": ["brew", "install", "shellcheck"]},
     "ext": ["sh", "bash", "ksh"], "shebang": r"\b(?:ba|k|da)?sh\b",
     "command": _shellcheck_command, "parse": _shellcheck_parse},
    {"id": "biome", "language": "TS/JS", "binary": "biome", "tier": "polyglot",
     "install": {"manager": "brew", "cmd": ["brew", "install", "biome"]},
     "ext": ["ts", "tsx", "js", "jsx"], "shebang": None,
     "command": lambda path: ["biome", "lint", "--reporter=checkstyle", str(path)],
     "parse": _biome_parse},
    {"id": "swiftlint", "language": "Swift", "binary": "swiftlint", "tier": "polyglot",
     "install": {"manager": "brew", "cmd": ["brew", "install", "swiftlint"]},
     "ext": ["swift"], "shebang": None,
     "command": lambda path: ["swiftlint", "lint", "--no-cache", str(path)],
     "parse": _gcc_parse},
    {"id": "rubocop", "language": "Ruby", "binary": "rubocop", "tier": "polyglot",
     "install": {"manager": "brew", "cmd": ["brew", "install", "rubocop"]},
     "ext": ["rb"], "shebang": None,
     # --lint = the Lint department: the pinned conservative error-cop set, as ruff is pinned.
     "command": lambda path: ["rubocop", "--lint", "--format", "json", str(path)],
     "parse": _rubocop_parse},
    {"id": "typescript-lsp", "language": "TS (LSP)", "binary": "typescript-language-server",
     "tier": "polyglot",
     # brew's typescript-language-server ships only cli.mjs and its sibling typescript
     # package can be a stub, so a bare install can be present but BROKEN (noServer on
     # every request): the typescript lib installs with it, and the probe below is what
     # decides "installed" (a functional probe, not a binary on PATH).
     "install": {"manager": "brew",
                 "cmd": ["brew", "install", "typescript-language-server", "typescript"]},
     "ext": ["ts", "tsx", "js", "jsx", "mts", "cts", "mjs", "cjs"], "shebang": None,
     "probe": {"fixture": "scripts/probe/bad.ts", "expect_code": "2322"},
     "command": _tslsp_command, "parse": _tslsp_parse},
    {"id": "actionlint", "language": "Actions", "binary": "actionlint", "tier": "polyglot",
     "install": {"manager": "brew", "cmd": ["brew", "install", "actionlint"]},
     "ext": [], "shebang": None,
     # matched by path, never by yml/yaml extension: actionlint would "lint" any yaml as a
     # broken workflow (verified on a docker-compose file)
     "path_regex": r"\.github/workflows/[^/]+\.ya?ml$",
     "command": lambda path: ["actionlint", "-format", ACTIONLINT_FORMAT, str(path)],
     "parse": _actionlint_parse},
]


def install_hint(linter):
    """Display form of the install command (local linters carry a plain string; a malformed
    entry degrades to what it holds instead of crashing a display path)."""
    install = linter.get("install") or ""
    if isinstance(install, str):
        return install
    cmd = install.get("cmd", [])
    return cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)


# ------------------------------------------- help/menu text, generated FROM the catalog
# The linter lists shown by --help, the setup menu and setup --help used to be hand-copied,
# and they drifted: 0.4.6 added typescript-lsp to BUILTIN but to none of the three texts.
# Every line below reads BUILTIN, so a new entry appears in all three by construction.

def _tier_ids(tier):
    """Every linter the tier covers (polyglot includes the basic ones — same rule
    setup_plan installs by), in catalog order."""
    return [lin["id"] for lin in BUILTIN if tier_covers(tier, lin["tier"])]


def _tier_languages(tier):
    """The covered linters' language labels, in catalog order, each once."""
    langs = []
    for lin in BUILTIN:
        if tier_covers(tier, lin["tier"]) and lin["language"] not in langs:
            langs.append(lin["language"])
    return langs


def _tier_summary(tier):
    """'ruff + shellcheck' — the tier's linter ids joined for prose."""
    return " + ".join(_tier_ids(tier))


def _linter_help_line(lin):
    """One 'linters' help line for a catalog entry. No-arg derivations only, so the text
    can never outrun the catalog: language label, id, install hint, then what it matches."""
    match = []
    if lin.get("ext"):
        match.append("/".join("." + e for e in lin["ext"]))
    if lin.get("shebang"):
        match.append("or a matching shebang")
    if lin.get("path_regex"):
        match.append("matched by path " + lin["path_regex"])
    detail = "   (%s)" % ", ".join(match) if match else ""
    return "  %-9s %-15s install: %-40s%s" % (lin["language"].lower(), lin["id"],
                                              install_hint(lin), detail)


def _wrap(text, indent=12, width=96):
    return textwrap.fill(text, width=width, subsequent_indent=" " * indent,
                         break_on_hyphens=False)


def _build_help_epilog():
    linter_lines = "\n".join(_linter_help_line(lin) for lin in BUILTIN)
    poly = _tier_summary("polyglot")
    basic = _tier_summary("basic")
    return """\
linters (each runs only if installed):
%s
  extra linters: add them to $CHECK_CODE_HOME/linters.local.json (see README)
setup (the preferred way to get them):
%s
%s
  setup --tier none      no external linters; built-in checks stay on, coverage notices go quiet
  Run it in a terminal for the menu; --dry-run shows the plan without changing anything.
  The chosen tier is recorded in $CHECK_CODE_HOME/config.json ("setup --help" has the details).
built-in checks (always on, no linter needed):
  STDERR-PIPE  python Popen/call/check_call with stderr=PIPE that nothing in the file drains
  NO-HELP      a Write of a new script file with no argument parsing / --help handling
  HARDCODED-PATH  a literal /Users/<name>/ home, ClaudeWorkspace/<name> checkout, or
               plugin-cache VERSION path on a touched line — use ~/…, $HOME/… or an env
               override instead ($TMP-built test-fixture lines, CHANGELOGs and plan/doc
               prose are exempt; skills/*.md still checked — it ships)
  SKILL-BUDGET a SKILL.md whose body after frontmatter exceeds 1500 words
               (CHECK_CODE_SKILL_BUDGET_WORDS overrides; once per file per session)
  MENU-NAV     a menu row giving NAVIGATION a number ("5) Exit"): house grammar is
               numbers = actions, letters = navigation (q Quit, b Back)

environment:
  CHECK_CODE_OFF=1        disable the hook
  CHECK_CODE_MAX          max findings reported per edit (default 10)
  CHECK_CODE_HOME         config dir (default ~/.claude/check-code)
  CHECK_CODE_DEBUG=1      log hook exceptions to $CHECK_CODE_HOME/debug.log
""" % (linter_lines,
       _wrap("  setup --tier polyglot  %s -- the recommended default: Enter in the menu, and "
             "what 'setup --yes' installs when --tier is not given" % poly),
       _wrap("  setup --tier basic     %s only -- opt in if you know you will not touch the "
             "polyglot languages" % basic))


HELP_EPILOG = _build_help_epilog()


def load_linters():
    """Catalog plus user linters from linters.local.json (same fields; command is a list
    with "{file}" as the placeholder, output parsed as gcc-style lines)."""
    linters = [dict(item) for item in BUILTIN]
    local = _load_json(config_home() / "linters.local.json", {})
    disabled = set(local.get("disabled", []))
    for entry in local.get("linters", []):
        if not entry.get("id") or not entry.get("command"):
            continue
        template = [str(part) for part in entry["command"]]
        linters.append({
            "id": entry["id"], "language": entry.get("language", entry["id"]),
            "binary": entry.get("binary", template[0]), "install": entry.get("install", ""),
            "tier": "local", "ext": [e.lstrip(".") for e in entry.get("ext", [])],
            "shebang": entry.get("shebang"),
            "command": (lambda path, t=template: [str(path) if p == "{file}" else p for p in t]),
            "parse": _gcc_parse})
    return [lin for lin in linters if lin["id"] not in disabled]


def _shebang(path):
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200).decode("utf-8", "replace")
    except OSError:
        return ""
    return first if first.startswith("#!") else ""


def linters_for(path, linters):
    ext = path.suffix.lstrip(".").lower()
    shebang = None
    hits = []
    for linter in linters:
        path_re = linter.get("path_regex")
        if path_re:  # path-matched entries (e.g. workflow files) ignore ext and shebang
            if re.search(path_re, str(path)):
                hits.append(linter)
            continue
        if ext and ext in linter["ext"]:
            hits.append(linter)
        elif not ext and linter.get("shebang"):
            shebang = _shebang(path) if shebang is None else shebang
            if shebang and "zsh" not in shebang and re.search(linter["shebang"], shebang):
                hits.append(linter)
    return hits


def run_linter(linter, path):
    """Return [(line, code, message)]; raises on timeout or unparseable output."""
    cmd = linter["command"](path)
    cmd[0] = find_binary(cmd[0]) or cmd[0]
    timeout = LSP_TIMEOUT if "lsp" in linter["id"] else LINT_TIMEOUT
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=str(path.parent))
    return linter["parse"](proc.stdout)


def _default_probe(linter):
    """The real functional probe: run the linter's own command on the catalog's probe
    fixture and require the expected code in the parsed findings. Returns (ok, detail).
    This is what separates a WORKING linter from a present-but-broken one."""
    probe = linter.get("probe") or {}
    fixture = (Path(__file__).resolve().parent.parent / str(probe.get("fixture") or "")).resolve()
    if not fixture.is_file():
        return False, "probe fixture missing: %s" % fixture
    cmd = linter["command"](fixture)
    cmd[0] = find_binary(cmd[0]) or cmd[0]
    timeout = LSP_TIMEOUT if "lsp" in linter["id"] else LINT_TIMEOUT
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(fixture.parent))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "probe crashed: %s" % exc
    codes = [str(code) for _line, code, _msg in linter["parse"](proc.stdout)]
    expect = str(probe.get("expect_code") or "")
    if expect and expect in codes:
        return True, "probe ok (%s reported)" % expect
    return False, "probe did not report %s (got: %s)" % (expect, ", ".join(codes) or "nothing")


def linter_ready(linter, probe_runner=None):
    """A linter is ready only if its binary resolves AND its functional probe passes (when
    the catalog defines one). A present-but-broken binary — e.g. typescript-language-server
    whose typescript lib is a stub, so every tsserverRequest answers noServer — is NOT
    ready. probe_runner is injectable so tests never start a real linter."""
    if not find_binary(linter["binary"]):
        return False
    if "probe" not in linter:
        return True
    runner = probe_runner or _default_probe
    ok, _detail = runner(linter)
    return ok


# ---------------------------------------------------------------- touched lines

def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def touched_ranges(tool_name, tool_input, content):
    """[(first, last)] line ranges the edit touched, or None meaning the whole file."""
    if tool_name == "Write":
        return None
    edits = tool_input.get("edits") if tool_name == "MultiEdit" else [tool_input]
    ranges = []
    for edit in edits or []:
        new = edit.get("new_string")
        if new is None:
            continue
        if new == "":
            continue  # a pure deletion leaves nothing new to lint
        start = content.find(new)
        if start < 0:
            return None  # the text moved again (formatter, later edit): be safe, lint it all
        while start >= 0:
            first = _line_of(content, start)
            ranges.append((first, first + new.count("\n")))
            if not edit.get("replace_all"):
                break
            start = content.find(new, start + max(1, len(new)))
    return ranges


def in_ranges(line, ranges):
    return ranges is None or any(a - MARGIN <= line <= b + MARGIN for a, b in ranges)


# ---------------------------------------------------------------- built-in checks
# check-code's own pattern checks: stdlib only, no external linter, still filtered to the
# touched lines, still never blocking.

STDERR_PIPE_MESSAGE = (
    "subprocess call with stderr=PIPE is never drained: once ~64KB of child stderr fills the "
    "OS pipe buffer, the child blocks on its next stderr write and hangs for good (pacing "
    "and retry do not help -- it is output volume, not timing). Read it (proc.communicate(), "
    "a stderr reader) or sink it (stderr=subprocess.DEVNULL, or a log file).")

NO_HELP_MESSAGE = (
    "new script file shows no argument parsing or --help handling. If it takes arguments, "
    "parse them before doing any work, exit non-zero on an unknown flag, and support --help. "
    "Verify by RUNNING '%s --help' -- a source grep proves nothing: argparse supplies "
    "-h/--help with the string nowhere in the source, and a script that ignores --help runs "
    "its real work on the probe.")

HELP_SIGNALS = ("argparse", "optparse", "getopt", "getopts", "click", "typer", "docopt",
                "sys.argv", "--help", "$#", "usage")
SCRIPT_EXTS = {"py", "sh", "bash", "ksh", "zsh"}
# a script that declares itself a sourced library, in its first lines, is not run by hand
SOURCED_HEADER_LINES = 15
SOURCED_MARKERS = ("sourced by", "not executable on its own", "meant to be sourced",
                   "source this file")

HARDCODED_PATH_MESSAGE = (
    "hardcoded machine-specific path in a committed file: it is wrong for every other reader "
    "(another machine, a plugin-cache version that moved on, a checkout that lives elsewhere) "
    "and silently breaks portability. Write ~/… or $HOME/… (the expanded form only where a "
    "file format demands it), take the location from an env override or a self-discovery "
    "helper, and never pin a plugin-cache VERSION path — resolve the newest at call time.")

SKILL_BUDGET_MESSAGE = (
    "SKILL.md body is %d words (budget %d): skills stay lean — keep imperative rules and "
    "steps; move incidents, rationale, worked examples to references/ or docs/.")

MENU_NAV_MESSAGE = (
    "menu gives NAVIGATION a number: house menu grammar is numbers = actions, letters = "
    "navigation — q Quit (top-level/standalone screens only), b Back (screens reached from a "
    "parent, never both on one screen), Enter = the shown default. Put this row on a letter "
    "(see free-agents bin/session_picker_model.py for the majority convention).")

CACHE_PATH = re.compile(r"plugins/cache/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\d+(?:\.\d+)+")
WORKSPACE_PATH = re.compile(r"ClaudeWorkspace/[A-Za-z0-9_.-]+")
HOME_PATH = re.compile(r"/Users/(?!Shared/)[A-Za-z0-9_.-]+/")
FIXTURE_MARKERS = ("$TMP", "tmp_path", "TMPDIR", "tempfile")
MENU_NAV = re.compile(r"(?:print|echo|_box).*?\b\d\)\s*(?:Exit|Quit|Back|Cancel)\b")
DEFAULT_SKILL_BUDGET_WORDS = 1500
FILE_LEVEL_CODES = {"NO-HELP", "SKILL-BUDGET"}  # whole-file properties: never range-filtered


def _dotted(func):
    """Dotted name of a call target ('subprocess.Popen', 'Popen', ...), or ''."""
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _pipe_value(value):
    return _dotted(value) in ("subprocess.PIPE", "PIPE")


def _bound_names(tree):
    """id(Call) -> variable name, for calls whose result is bound to a plain variable."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(id(node.value), target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Call):
            if isinstance(node.target, ast.Name):
                names.setdefault(id(node.value), node.target.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if (isinstance(item.context_expr, ast.Call)
                        and isinstance(item.optional_vars, ast.Name)):
                    names.setdefault(id(item.context_expr), item.optional_vars.id)
    return names


def _stderr_reads_or_hidden(tree, name):
    """True if the file reads that handle's stderr, or passes the object out of sight."""
    attr_bases = set()
    drained = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attr_bases.add(id(node.value))
            if node.value.id == name and node.attr in ("communicate", "stderr"):
                drained = True
    if drained:
        return True
    for node in ast.walk(tree):  # used as a bare value (argument, store, return): can't tell
        if (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
                and id(node) not in attr_bases):
            return True
    return False


def _stderr_pipe_lines(content):
    """Line numbers of Popen/call/check_call with stderr=PIPE that the file never drains.
    subprocess.run and check_output drain their pipes themselves, so they are never flagged."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []
    names = _bound_names(tree)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        popen = dotted in ("subprocess.Popen", "Popen")
        waits = dotted in ("subprocess.call", "subprocess.check_call", "call", "check_call")
        if not popen and not waits:
            continue
        kw = dict((k.arg, k.value) for k in node.keywords if k.arg is not None)
        if not _pipe_value(kw.get("stderr")):
            continue
        if popen:  # call/check_call return only a code: their pipe can never be read
            name = names.get(id(node))
            if name and _stderr_reads_or_hidden(tree, name):
                continue
        lines.append(node.lineno)
    return lines


def _not_meant_to_run(path, content):
    """True for files nobody invokes by hand, so --help would be meaningless: pytest-collected
    tests and sourced libraries. The sourced-library claim is read from the HEADER only --
    a body comment that merely mentions 'sourced by' must not exempt a runnable script."""
    name = path.name.lower()
    if (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py"):
        return True
    if any(part.lower() in ("tests", "test") for part in path.parent.parts):
        return True
    header = "\n".join(content.splitlines()[:SOURCED_HEADER_LINES]).lower()
    return any(marker in header for marker in SOURCED_MARKERS)


def _is_new_script(path, content, shebang):
    ext = path.suffix.lstrip(".").lower()
    if ext not in SCRIPT_EXTS and not shebang:
        return False
    if _not_meant_to_run(path, content):
        return False
    pythonish = ext == "py" or "python" in shebang
    if pythonish and not shebang and "__main__" not in content:
        return False  # a library module, not a runnable script
    return True


def _skill_budget_words():
    """(budget, env_error): the documented default, or the CHECK_CODE_SKILL_BUDGET_WORDS
    override. An override that is SET but invalid is reported, never silently ignored."""
    raw = os.environ.get("CHECK_CODE_SKILL_BUDGET_WORDS")
    if not raw:
        return DEFAULT_SKILL_BUDGET_WORDS, None
    try:
        return max(1, int(raw)), None
    except ValueError:
        return (DEFAULT_SKILL_BUDGET_WORDS,
                "CHECK_CODE_SKILL_BUDGET_WORDS=%r is not an integer -- using %d"
                % (raw, DEFAULT_SKILL_BUDGET_WORDS))


def _skill_body_after_frontmatter(content):
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end >= 0:
            return content[end + 4:]
    return content


def _in_git_repo(path):
    """True when the file lives inside a git repo. `.git` is a DIRECTORY in a normal repo
    but a FILE in a worktree, so existence — not is_file — is the test."""
    for folder in [path.parent, *path.parent.parents]:
        if (folder / ".git").exists():
            return True
    return False


def _hardcoded_path_lines(content):
    out = []
    for i, line in enumerate(content.splitlines(), 1):
        if any(marker in line for marker in FIXTURE_MARKERS):
            continue
        if CACHE_PATH.search(line) or WORKSPACE_PATH.search(line) or HOME_PATH.search(line):
            out.append(i)
    return out


DOC_SUFFIXES = {".md", ".txt", ".rst"}


def _portability_checked(path, shebang):
    """HARDCODED-PATH is a CODE rule. Plan/doc prose may legitimately name THIS machine's local
    state (a ~/.claude/plans entry citing a worktree path is correct there) — measured
    2026-11-03: the hook flagged a plans/*.md line (waypoint check-code-hardcoded-path).
    Published skill docs stay checked: skills/*.md ships to every reader."""
    if "skills" in path.parts:
        return True
    return bool(shebang) or path.suffix.lower() not in DOC_SUFFIXES


def _menu_nav_lines(content):
    return [i for i, line in enumerate(content.splitlines(), 1) if MENU_NAV.search(line)]


def builtin_findings(path, content, tool_name, session_id):
    """(line, code, message) from check-code's own checks; a clean file costs one scan."""
    out = []
    shebang = _shebang(path)
    if path.suffix.lstrip(".").lower() == "py" or "python" in shebang:
        for line in _stderr_pipe_lines(content):
            out.append((line, "STDERR-PIPE", STDERR_PIPE_MESSAGE))
    if (tool_name == "Write" and _is_new_script(path, content, shebang)
            and not any(signal in content for signal in HELP_SIGNALS)
            and _note_once(session_id, "nohelp:" + str(path))):
        out.append((1, "NO-HELP", NO_HELP_MESSAGE % path.name))
    if "changelog" not in path.name.lower() and _in_git_repo(path) and _portability_checked(path, shebang):
        for line in _hardcoded_path_lines(content):
            out.append((line, "HARDCODED-PATH", HARDCODED_PATH_MESSAGE))
    budget, env_error = _skill_budget_words()
    if env_error and _note_once(session_id, "skillbudgetenv"):
        out.append((1, "SKILL-BUDGET", env_error))
    if path.name == "SKILL.md":
        body = _skill_body_after_frontmatter(content)
        if len(body.split()) > budget and _note_once(session_id, "skillbudget:" + str(path)):
            out.append((1, "SKILL-BUDGET", SKILL_BUDGET_MESSAGE % (len(body.split()), budget)))
    for line in _menu_nav_lines(content):
        out.append((line, "MENU-NAV", MENU_NAV_MESSAGE))
    return out


# ---------------------------------------------------------------- session state

def _state_file(session_id):
    safe = re.sub(r"[^\w.-]", "_", session_id or "nosession")[:80]
    return Path(tempfile.gettempdir()) / "check-code" / (safe + ".json")


def _note_once(session_id, key):
    """True the first time `key` is seen this session."""
    path = _state_file(session_id)
    seen = set(_load_json(path, []))
    if key in seen:
        return False
    seen.add(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        now = time.time()
        for old in path.parent.glob("*.json"):
            if now - old.stat().st_mtime > STATE_TTL:
                old.unlink()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- hook

def _cap():
    try:
        return max(1, int(os.environ.get("CHECK_CODE_MAX", MAX_FINDINGS)))
    except ValueError:
        return MAX_FINDINGS


def _format_findings(findings, ranges, path):
    findings = sorted(set(findings))
    cap = _cap()
    where = "the lines you just edited in" if ranges is not None else "the file you just wrote,"
    lines = ["🩻 check-code: %d issue(s) in %s %s (as of THIS edit -- a sibling edit in the same "
             "batch may already fix them) -- fix them now, or say why a finding is "
             "intentional:" % (len(findings), where, path.name)]
    lines += ["  L%d %s %s" % f for f in findings[:cap]]
    if len(findings) > cap:
        lines.append("  +%d more (run the linter on the file to see all)" % (len(findings) - cap))
    return "\n".join(lines)


COVERAGE_NOTICE_C3 = ('check-code: %s edits are not linted — missing linters install '
                      'automatically at the next session start ("setup" picks a tier now, '
                      '"setup --tier none" silences this).')
COVERAGE_NOTICE_C4 = ('check-code: %s is missing but your "%s" tier includes it — it installs '
                      'automatically at the next session start ("setup" can do it now).')


def _coverage_notice(path, candidates, recorded, session_id):
    """The quiet coverage notice (plan section C); first rule that applies decides.
    Reached only when no installed linter covered the file and nothing else will print.
    C1 no catalog entry covers the file type / C2 the recorded tier excludes it -> silent.
    C3 setup never ran -> ONE line, once per session in TOTAL. C4 the tier includes the
    linter but it went missing -> one line, once per session per linter, pointing at
    setup, never at a per-tool install command."""
    if not candidates:
        return ""
    in_tier = [lin for lin in candidates
               if recorded is None or tier_covers(recorded, lin["tier"])]
    if not in_tier:
        return ""
    if recorded is None:
        if not _note_once(session_id, "coverage"):
            return ""
        label = ("." + path.suffix.lstrip(".")) if path.suffix else candidates[0]["language"].lower()
        return COVERAGE_NOTICE_C3 % label
    for lin in in_tier:
        if _note_once(session_id, "tiermissing:" + lin["id"]):
            return COVERAGE_NOTICE_C4 % (lin["id"], recorded)
    return ""


def check_edit(payload):
    """Return the feedback text for one PostToolUse payload, or '' for nothing to say."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.is_file():
        return ""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    content = path.read_text(encoding="utf-8", errors="replace")
    ranges = touched_ranges(tool_name, tool_input, content)
    findings = [f for f in builtin_findings(path, content, tool_name, session_id)
                if f[1] in FILE_LEVEL_CODES or in_ranges(f[0], ranges)]
    candidates = linters_for(path, load_linters())
    recorded = recorded_tier()
    if recorded == "none":
        candidates = []  # an explicit choice: no external linters run, nothing to notice
    if not candidates and not findings:
        return ""
    installed = [lin for lin in candidates if find_binary(lin["binary"])]
    if not installed:
        if findings:  # the built-in checks need no installed linter
            return _format_findings(findings, ranges, path)
        return _coverage_notice(path, candidates, recorded, session_id)
    for linter in installed:
        for line, code, message in run_linter(linter, path):
            if in_ranges(line, ranges):
                findings.append((line, code, message))
    if not findings:
        return ""
    return _format_findings(findings, ranges, path)


def run_hook(stdin):
    """Entry point for the hook: never raises, never blocks, exit code always 0."""
    if os.environ.get("CHECK_CODE_OFF") == "1":
        return 0
    try:
        raw = stdin.read()
        text = check_edit(json.loads(raw) if raw.strip() else {})
        if text:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": text}}) + "\n")
    except Exception as exc:  # fail open by design
        if os.environ.get("CHECK_CODE_DEBUG") == "1":
            try:
                log = config_home() / "debug.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write("%s: %r\n" % (time.strftime("%F %T"), exc))
            except OSError:
                pass
    return 0


# ---------------------------------------------------------------- setup
# The preferred route: choose a coverage tier up front and install its linters, instead of
# discovering a missing dependency mid-edit. Only the catalog is managed here; a user's own
# linters.local.json entries are theirs. Never sudo, never a system-package override.

INSTALL_TIMEOUT = 600  # seconds per install command (a brew bottle can be large)

def _build_setup_menu():
    head = ("🩻🔬 Check Code\n"
            "   Lints the lines Claude just edited\n"
            + "=" * 72 + "\n\n"
            + "check-code setup — choose linter coverage\n\n")
    items = [
        "  1) polyglot  %s: %s -- recommended, and the default (Enter)"
        % (_tier_summary("polyglot"), ", ".join(_tier_languages("polyglot"))),
        "  2) basic     %s only: %s"
        % (_tier_summary("basic"), ", ".join(_tier_languages("basic"))),
        "  3) none      no external linters; built-in checks still run and coverage notices stay silent",
    ]
    body = "\n".join(_wrap(item, indent=15) for item in items)
    return head + body + "\n\n  Enter = polyglot    q) Quit\n"


SETUP_MENU = _build_setup_menu()


def _build_setup_epilog():
    poly = _wrap("  polyglot  %s (%s) -- the recommended default: Enter in the menu, and what "
                 "'setup --yes' installs when --tier is not given"
                 % (_tier_summary("polyglot"), ", ".join(_tier_languages("polyglot"))))
    basic = _wrap("  basic     %s (%s) -- opt in if you know you will not touch the polyglot "
                  "languages"
                  % (_tier_summary("basic"), ", ".join(_tier_languages("basic"))))
    return """\
tiers:
%s
%s
  none      no external linters: built-in checks still run and coverage notices stay silent
interactive (stdin is a TTY): choose a tier (Enter = polyglot, the shown default), see the
plan, confirm. Installs use the package manager the catalog names (brew, pipx) and never sudo;
a missing package manager prints the command to run yourself instead of failing. After each
install the binary is re-resolved, so the report is installed / already present / FAILED, not
the installer's exit code.
environment:
  CHECK_CODE_HOME   config dir holding config.json (the recorded tier) and linters.local.json
                    (default ~/.claude/check-code)
""" % (poly, basic)


SETUP_EPILOG = _build_setup_epilog()


def _isatty():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _run_install(cmd):
    """Run one install command: (ok, reason). THE injectable runner — tests replace it so no
    test ever runs a real installer."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    reason = lines[-1].strip() if lines else "exited %d" % proc.returncode
    return proc.returncode == 0, reason


def setup_plan(tier, probe_runner=None):
    """[(linter, status, detail)] for the tier: installed / install / cannot.
    "installed" means READY (binary resolves AND the catalog's probe passes), not merely
    on PATH — a present-but-broken linter is planned as "install"."""
    if tier == "none":
        return []
    plan = []
    for linter in [dict(item) for item in BUILTIN]:
        if not tier_covers(tier, linter["tier"]):
            continue
        if linter_ready(linter, probe_runner):
            plan.append((linter, "installed", find_binary(linter["binary"])))
        elif find_binary(linter["install"]["manager"]):
            plan.append((linter, "install", install_hint(linter)))
        else:
            plan.append((linter, "cannot", "%s is not installed here -- run '%s' yourself"
                         % (linter["install"]["manager"], install_hint(linter))))
    return plan


def _print_plan(tier, plan):
    print("check-code setup -- tier: %s" % tier)
    if tier == "none":
        print("  no external linters will be installed or run; built-in checks stay on and the "
              "missing-linter notice stays silent")
        return
    for linter, status, detail in plan:
        print("  %-11s %s: %s" % (linter["id"],
                                  {"installed": "already installed",
                                   "install": "will install",
                                   "cannot": "cannot install here"}[status], detail))


def record_tier(tier):
    """Write (or overwrite) the recorded tier; setup re-runs may change it and other config
    keys are preserved."""
    path = config_home() / CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    config = _load_json(path, {})
    config["tier"] = tier
    path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")


def _confirm(count, read_line=None):
    read_line = read_line or input
    try:
        answer = read_line("install %d linter(s)? [y/N] " % count)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _prompt_tier(read_line=None):
    """Interactive tier choice: numbers = actions, letters = navigation, Enter = the shown
    default (polyglot). Returns the tier, or None when the user quits."""
    read_line = read_line or input
    print(SETUP_MENU)
    choices = {"1": "polyglot", "2": "basic", "3": "none", "": DEFAULT_TIER}
    while True:
        try:
            answer = read_line("choose 1, 2, 3 [Enter = polyglot, q = quit]: ").strip().lower()
        except EOFError:
            return None
        if answer in ("q", "quit"):
            return None
        if answer in choices:
            return choices[answer]
        print("not a choice: 1, 2, 3, Enter, or q")


def cmd_setup(args):
    if args.dry_run:
        tier = args.tier or DEFAULT_TIER   # a probe looks at the default's plan; changes nothing
    elif args.tier is None:
        if _isatty() and not args.yes:
            tier = _prompt_tier()      # the menu: Enter = polyglot, the opt-out default
            if tier is None:
                print("nothing changed")
                return 0
        elif args.yes:
            tier = DEFAULT_TIER        # confirmed by --yes: the recommended default
        else:
            print("setup: the menu needs a TTY; pass --tier basic|polyglot|none with --yes "
                  "for scripts", file=sys.stderr)
            return 2
    else:
        tier = args.tier
    plan = setup_plan(tier)
    _print_plan(tier, plan)
    if args.dry_run:
        print("dry run: nothing installed, nothing recorded")
        return 0
    if not args.yes:
        if not _isatty():
            print("setup: --yes is required when stdin is not a TTY (or use --dry-run)",
                  file=sys.stderr)
            return 2
        pending = [entry for entry in plan if entry[1] == "install"]
        if pending and not _confirm(len(pending)):
            print("aborted: nothing installed, nothing recorded")
            return 0
    for linter, status, reason in plan:
        if status != "install":
            continue
        _run_install(linter["install"]["cmd"])
        # verify by outcome, not by exit code: ready = binary AND probe, not just on PATH
        if linter_ready(linter):
            print("  %-11s installed: %s" % (linter["id"], find_binary(linter["binary"])))
        else:
            print("  %-11s FAILED: %s" % (linter["id"], reason or
                                          "the installer reported success but the linter is "
                                          "still not functional (probe did not pass)"))
    record_tier(tier)
    print("tier recorded: %s (%s)" % (tier, config_home() / CONFIG_FILE))
    print('coverage applies from the next edit; "check-code.py doctor" shows what is installed')
    return 0


# ---------------------------------------------------------------- sync (SessionStart)
# The install path for people who never ran setup: at every session start this prints
# nothing (fast, < 1 s, never blocks) unless a previous background worker has results to
# report (once) or the fast binary check finds tier linters missing — then it spawns ONE
# detached worker and goes quiet. The worker (never the hook) probes and installs.
# 0.5.0: the flaw this fixes — only `setup` installed anything, so an update that added a
# linter (typescript-lsp in 0.4.6) silently left it uninstalled until the user noticed.

SYNC_LOCK_TTL = 1800           # a worker dead > 30 min no longer holds sync.lock
SYNC_RETRY_AFTER = 24 * 3600   # a failed install is retried at most once per day
SYNC_STATE_FILE = "sync-state.json"
SYNC_LOCK = "sync.lock"


def _system_message(text):
    """One line of SessionStart hook JSON; ensure_ascii keeps 🩻 and — human-readable."""
    print(json.dumps({"systemMessage": text}, ensure_ascii=False), flush=True)


def _load_sync_state(home):
    return _load_json(home / SYNC_STATE_FILE, {"results": {}, "manager_notice": False})


def _save_sync_state(home, state):
    home.mkdir(parents=True, exist_ok=True)
    (home / SYNC_STATE_FILE).write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")


def _release_lock(lock):
    try:
        lock.unlink()
    except OSError:
        pass


def _sync_main(home, tier, clock, spawn, find_bin):
    """Fast session-start check. clock/spawn/find_bin injectable so tests never spawn,
    probe, or touch the real PATH. Hook fail-open: any error -> exit 0, no output."""
    try:
        return _sync_main_run(home, tier, clock, spawn, find_bin)
    except Exception:
        return 0


def _sync_main_run(home, tier, clock, spawn, find_bin):
    if tier == "none":
        return 0
    state = _load_sync_state(home)
    pending = [(lid, res) for lid, res in sorted(state.get("results", {}).items())
               if not res.get("reported")]
    if pending:
        ok_ids = [lid for lid, res in pending if res.get("status") == "installed"]
        parts = (["installed: " + ", ".join(ok_ids)] if ok_ids else []) + \
                ["could not install %s: %s — run: %s"
                 % (lid, res.get("reason") or "?", res.get("cmd") or "?")
                 for lid, res in pending if res.get("status") != "installed"]
        _system_message("🩻 check-code: " + "; ".join(parts))
        for _lid, res in pending:
            res["reported"] = True
        _save_sync_state(home, state)
    missing = [lin for lin in BUILTIN
               if tier_covers(tier, lin["tier"]) and not find_bin(lin["binary"])]
    if not missing:
        return 0
    spawnable = [lin for lin in missing if find_bin(lin["install"]["manager"])]
    manual = [lin for lin in missing if not find_bin(lin["install"]["manager"])]
    if manual and not state.get("manager_notice"):
        _system_message("🩻 check-code: no %s here — run yourself: %s" % (
            " or ".join(sorted({lin["install"]["manager"] for lin in manual})),
            "; ".join(install_hint(lin) for lin in manual)))
        state["manager_notice"] = True
        _save_sync_state(home, state)
    if not spawnable:
        return 0
    lock = home / SYNC_LOCK
    if lock.is_file():
        if clock() - _load_json(lock, {}).get("at", 0) < SYNC_LOCK_TTL:
            return 0                     # another session's worker is already installing
        _release_lock(lock)              # stale: a worker died > 30 min ago
    home.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return 0                         # lost the race — exactly one worker, ever
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"at": clock(), "pid": os.getpid()}))
    spawn([sys.executable, str(Path(__file__).resolve()), "sync", "--worker"])
    _system_message("🩻 check-code: installing %s for your %s tier in the background"
                    % (", ".join(lin["id"] for lin in spawnable), tier))
    return 0


def _sync_worker(home, tier, clock, run_install, ready):
    """The detached installer. Re-checks the WHOLE tier with readiness (probe included, so
    a present-but-broken linter counts as not installed), installs what is missing,
    records installed/failed per id for the next sync to report, releases the lock.
    Never prints to the hook; never raises out."""
    try:
        _sync_worker_run(home, tier, clock, run_install, ready)
        return 0
    except Exception:
        return 0
    finally:
        _release_lock(home / SYNC_LOCK)


def _sync_worker_run(home, tier, clock, run_install, ready):
    if tier == "none":
        return
    state = _load_sync_state(home)
    now = clock()
    for lin in [dict(item) for item in BUILTIN]:
        if not tier_covers(tier, lin["tier"]):
            continue
        lid = lin["id"]
        prev = (state.get("results") or {}).get(lid) or {}
        if prev.get("status") == "failed" and now - prev.get("at", 0) < SYNC_RETRY_AFTER:
            continue                     # a broken installer must not loop every session
        if ready(lin):
            continue                     # probe says it works — nothing to do
        ok, reason = run_install(lin["install"]["cmd"])
        state.setdefault("results", {})[lid] = {
            "status": "installed" if ready(lin) else "failed",
            "reason": reason if (ok and ready(lin)) else (reason or "install did not make "
                                                           "the linter ready (probe failed)"),
            "cmd": install_hint(lin), "at": now, "reported": False}
        _save_sync_state(home, state)


def _spawn(argv):
    """Detached worker: own session, stdin devnull, output to sync.log — the hook that
    spawned us returns in < 1 s no matter how long the install takes."""
    home = config_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "sync.log", "ab") as log:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)


def cmd_sync(args):
    tier = recorded_tier() or DEFAULT_TIER   # absent config = the opt-out default
    if getattr(args, "worker", False):
        return _sync_worker(config_home(), tier, time.time, _run_install, linter_ready)
    return _sync_main(config_home(), tier, time.time, _spawn, find_binary)


# ---------------------------------------------------------------- CLI

def cmd_check(args):
    # Validate on the path AS GIVEN, before resolve(): resolve() maps a dangling symlink to
    # its nonexistent target, and the error must name what the user typed (review finding 3).
    given = Path(args.file)
    if not given.exists():
        # The hook treats a missing file as no-findings (fail-open, a file may be deleted
        # mid-session); an EXPLICIT CLI ask must say so loudly instead of crashing with a
        # raw FileNotFoundError whose unhandled-exception exit 1 reads as "findings found".
        print("check-code: no such file: %s" % given, file=sys.stderr)
        return 2
    if not given.is_file():
        print("check-code: not a regular file: %s" % given, file=sys.stderr)
        return 2
    path = given.resolve()
    # ONE report, ONE order: builtin and linter findings are collected into a single list
    # and printed sorted together — the two separate loops never interleaved, so a builtin
    # on line 9 printed before a linter finding on line 3 no matter what (review finding 1).
    findings = list(builtin_findings(
        path, path.read_text(encoding="utf-8", errors="replace"), "check", None))
    linters = linters_for(path, load_linters())
    if not linters and not findings:
        print("no linter configured for %s" % path.name)
        return 0
    for linter in linters:
        if not find_binary(linter["binary"]):
            print("%s: not installed (%s)" % (linter["id"], install_hint(linter)))
            continue
        findings.extend(run_linter(linter, path))
    status = 0
    for line, code, message in sorted(findings):
        print("%s:%d %s %s" % (path.name, line, code, message))
        status = 1
    return status


def cmd_doctor(_args):
    linters = load_linters()
    recorded = recorded_tier()
    raw = _load_json(config_home() / CONFIG_FILE, {}).get("tier")
    if recorded is None:
        print("tier: setup never run" + (" (config.json holds %r, which is not a tier)" % raw
                                         if raw is not None else ""))
    else:
        print("tier: %s" % recorded)
    missing = 0
    for linter in linters:
        found = find_binary(linter["binary"])
        if found:
            missing += 0
            print("%-11s %-8s %s" % (linter["id"], linter["language"], found))
            continue
        missing += 1
        if recorded is not None and linter["tier"] != "local" \
                and not tier_covers(recorded, linter["tier"]):
            note = "not in your tier (%s)" % recorded       # excluded on purpose: not MISSING
        else:
            note = "MISSING (%s)" % install_hint(linter)
        print("%-11s %-8s %s" % (linter["id"], linter["language"], note))
    local = config_home() / "linters.local.json"
    print("local config: %s" % (local if local.exists() else "none (%s)" % local))
    print("built-in checks (always on): STDERR-PIPE (python), NO-HELP (new script files), "
          "HARDCODED-PATH (committed paths), SKILL-BUDGET (SKILL.md size), MENU-NAV (menu grammar)")
    if missing == len(linters):
        print("check-code is reduced to its built-in checks: no external linter installed.")
    return 1 if missing == len(linters) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check-code.py",
        description="Lint the lines Claude just edited and feed the findings back (PostToolUse).",
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("hook", help="PostToolUse entry point (reads hook JSON on stdin)")
    p = sub.add_parser("check", help="lint a whole FILE the way the hook would "
                                     "(a missing file: exit 2, unlike the hook)")
    p.add_argument("file")
    sub.add_parser("doctor", help="show which linters are installed")
    s = sub.add_parser("setup", help="install a tier's linters and record the choice",
                       description="Install the linters of a coverage tier and record it in "
                                   "$CHECK_CODE_HOME/config.json. Never uses sudo.",
                       epilog=SETUP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--tier", choices=TIERS, help="basic | polyglot | none (default: polyglot)")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--dry-run", action="store_true",
                   help="print the plan and change nothing")
    y = sub.add_parser("sync", help="SessionStart entry: install missing tier linters in the "
                                    "background (never blocks)")
    y.add_argument("--worker", action="store_true",
                   help="internal: the detached installer sync spawns — re-checks the whole "
                        "tier with probes and records results")
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2
    if args.cmd == "hook":
        return run_hook(sys.stdin)
    return {"check": cmd_check, "doctor": cmd_doctor, "setup": cmd_setup,
            "sync": cmd_sync}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
