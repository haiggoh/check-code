#!/usr/bin/env python3
"""check-code: lint the lines Claude just edited and hand the findings straight back.

A PostToolUse hook for Edit/Write/MultiEdit. It runs only linters that are already
installed, only on the edited file, and reports only findings on the lines that edit
touched, so existing debt elsewhere in the file stays quiet. It never blocks an edit.
Python 3.9 compatible (hooks may run under macOS /usr/bin/python3).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "0.1.0"  # also in .claude-plugin/plugin.json and CHANGELOG.md
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


def check_edit(payload):
    """Return the feedback text for one PostToolUse payload, or '' for nothing to say."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.is_file():
        return ""
    candidates = linters_for(path, load_linters())
    if not candidates:
        return ""
    installed = [lin for lin in candidates if find_binary(lin["binary"])]
    if not installed:
        lin = candidates[0]
        if _note_once(payload.get("session_id"), "missing:" + lin["id"]):
            return ("check-code: %s edits are not being checked -- %s is not installed (%s). "
                    "Tell the user once; installing is their call." % (lin["language"], lin["binary"],
                                                                        lin["install"]))
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    ranges = touched_ranges(payload.get("tool_name", ""), tool_input, content)
    findings = []
    for linter in installed:
        for line, code, message in run_linter(linter, path):
            if in_ranges(line, ranges):
                findings.append((line, code, message))
    if not findings:
        return ""
    findings = sorted(set(findings))
    cap = _cap()
    where = "the lines you just edited in" if ranges is not None else "the file you just wrote,"
    lines = ["check-code: %d issue(s) in %s %s -- fix them now, or say why a finding is "
             "intentional:" % (len(findings), where, path.name)]
    lines += ["  L%d %s %s" % f for f in findings[:cap]]
    if len(findings) > cap:
        lines.append("  +%d more (run the linter on the file to see all)" % (len(findings) - cap))
    return "\n".join(lines)


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
    linters = linters_for(path, load_linters())
    if not linters:
        print("no linter configured for %s" % path.name)
        return 0
    status = 0
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
    if missing == len(linters):
        print("check-code is INERT: no linter installed.")
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
