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
import time
from pathlib import Path

VERSION = "0.3.3"  # also in .claude-plugin/plugin.json and CHANGELOG.md
MAX_FINDINGS = 10
MARGIN = 2              # lines of context around an edit that still count as touched
LINT_TIMEOUT = 10       # seconds per linter run
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

HELP_EPILOG = """\
built-in linters (each runs only if installed):
  python  ruff check          install: pipx install ruff
  shell   shellcheck          install: brew install shellcheck   (.sh/.bash/.ksh, or a sh/bash shebang)
  extra linters: add them to $CHECK_CODE_HOME/linters.local.json (see README)
built-in checks (always on, no linter needed):
  STDERR-PIPE  python Popen/call/check_call with stderr=PIPE that nothing in the file drains
  NO-HELP      a Write of a new script file with no argument parsing / --help handling
  HARDCODED-PATH  a literal /Users/<name>/ home, ClaudeWorkspace/<name> checkout, or
               plugin-cache VERSION path on a touched line — use ~/…, $HOME/… or an env
               override instead ($TMP-built test-fixture lines and CHANGELOGs are exempt)
  SKILL-BUDGET a SKILL.md whose body after frontmatter exceeds 1500 words
               (CHECK_CODE_SKILL_BUDGET_WORDS overrides; once per file per session)
  MENU-NAV     a menu row giving NAVIGATION a number ("5) Exit"): house grammar is
               numbers = actions, letters = navigation (q Quit, b Back)

environment:
  CHECK_CODE_OFF=1        disable the hook
  CHECK_CODE_MAX          max findings reported per edit (default 10)
  CHECK_CODE_HOME         config dir (default ~/.claude/check-code)
  CHECK_CODE_DEBUG=1      log hook exceptions to $CHECK_CODE_HOME/debug.log
"""


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


BUILTIN = [
    {"id": "ruff", "language": "Python", "binary": "ruff", "install": "pipx install ruff",
     "ext": ["py", "pyi"], "shebang": r"python[0-9.]*\b",
     "command": _ruff_command, "parse": _ruff_parse},
    {"id": "shellcheck", "language": "shell", "binary": "shellcheck",
     "install": "brew install shellcheck", "ext": ["sh", "bash", "ksh"],
     "shebang": r"\b(?:ba|k|da)?sh\b", "command": _shellcheck_command,
     "parse": _shellcheck_parse},
]


def load_linters():
    """Built-ins plus user linters from linters.local.json (same fields; command is a list
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
            "ext": [e.lstrip(".") for e in entry.get("ext", [])], "shebang": entry.get("shebang"),
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
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=LINT_TIMEOUT,
                          cwd=str(path.parent))
    return linter["parse"](proc.stdout)


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
    if "changelog" not in path.name.lower() and _in_git_repo(path):
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
    if not candidates and not findings:
        return ""
    installed = [lin for lin in candidates if find_binary(lin["binary"])]
    if not installed:
        if findings:  # the built-in checks need no installed linter
            return _format_findings(findings, ranges, path)
        lin = candidates[0]
        if _note_once(session_id, "missing:" + lin["id"]):
            return ("check-code: %s edits are not being checked -- %s is not installed (%s). "
                    "Tell the user once; installing is their call." % (lin["language"], lin["binary"],
                                                                        lin["install"]))
        return ""
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


# ---------------------------------------------------------------- CLI

def cmd_check(args):
    path = Path(args.file).resolve()
    status = 0
    for line, code, message in builtin_findings(
            path, path.read_text(encoding="utf-8", errors="replace"), "check", None):
        print("%s:%d %s %s" % (path.name, line, code, message))
        status = 1
    linters = linters_for(path, load_linters())
    if not linters and status == 0:
        print("no linter configured for %s" % path.name)
        return 0
    for linter in linters:
        if not find_binary(linter["binary"]):
            print("%s: not installed (%s)" % (linter["id"], linter["install"]))
            continue
        for line, code, message in sorted(run_linter(linter, path)):
            print("%s:%d %s %s" % (path.name, line, code, message))
            status = 1
    return status


def cmd_doctor(_args):
    linters = load_linters()
    missing = 0
    for linter in linters:
        found = find_binary(linter["binary"])
        missing += 0 if found else 1
        print("%-11s %-8s %s" % (linter["id"], linter["language"],
                                 found or "MISSING (%s)" % linter["install"]))
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
    p = sub.add_parser("check", help="lint a whole FILE the way the hook would")
    p.add_argument("file")
    sub.add_parser("doctor", help="show which linters are installed")
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2
    if args.cmd == "hook":
        return run_hook(sys.stdin)
    return {"check": cmd_check, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
