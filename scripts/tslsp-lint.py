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


def _tsserver_lib():
    """A directory containing a COMPLETE typescript lib (tsserver.js + typescript.js).

    The brew typescript-language-server package ships only cli.mjs; its sibling typescript
    package can be a stub (lib/ with just tsc.js), which makes tsserver never start (every
    tsserverRequest answers {"type":"noServer"}). Find a complete lib: env override first,
    then common locations (intelephense's bundled typescript, the global typescript)."""
    env_dir = os.environ.get("CHECK_CODE_TSSERVER_LIB")
    candidates = [env_dir] if env_dir else []
    candidates += [
        "/opt/homebrew/lib/node_modules/intelephense/node_modules/typescript/lib",
        "/opt/homebrew/lib/node_modules/typescript/lib",
        "/usr/local/lib/node_modules/typescript/lib",
    ]
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "tsserver.js")) and os.path.isfile(os.path.join(d, "typescript.js")):
            return d
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file")
    a = ap.parse_args()
    path = os.path.abspath(a.file)
    uri = "file://" + path
    root = "file://" + os.path.dirname(path)
    server = "typescript-language-server"
    lib = _tsserver_lib()
    init_opts = {"tsserver": {"path": lib}} if lib else {}
    p = subprocess.Popen([server, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    if p.stdin is None or p.stdout is None:
        return 2

    def send(obj):
        body = json.dumps(obj).encode()
        p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        p.stdin.flush()

    def read_msg(timeout_s):
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        if not sel.select(timeout_s):
            return None
        headers = {}
        while True:
            line = p.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            k, _, v = line.decode().partition(":")
            headers[k.lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        return json.loads(p.stdout.read(length)) if length else None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "processId": None, "rootUri": root, "capabilities": {},
        "initializationOptions": init_opts}})
    if read_msg(15) is None:
        return 2
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": uri, "languageId": "typescript", "version": 1, "text": text}}})
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
