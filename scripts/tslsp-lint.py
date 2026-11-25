#!/usr/bin/env python3
"""Drive typescript-language-server over stdio for ONE file; print one publishDiagnostics
JSON per line to stdout (parsed by check-code.py's _tslsp_parse).

Usage: tslsp-lint.py <file.ts> [--help]
Environment: typescript-language-server on PATH. Exit 0 even with findings (the findings are
the output); exit 2 when the server cannot be started or answers nothing (loud, not silent).
"""
import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from urllib.parse import quote


# LSP languageId per file extension: tsserver cannot tell a TSX generic from a
# JSX element otherwise (it parses `<Text dimColor>` as two comparisons).
_LANGUAGE_IDS = {
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascriptreact",
}
_LANGUAGE_ID_DEFAULT = "typescript"

# A project's config files, nearest first: tsserver reads `jsx`, `jsxFactory`,
# `lib` and `types` from them, so the root must be the project, not a scratch dir.
_PROJECT_MARKERS = ("tsconfig.json", "jsconfig.json", "package.json")


def _language_id(path):
    """The LSP languageId for a file, by extension (case-insensitive)."""
    ext = os.path.splitext(path)[1]
    return _LANGUAGE_IDS.get(ext.lower() if ext else "", _LANGUAGE_ID_DEFAULT)


def _file_uri(path):
    """file:// URI with percent-encoding — a path containing a space must not produce a
    broken URI (LSP servers decode it before opening the file)."""
    return "file://" + quote(os.path.abspath(path))


def _project_root(path):
    """Nearest ancestor directory holding a tsconfig/jsconfig/package.json;
    failing that, the git top level; failing that, the file's own directory."""
    path = os.path.abspath(path)
    nearest = path if os.path.isdir(path) else os.path.dirname(path)
    current = nearest
    while True:
        if any(os.path.isfile(os.path.join(current, m)) for m in _PROJECT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=nearest,
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return nearest
    if top.returncode == 0 and top.stdout.strip():
        return os.path.abspath(top.stdout.strip())
    return nearest


# Machine-wide fallback locations, tried only AFTER the private per-home lib. A complete
# lib = tsserver.js + typescript.js; a TS 7 package ships neither (its lib/ holds only
# tsc.js + getExePath.js), so it can never match.
_FALLBACK_LIB_DIRS = [
    "/opt/homebrew/lib/node_modules/intelephense/node_modules/typescript/lib",
    "/opt/homebrew/lib/node_modules/typescript/lib",
    "/opt/homebrew/opt/typescript/libexec/lib/node_modules/typescript/lib",
    "/usr/local/lib/node_modules/typescript/lib",
    "/usr/local/opt/typescript/libexec/lib/node_modules/typescript/lib",
]


def _tsserver_lib():
    """A directory containing a COMPLETE typescript lib (tsserver.js + typescript.js).

    The brew typescript-language-server package ships only cli.mjs; its sibling typescript
    package can be a stub (lib/ with just tsc.js), which makes tsserver never start (every
    tsserverRequest answers {"type":"noServer"}). TypeScript 7 is native and ships no
    tsserver.js at all, so check-code installs a PRIVATE typescript@6 under
    $CHECK_CODE_HOME/tsserver (the global typescript is never touched). Find a complete
    lib: env override first, then the private per-home lib, then the machine locations."""
    env_dir = os.environ.get("CHECK_CODE_TSSERVER_LIB")
    home = os.environ.get("CHECK_CODE_HOME", "~/.claude/check-code")
    candidates = [env_dir] if env_dir else []
    candidates.append(os.path.join(os.path.expanduser(home),
                                   "tsserver", "node_modules", "typescript", "lib"))
    candidates += _FALLBACK_LIB_DIRS
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "tsserver.js")) and os.path.isfile(os.path.join(d, "typescript.js")):
            return d
    return None


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("file")
    a = ap.parse_args()
    path = os.path.abspath(a.file)
    uri = _file_uri(path)
    root = _file_uri(_project_root(path))
    server = "typescript-language-server"
    lib = _tsserver_lib()
    if lib is None:
        # TS 7 ships no tsserver.js and a stub lib answers noServer on every request:
        # fail loud and name the fix instead of hanging until the timeout.
        home = os.path.expanduser(os.environ.get("CHECK_CODE_HOME", "~/.claude/check-code"))
        print("check-code: no TypeScript with tsserver.js found (TypeScript 7 ships none) — "
              "run: npm install --prefix %s typescript@6"
              % os.path.join(home, "tsserver"), file=sys.stderr)
        sys.exit(2)
    init_opts = {"tsserver": {"path": lib}}
    p = subprocess.Popen([server, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    # bind to non-Optional locals so the closures below type-check (Popen with PIPE
    # always yields streams, but the type says Optional)
    stdin, stdout = p.stdin, p.stdout
    if stdin is None or stdout is None:
        return 2

    def send(obj):
        body = json.dumps(obj).encode()
        stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        stdin.flush()

    def read_msg(timeout_s):
        sel = selectors.DefaultSelector()
        sel.register(stdout, selectors.EVENT_READ)
        if not sel.select(timeout_s):
            return None
        headers = {}
        while True:
            line = stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode().partition(":")
            headers[k.lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        return json.loads(stdout.read(length)) if length else None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "processId": None, "rootUri": root, "capabilities": {},
        "initializationOptions": init_opts}})
    if read_msg(15) is None:
        return 2
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": uri, "languageId": _language_id(path), "version": 1, "text": text}}})
    # publishDiagnostics is unreliable on some installs (noServer without a complete
    # typescript lib); semanticDiagnosticsSync via tsserverRequest is the reliable path.
    send({"jsonrpc": "2.0", "id": 2, "method": "workspace/executeCommand", "params": {
        "command": "typescript.tsserverRequest",
        "arguments": ["semanticDiagnosticsSync", {"file": path}]}})
    deadline = time.time() + 25
    saw = False
    while time.time() < deadline:
        m = read_msg(max(1.0, deadline - time.time()))
        if m is None:
            break
        if m.get("id") == 2 and m.get("result", {}).get("type") == "response" \
                and m["result"].get("success"):
            body = m["result"].get("body") or []
            notif = {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                     "params": {"uri": uri, "diagnostics": [
                         {"range": {"start": {"line": d["start"]["line"] - 1,
                                             "character": d["start"]["offset"] - 1},
                                    "end": {"line": d["end"]["line"] - 1,
                                            "character": d["end"]["offset"] - 1}},
                          "severity": {"error": 1, "warning": 2}.get(d.get("category"), 4),
                          "code": d.get("code"), "source": "ts", "message": d.get("text", "")}
                         for d in body]}}
            print(json.dumps(notif), flush=True)
            saw = True
            break
        if m.get("method") == "textDocument/publishDiagnostics":
            print(json.dumps(m), flush=True)
            saw = True
            break
    send({"jsonrpc": "2.0", "id": 99, "method": "exit"})
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    return 0 if saw else 2


if __name__ == "__main__":
    sys.exit(main())
