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
    # the HARDCODED-PATH check only speaks inside a git repo: give every test one
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)


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


# ---------------------------------------------------------------- built-in checks: stderr-pipe

def write_payload(path, session="w"):
    return {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": str(path)}}


def test_popen_stderr_pipe_unread_is_flagged(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "proc.wait()\n")
    _, out = hook(edit_payload(f, "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)"))
    assert "STDERR-PIPE" in out and "L3" in out and "communicate()" in out


def test_unassigned_popen_stderr_pipe_is_flagged(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "subprocess.Popen(cmd, stderr=subprocess.PIPE)\n")
    _, out = hook(edit_payload(f, "subprocess.Popen(cmd, stderr=subprocess.PIPE)"))
    assert "STDERR-PIPE" in out and "L3" in out


def test_check_call_stderr_pipe_is_flagged(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "subprocess.check_call(cmd, stderr=subprocess.PIPE)\n")
    _, out = hook(edit_payload(f, "subprocess.check_call(cmd, stderr=subprocess.PIPE)"))
    assert "STDERR-PIPE" in out and "L3" in out


def test_popen_stderr_pipe_without_any_linter_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "find_binary", lambda name: None)
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "proc.wait()\n")
    _, out = hook(edit_payload(f, "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)"))
    assert "STDERR-PIPE" in out and "not installed" not in out


def test_popen_stderr_pipe_on_untouched_line_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "proc.wait()\n"
        "a0 = 0\n"
        "a1 = 0\n"
        "y = 2\n")
    _, out = hook(edit_payload(f, "y = 2"))
    assert "STDERR-PIPE" not in out


def test_popen_stderr_pipe_drained_via_communicate_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "proc.communicate()\n")
    _, out = hook(write_payload(f))
    assert "STDERR-PIPE" not in out


def test_popen_stderr_pipe_read_directly_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "proc.stderr.read()\n")
    _, out = hook(write_payload(f))
    assert "STDERR-PIPE" not in out


def test_run_stderr_pipe_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "subprocess.run(cmd, stderr=subprocess.PIPE)\n")
    _, out = hook(write_payload(f))
    assert "STDERR-PIPE" not in out


def test_popen_devnull_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)\n"
        "proc.wait()\n")
    _, out = hook(write_payload(f))
    assert "STDERR-PIPE" not in out


def test_popen_handed_to_a_reader_stays_quiet(tmp_path):
    f = tmp_path / "probe.py"
    f.write_text(
        "import subprocess\n"
        "cmd = ['echo', 'hi']\n"
        "proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)\n"
        "def drain(p):\n"
        "    p.stderr.read()\n"
        "drain(proc)\n")
    _, out = hook(write_payload(f))
    assert "STDERR-PIPE" not in out


