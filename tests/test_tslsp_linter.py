"""0.4.6: 'typescript-lsp' linter in the catalog: drives typescript-language-server over stdio,
parses publishDiagnostics JSON, and reports tsserver's semanticDiagnosticsSync as a fallback
(the brew package's bundled tsserver can fail to start -> every request answers noServer)."""
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_code", ROOT / "bin" / "check-code.py")
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
RUNNER = os.path.join(ROOT, "scripts", "tslsp-lint.py")


def test_tslsp_in_builtin_catalog_with_ts_exts():
    entry = next(e for e in cc.BUILTIN if e["id"] == "typescript-lsp")
    assert entry["binary"] == "typescript-language-server"
    assert set(entry["ext"]) >= {"ts", "tsx", "js", "jsx"}
    assert entry["tier"] == "polyglot"


def test_tslsp_parser_reads_real_fixture():
    out = cc._tslsp_parse(open(os.path.join(FIXTURES, "tslsp-diagnostics.json")).read())
    assert (1, "2322", "Type '\"s\"' is not assignable to type 'number'.") in out
    assert (2, "6133", "'y' is declared but its value is never read.") in out
    assert all(line >= 1 for line, _, _ in out)  # 0-based LSP lines become 1-based


def test_tslsp_parser_ignores_non_diagnostic_lines():
    noisy = '{"jsonrpc":"2.0","method":"window/logMessage","params":{}}\nnot json\n'
    assert cc._tslsp_parse(noisy) == []


def test_tslsp_parser_empty_is_no_findings():
    assert cc._tslsp_parse("") == []


@pytest.mark.skipif(shutil.which("typescript-language-server") is None,
                    reason="typescript-language-server not installed")
def test_runner_end_to_end_on_a_real_file(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text('const x: number = "s";\n')
    r = subprocess.run(["python3", RUNNER, str(f)], capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout.strip().splitlines()[-1])
    diags = msg["params"]["diagnostics"]
    assert any(d["code"] == 2322 and d["range"]["start"]["line"] == 0 for d in diags)


# ---- 0.5.0: languageId by extension, project root for tsconfig discovery ----------------
_rspec = importlib.util.spec_from_file_location("tslsp_lint", RUNNER)
assert _rspec is not None and _rspec.loader is not None
tslsp = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(tslsp)


@pytest.mark.parametrize("name,expected", [
    ("a.tsx", "typescriptreact"), ("a.jsx", "javascriptreact"),
    ("a.js", "javascript"), ("a.mjs", "javascript"), ("a.cjs", "javascript"),
    ("a.ts", "typescript"), ("a.mts", "typescript"), ("a.cts", "typescript"),
])
def test_language_id_follows_the_extension(name, expected):
    assert tslsp._language_id(name) == expected


def test_project_root_is_the_nearest_folder_with_a_ts_config(tmp_path):
    (tmp_path / "proj" / "hooks" / "deep").mkdir(parents=True)
    (tmp_path / "proj" / "tsconfig.json").write_text("{}")
    f = tmp_path / "proj" / "hooks" / "deep" / "x.tsx"
    f.write_text("")
    assert tslsp._project_root(str(f)) == str(tmp_path / "proj")


def test_project_root_falls_back_to_the_files_folder(tmp_path):
    f = tmp_path / "lonely.ts"
    f.write_text("")
    # tmp_path has no tsconfig/jsconfig/package.json and is not a git checkout
    assert tslsp._project_root(str(f)) == str(tmp_path)


@pytest.mark.skipif(shutil.which("typescript-language-server") is None,
                    reason="typescript-language-server not installed")
def test_tsx_in_a_project_reports_only_the_planted_error():
    f = os.path.join(FIXTURES, "tsx_project", "src", "view.tsx")
    r = subprocess.run(["python3", RUNNER, f], capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    codes = [str(d.get("code")) for line in r.stdout.splitlines() if line.strip()
             for d in json.loads(line)["params"]["diagnostics"]]
    assert codes == ["2322"], codes
