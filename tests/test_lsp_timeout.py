"""0.4.7: LSP linters get a longer per-run timeout than plain linters, and the lsp-lint runner
still reports findings for a one-line file (measured 2026-11-19: 10s was too short — tsserver
project spin-up alone took longer, so the check crashed with TimeoutExpired)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_code", ROOT / "bin" / "check-code.py")
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

TS_SRC = 'const x: number = "s";\n'


def test_lsp_entry_gets_the_longer_timeout():
    linters = cc.load_linters()
    lsp = next(e for e in linters if e["id"] == "typescript-lsp")
    assert cc.run_linter.__defaults__ is None  # sanity
    # the decision is by id substring; pin it with a tiny monkeypatch of the subprocess call
    calls = {}
    import subprocess

    real_run = subprocess.run

    def fake_run(cmd, **kw):
        calls["timeout"] = kw.get("timeout")
        raise AssertionError("stop after the timeout is observed")

    subprocess.run = fake_run
    try:
        try:
            cc.run_linter(lsp, Path("/tmp/x.ts"))
        except AssertionError:
            pass
    finally:
        subprocess.run = real_run
    assert calls["timeout"] == cc.LSP_TIMEOUT
    assert calls["timeout"] > cc.LINT_TIMEOUT


def test_plain_linter_keeps_the_short_timeout():
    linters = cc.load_linters()
    linter = next(e for e in linters if e["id"] == "ruff")
    import subprocess
    calls = {}
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        calls["timeout"] = kw.get("timeout")
        raise AssertionError("stop")

    subprocess.run = fake_run
    try:
        try:
            cc.run_linter(linter, Path("/tmp/x.py"))
        except AssertionError:
            pass
    finally:
        subprocess.run = real_run
    assert calls["timeout"] == cc.LINT_TIMEOUT


def test_runner_end_to_end_one_line_file(tmp_path):
    import json
    import subprocess
    f = tmp_path / "a.ts"
    f.write_text(TS_SRC)
    r = subprocess.run(["python3", str(ROOT / "scripts" / "tslsp-lint.py"), str(f)],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout.strip().splitlines()[-1])
    diags = msg["params"]["diagnostics"]
    assert any(d["code"] == 2322 and d["range"]["start"]["line"] == 0 for d in diags)