def test_broken_python_file_does_not_crash_the_builtin(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n")
    code, out = hook(write_payload(f))
    assert code == 0 and "STDERR-PIPE" not in out


# ---------------------------------------------------------------- built-in checks: no --help

def test_write_new_shell_script_without_help_is_flagged(tmp_path):
    f = tmp_path / "do-thing.sh"
    f.write_text("#!/bin/bash\necho doing\n")
    _, out = hook(write_payload(f, session="h1"))
    assert "NO-HELP" in out and "RUNNING" in out


def test_write_new_zsh_script_without_help_is_flagged(tmp_path):
    # zsh matches no external linter at all -- the built-ins must still run
    f = tmp_path / "prompt"
    f.write_text("#!/bin/zsh\necho hi\n")
    _, out = hook(write_payload(f, session="h2"))
    assert "NO-HELP" in out


def test_write_main_guard_py_script_without_parsing_is_flagged(tmp_path):
    f = tmp_path / "runner.py"
    f.write_text("if __name__ == \"__main__\":\n    print(\"go\")\n")
    _, out = hook(write_payload(f, session="h3"))
    assert "NO-HELP" in out


def test_write_py_script_with_argparse_stays_quiet(tmp_path):
    f = tmp_path / "cli.py"
    f.write_text("#!/usr/bin/env python3\nimport argparse\n"
                 "parser = argparse.ArgumentParser()\nparser.parse_args()\n")
    _, out = hook(write_payload(f, session="h4"))
    assert "NO-HELP" not in out


def test_write_shell_script_with_getopts_stays_quiet(tmp_path):
    f = tmp_path / "do.sh"
    f.write_text("#!/bin/bash\nwhile getopts \"v\" o; do :; done\n")
    _, out = hook(write_payload(f, session="h5"))
    assert "NO-HELP" not in out


def test_write_py_module_stays_quiet(tmp_path):
    # no shebang and no __main__ guard: a library module, not a script
    f = tmp_path / "lib.py"
    f.write_text("def add(a, b):\n    return a + b\n")
    _, out = hook(write_payload(f, session="h6"))
    assert "NO-HELP" not in out


def test_write_pytest_file_under_tests_dir_stays_quiet(tmp_path):
    # pytest collects it; nobody runs it by hand, so --help would be meaningless
    d = tmp_path / "tests"
    d.mkdir()
    f = d / "test_thing.py"
    f.write_text("#!/usr/bin/env python3\ndef test_x():\n    pass\n\n"
                 "if __name__ == \"__main__\":\n    test_x()\n")
    _, out = hook(write_payload(f, session="h6a"))
    assert "NO-HELP" not in out


def test_write_test_named_py_outside_tests_dir_stays_quiet(tmp_path):
    f = tmp_path / "test_thing.py"
    f.write_text("#!/usr/bin/env python3\nif __name__ == \"__main__\":\n    pass\n")
    _, out = hook(write_payload(f, session="h6b"))
    assert "NO-HELP" not in out


def test_write_sourced_library_header_stays_quiet(tmp_path):
    f = tmp_path / "lib.sh"
    f.write_text("#!/bin/bash\n# Shared helpers -- sourced by launch.sh, not executable on its own.\n"
                 "helper() { echo hi; }\n")
    _, out = hook(write_payload(f, session="h6c"))
    assert "NO-HELP" not in out


def test_sourced_mention_deep_in_body_does_not_exempt(tmp_path):
    # the exemption reads the HEADER only: a script that merely mentions 'sourced by'
    # far down is still a runnable script and must still be flagged
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/bash\n" + "echo step\n" * 30 + "# note: sourced by nobody\n")
    _, out = hook(write_payload(f, session="h6d"))
    assert "NO-HELP" in out


def test_tests_in_name_but_not_a_dir_component_still_flagged(tmp_path):
    f = tmp_path / "mytests-runner.sh"
    f.write_text("#!/bin/bash\necho go\n")
    _, out = hook(write_payload(f, session="h6e"))
    assert "NO-HELP" in out


def test_write_markdown_stays_quiet(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# notes\n")
    assert hook(write_payload(f, session="h7")) == (0, "")


def test_no_help_not_repeated_within_a_session(tmp_path):
    f = tmp_path / "do-thing.sh"
    f.write_text("#!/bin/bash\necho doing\n")
    _, first = hook(write_payload(f, session="h8"))
    _, second = hook(write_payload(f, session="h8"))
    assert "NO-HELP" in first and "NO-HELP" not in second


def test_edit_of_existing_script_gets_no_help_check(tmp_path):
    f = tmp_path / "do-thing.sh"
    f.write_text("#!/bin/bash\necho doing\n")
    _, out = hook(edit_payload(f, "echo doing"))
    assert "NO-HELP" not in out


def test_doctor_lists_builtin_checks():
    r = cli("doctor")
    assert "builtin" in r.stdout


# ---------------------------------------------------------------- built-in checks: hardcoded paths

def test_hardcoded_cache_version_path_is_flagged(tmp_path):
    f = tmp_path / "tool.sh"
    f.write_text("#!/bin/bash\nls ~/.claude/plugins/cache/haiggoh/free-agents/0.44.1/bin\n")
    _, out = hook(edit_payload(f, 'ls ~/.claude/plugins/cache/haiggoh/free-agents/0.44.1/bin'))
    assert "HARDCODED-PATH" in out and "L2" in out and "portability" in out.lower()


def test_hardcoded_workspace_checkout_path_is_flagged(tmp_path):
    f = tmp_path / "tool.sh"
    f.write_text("#!/bin/bash\ncd ~/ClaudeWorkspace/free-agents && git status\n")
    _, out = hook(edit_payload(f, 'cd ~/ClaudeWorkspace/free-agents && git status'))
    assert "HARDCODED-PATH" in out


def test_hardcoded_home_path_is_flagged(tmp_path):
    f = tmp_path / "tool.py"
    f.write_text('x = 1\np = "/Users/bra0002h/.claude/settings.json"\n')
    _, out = hook(edit_payload(f, 'p = "/Users/bra0002h/.claude/settings.json"'))
    assert "HARDCODED-PATH" in out


def test_users_shared_stays_quiet(tmp_path):
    f = tmp_path / "tool.sh"
    f.write_text("#!/bin/bash\nls /Users/Shared/fonts\n")
    assert hook(edit_payload(f, 'ls /Users/Shared/fonts'))[1] == ""


def test_changelog_lines_stay_quiet(tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text("# Changelog\nshipped from ~/.claude/plugins/cache/haiggoh/free-agents/0.44.1 today\n")
    assert hook(edit_payload(f, "shipped from ~/.claude/plugins/cache/haiggoh/free-agents/0.44.1 today"))[1] == ""


def test_tmp_built_fixture_lines_stay_quiet(tmp_path):
    f = tmp_path / "test_fix.py"
    line = '# fixture: p = tmp_path / "plugins/cache/own/name/1.2.3"  ($TMP-built)'
    f.write_text("x = 1\n" + line + "\n")
    assert hook(edit_payload(f, line))[1] == ""


def test_hardcoded_path_on_untouched_line_stays_quiet(tmp_path):
    f = tmp_path / "tool.sh"
    f.write_text("#!/bin/bash\nls ~/.claude/plugins/cache/a/b/1.0.0/x\necho hi\necho ho\necho he\ny = 3\n")
    _, out = hook(edit_payload(f, "y = 3"))
    assert "HARDCODED-PATH" not in out


def test_hardcoded_path_scope_is_git_repos_only(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")  # a sibling dir: no .git above it
    outside.mkdir(exist_ok=True)
    f = outside / "notes.md"
    line = "see /Users/bra0002h/.claude/x"
    f.write_text("x\n" + line + "\n")
    assert hook(edit_payload(f, line))[1] == ""  # not a git repo: out of scope
    subprocess.run(["git", "init", "-q", str(outside)], check=True, capture_output=True)
    _, out = hook(edit_payload(f, line))
    assert "HARDCODED-PATH" in out  # the same line, now inside a repo


# ---------------------------------------------------------------- built-in checks: skill budget

def skill_doc(body_words, front_words=0):
    front = "---\nname: x\ndescription: d %s\n---\n" % ("fw" * front_words)
    return front + "\n" + " ".join(["word"] * body_words) + "\n"


def test_oversized_skill_md_is_flagged_on_write(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(1600))
    _, out = hook(write_payload(f, session="sb1"))
    assert "SKILL-BUDGET" in out and "references/" in out


def test_undersized_skill_md_stays_quiet(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(800))
    assert hook(write_payload(f, session="sb2"))[1] == ""


def test_skill_budget_counts_body_not_frontmatter(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(100, front_words=1400))
    assert hook(write_payload(f, session="sb3"))[1] == ""


def test_non_skill_markdown_stays_quiet(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text(" ".join(["word"] * 1600) + "\n")
    assert hook(write_payload(f, session="sb4"))[1] == ""


def test_skill_budget_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_CODE_SKILL_BUDGET_WORDS", "10")
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(12))
    _, out = hook(write_payload(f, session="sb5"))
    assert "SKILL-BUDGET" in out


def test_skill_budget_fires_once_per_session(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(1600))
    _, first = hook(write_payload(f, session="sb6"))
    _, second = hook(write_payload(f, session="sb6"))
    assert "SKILL-BUDGET" in first and "SKILL-BUDGET" not in second


def test_skill_budget_on_far_edit_still_fires(tmp_path):
    # a whole-file property must not be silenced by the touched-lines filter
    f = tmp_path / "SKILL.md"
    f.write_text(skill_doc(1600) + "\n" + "\n".join("pad line %d" % i for i in range(400)))
    _, out = hook(edit_payload(f, "pad line 399 touched"))
    assert "SKILL-BUDGET" in out


# ---------------------------------------------------------------- built-in checks: menu nav

def test_numbered_quit_is_flagged(tmp_path):
    f = tmp_path / "menu.sh"
    f.write_text('#!/bin/bash\necho "5) Exit"\n')
    _, out = hook(edit_payload(f, 'echo "5) Exit"'))
    assert "MENU-NAV" in out and "numbers = actions" in out


def test_numbered_back_is_flagged(tmp_path):
    f = tmp_path / "menu.py"
    f.write_text('x = 1\nprint("2) Back")\n')
    _, out = hook(edit_payload(f, 'print("2) Back")'))
    assert "MENU-NAV" in out


def test_numbered_cancel_is_flagged(tmp_path):
    f = tmp_path / "menu.py"
    f.write_text('x = 1\n_box row "3) Cancel"\n')
    _, out = hook(edit_payload(f, '_box row "3) Cancel"'))
    assert "MENU-NAV" in out


def test_numbered_action_stays_quiet(tmp_path):
    f = tmp_path / "menu.sh"
    f.write_text('#!/bin/bash\necho "1) Install everything"\n')
    assert hook(edit_payload(f, 'echo "1) Install everything"'))[1] == ""


def test_lowercase_label_stays_quiet(tmp_path):
    # the plan's grammar pattern targets capitalised label rows; keep scope
    f = tmp_path / "menu.sh"
    f.write_text('#!/bin/bash\necho "5) exit"\n')
    assert hook(edit_payload(f, 'echo "5) exit"'))[1] == ""


def test_letter_navigation_stays_quiet(tmp_path):
    f = tmp_path / "menu.sh"
    f.write_text('#!/bin/bash\necho "q) Quit  ·  b Back"\n')
    assert hook(edit_payload(f, 'echo "q) Quit  ·  b Back"'))[1] == ""


# ---------------------------------------------------------------- mutation: each check is what fires

def test_mutation_new_checks_are_what_fire(tmp_path, monkeypatch):
    f = tmp_path / "tool.sh"
    f.write_text('#!/bin/bash\necho "5) Exit"\nls ~/.claude/plugins/cache/a/b/1.0.0/x\n')
    monkeypatch.setattr(cc, "_menu_nav_lines", lambda content: [])
    assert "MENU-NAV" not in hook(edit_payload(f, 'echo "5) Exit"'))[1]
    assert "HARDCODED-PATH" in hook(edit_payload(f, 'ls ~/.claude/plugins/cache/a/b/1.0.0/x'))[1]
    monkeypatch.setattr(cc, "_hardcoded_path_lines", lambda content: [])
    assert "HARDCODED-PATH" not in hook(edit_payload(f, 'ls ~/.claude/plugins/cache/a/b/1.0.0/x'))[1]
    big = tmp_path / "SKILL.md"
    big.write_text(skill_doc(1600))
    monkeypatch.setattr(cc, "_skill_body_after_frontmatter", lambda content: "tiny")
    assert "SKILL-BUDGET" not in hook(write_payload(big, session="mut1"))[1]


# ---------------------------------------------------------------- branding: the mark leads the findings report

def test_findings_header_leads_with_the_branding_mark(tmp_path):
    f = tmp_path / "do-thing.sh"
    f.write_text("#!/bin/bash\necho doing\n")
    _, out = hook(write_payload(f, session="brand1"))
    assert out.splitlines()[0].startswith("🩻 check-code: ")
    assert "issue(s) in" in out and "NO-HELP" in out


def test_findings_header_exact_string(tmp_path):
    # Exact-string pin on the formatter seam: synthetic findings, so the header is
    # deterministic and independent of which linters this machine happens to have.
    out = cc._format_findings([(1, "NO-HELP", "msg")], None, tmp_path / "do-thing.sh")
    assert out.splitlines()[0] == (
        "🩻 check-code: 1 issue(s) in the file you just wrote, do-thing.sh (as of THIS edit -- "
        "a sibling edit in the same batch may already fix them) -- fix them now, or say why a "
        "finding is intentional:")
