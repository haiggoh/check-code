"""Tests for check-code. Real ruff/shellcheck are used when installed; stubs otherwise."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "check-code.py"
spec = importlib.util.spec_from_file_location("check_code", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

needs_ruff = pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
needs_sc = pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_CODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.delenv("CHECK_CODE_OFF", raising=False)
    monkeypatch.delenv("CHECK_CODE_MAX", raising=False)


def edit_payload(path, new, tool="Edit", session="s", **extra):
    tool_input = {"file_path": str(path), "old_string": "x", "new_string": new}
    tool_input.update(extra)
    return {"session_id": session, "tool_name": tool, "tool_input": tool_input}


def hook(payload):
    out, old = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        code = cc.run_hook(io.StringIO(json.dumps(payload)))
    finally:
        sys.stdout = old
    text = out.getvalue()
    return code, (json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else "")


# ---------------------------------------------------------------- touched ranges

def test_ranges_edit_multiedit_write_and_replace_all():
    content = "a\nb\nNEW1\nNEW2\nc\nNEW1\nNEW2\n"
    assert cc.touched_ranges("Edit", {"new_string": "NEW1\nNEW2"}, content) == [(3, 4)]
    assert cc.touched_ranges("Edit", {"new_string": "NEW1\nNEW2", "replace_all": True},
                             content) == [(3, 4), (6, 7)]
    assert cc.touched_ranges("MultiEdit", {"edits": [{"new_string": "b"}, {"new_string": "c"}]},
                             content) == [(2, 2), (5, 5)]
    assert cc.touched_ranges("Write", {}, content) is None
    assert cc.touched_ranges("Edit", {"new_string": "moved away"}, content) is None
    assert cc.touched_ranges("Edit", {"new_string": ""}, content) == []


def test_in_ranges_margin():
    assert cc.in_ranges(12, [(10, 10)]) and not cc.in_ranges(13, [(10, 10)])
    assert cc.in_ranges(999, None)


# ---------------------------------------------------------------- dispatch

def test_linters_for_extension_and_shebang(tmp_path):
    linters = cc.load_linters()
    script = tmp_path / "deploy"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    zsh = tmp_path / "prompt"
    zsh.write_text("#!/bin/zsh\necho hi\n")
    py = tmp_path / "tool"
    py.write_text("#!/usr/bin/env python3\nprint(1)\n")
    ids = lambda p: [lin["id"] for lin in cc.linters_for(p, linters)]  # noqa: E731
    assert ids(tmp_path / "a.py") == ["ruff"]
    assert ids(tmp_path / "a.sh") == ["shellcheck"]
    assert ids(script) == ["shellcheck"]
    assert ids(zsh) == []            # shellcheck does not support zsh
    assert ids(py) == ["ruff"]
    assert ids(tmp_path / "a.md") == []


# ---------------------------------------------------------------- real linters

@needs_ruff
def test_ruff_reports_only_touched_lines(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("import os\n\n\n\n\n\n\ndef f(x=[]):\n    return x\n")  # F401 on 1, B006 on 8
    _, out = hook(edit_payload(f, "def f(x=[]):\n    return x"))
    assert "B006" in out and "F401" not in out and "L8" in out


@needs_ruff
def test_ruff_write_reports_whole_file(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("import os\n\n\n\n\n\n\ndef f(x=[]):\n    return x\n")
    _, out = hook({"session_id": "w", "tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert "F401" in out and "B006" in out and "file you just wrote" in out


@needs_ruff
def test_repo_ruff_config_wins(tmp_path):
    (tmp_path / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    f = tmp_path / "m.py"
    f.write_text("def f(x=[]):\n    return x\n")
    assert hook(edit_payload(f, "def f(x=[]):\n    return x"))[1] == ""  # B006 not selected


@needs_ruff
def test_clean_edit_is_silent(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("def f(x):\n    return x\n")
    assert hook(edit_payload(f, "def f(x):\n    return x")) == (0, "")


@needs_sc
def test_shellcheck_catches_unquoted_var_and_skips_noisy_defaults(tmp_path):
    f = tmp_path / "s.sh"
    f.write_text("#!/bin/bash\nunused=1\necho '$HOME'\nrm $1\n")
    _, out = hook(edit_payload(f, "unused=1\necho '$HOME'\nrm $1"))
    assert "SC2086" in out
    assert "SC2034" not in out and "SC2016" not in out


@needs_sc
def test_shellcheckrc_wins(tmp_path):
    (tmp_path / ".shellcheckrc").write_text("disable=SC2086\n")
    f = tmp_path / "s.sh"
    f.write_text("#!/bin/bash\nunused=1\nrm $1\n")
    _, out = hook(edit_payload(f, "unused=1\nrm $1"))
    assert "SC2034" in out and "SC2086" not in out


def test_cap_and_more_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_CODE_MAX", "2")
    f = tmp_path / "a.py"
    f.write_text("x\n")
    monkeypatch.setattr(cc, "find_binary", lambda name: "/bin/" + name)
    monkeypatch.setattr(cc, "run_linter", lambda lin, p: [(1, "X%d" % i, "m") for i in range(5)])
    out = hook({"tool_name": "Write", "tool_input": {"file_path": str(f)}})[1]
    assert out.count("\n  L1") == 2 and "+3 more" in out


# ---------------------------------------------------------------- missing linter / fail-open

def test_missing_linter_noted_once_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "find_binary", lambda name: None)
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    first = hook(edit_payload(f, "x = 1", session="m"))[1]
    assert "not installed" in first and "pipx install ruff" in first
    assert hook(edit_payload(f, "x = 1", session="m"))[1] == ""
    assert "not installed" in hook(edit_payload(f, "x = 1", session="other"))[1]


@pytest.mark.parametrize("payload", [
    "{garbage", json.dumps({}), json.dumps({"tool_input": {"file_path": "/nonexistent/x.py"}}),
])
def test_fail_open_on_bad_input(payload):
    out, old = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        assert cc.run_hook(io.StringIO(payload)) == 0
    finally:
        sys.stdout = old
    assert out.getvalue() == ""


def test_fail_open_when_linter_crashes(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(cc, "find_binary", lambda name: "/bin/" + name)
    monkeypatch.setattr(cc, "run_linter", lambda lin, p: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("ruff", 10)))
    assert hook(edit_payload(f, "x = 1")) == (0, "")


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_CODE_OFF", "1")
    f = tmp_path / "a.py"
    f.write_text("import os\n")
    assert hook(edit_payload(f, "import os")) == (0, "")


# ---------------------------------------------------------------- local linters

def test_local_linter_and_disable(tmp_path, monkeypatch):
    home = Path(os.environ["CHECK_CODE_HOME"])
    home.mkdir(parents=True)
    fake = tmp_path / "fakelint"
    fake.write_text('#!/bin/sh\necho "$1:2:5: warning: bad thing (R42)"\n')
    fake.chmod(0o755)
    (home / "linters.local.json").write_text(json.dumps({
        "disabled": ["ruff"],
        "linters": [{"id": "fake", "language": "Toy", "ext": ["toy"], "command": [str(fake), "{file}"]}]}))
    f = tmp_path / "a.toy"
    f.write_text("one\ntwo\n")
    ids = [lin["id"] for lin in cc.load_linters()]
    assert "ruff" not in ids and "fake" in ids
    out = hook(edit_payload(f, "two"))[1]
    assert "L2 R42 bad thing" in out


# ---------------------------------------------------------------- CLI (real subprocess)

def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=dict(os.environ))


def test_help_runs_without_side_effects():
    r = cli("--help")
    assert r.returncode == 0 and "CHECK_CODE_OFF" in r.stdout and "doctor" in r.stdout
    assert not Path(os.environ["CHECK_CODE_HOME"]).exists()


def test_unknown_flag_and_no_command_exit_2():
    assert cli("--bogus").returncode == 2
    assert cli().returncode == 2


def test_doctor_lists_builtins():
    r = cli("doctor")
    assert "ruff" in r.stdout and "shellcheck" in r.stdout


@needs_ruff
def test_hook_via_subprocess_emits_hook_json(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def f(x=[]):\n    return x\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "hook"], capture_output=True, text=True,
                       input=json.dumps(edit_payload(f, "def f(x=[]):\n    return x")),
                       env=dict(os.environ))
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
