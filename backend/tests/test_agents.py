import json
import sys
import time
from pathlib import Path

import pytest

from project_os import agents
from project_os.agents import AgentError

# Trimmed from real `claude -p --output-format json` and `agy -p --output-format json` runs.
CLAUDE_OK = {
    "type": "result", "subtype": "success", "is_error": False, "result": "Probe is a tiny test project.",
    "session_id": "a0f12d5f", "num_turns": 2, "duration_ms": 10015, "total_cost_usd": 0.147,
    "usage": {"input_tokens": 4, "cache_creation_input_tokens": 17034, "cache_read_input_tokens": 33409,
              "output_tokens": 224},
}
AGY_OK = {
    "conversation_id": "7cc94f1a", "status": "SUCCESS", "response": "Probe is a tiny test project.\n",
    "duration_seconds": 14.47, "num_turns": 1,
    "usage": {"input_tokens": 56297, "output_tokens": 438, "thinking_tokens": 0, "cache_read_tokens": 0},
}
AGY_TIMED_OUT = {**AGY_OK, "response": "", "duration_seconds": 0}


def fake_cli(tmp_path: Path, stdout: str = "", stderr: str = "", exit_code: int = 0, sleep_s: float = 0) -> Path:
    """An executable that records its argv and stdin, then answers like the real CLI."""
    script = tmp_path / "fake-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"log = {str(tmp_path / 'call.json')!r}\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}, open(log, 'w'))\n"
        f"time.sleep({sleep_s})\n"
        f"sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def last_call(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "call.json").read_text())


def test_claude_success_is_normalized(tmp_path):
    exe = fake_cli(tmp_path, json.dumps(CLAUDE_OK))

    r = agents.run("claude", "--summarize the README", tmp_path, executable=str(exe))

    assert r.ok and r.status == "success"
    assert r.text == "Probe is a tiny test project."
    assert r.session_id == "a0f12d5f"
    assert r.duration_s == pytest.approx(10.015)
    assert r.cost_usd == 0.147
    assert r.tokens == {"input": 4 + 17034 + 33409, "output": 224}
    assert r.exit_code == 0
    call = last_call(tmp_path)
    # The prompt goes through stdin, so a leading "-" is not read as a flag.
    assert call["stdin"] == "--summarize the README"
    assert call["argv"] == ["-p", "--output-format", "json", "--permission-mode", "plan"]


def test_agy_success_is_normalized(tmp_path):
    exe = fake_cli(tmp_path, json.dumps(AGY_OK))

    r = agents.run("agy", "Summarize", tmp_path, mode="edit", timeout_s=120, model="m1", executable=str(exe))

    assert r.ok
    assert r.text == "Probe is a tiny test project.\n"
    assert r.session_id == "7cc94f1a"
    assert r.cost_usd is None
    assert r.tokens == {"input": 56297, "output": 438}
    assert last_call(tmp_path)["argv"] == [
        "-p", "Summarize", "--print-timeout", "105s",
        "--output-format", "json", "--mode", "accept-edits", "--model", "m1",
    ]


def test_agy_print_timeout_is_not_a_success(tmp_path):
    # agy says SUCCESS and exits 0 even when it gave up on the turn.
    exe = fake_cli(tmp_path, json.dumps(AGY_TIMED_OUT),
                   stderr="[agy] print timeout after 2s with turn in progress; returning partial output\n")

    r = agents.run("agy", "Write an essay", tmp_path, executable=str(exe))

    assert r.status == "timeout"
    assert not r.ok
    assert r.exit_code == 0


def test_claude_error_result(tmp_path):
    exe = fake_cli(tmp_path, json.dumps({**CLAUDE_OK, "subtype": "error_max_turns", "is_error": True}), exit_code=1)

    r = agents.run("claude", "x", tmp_path, executable=str(exe))

    assert r.status == "error"
    assert r.exit_code == 1


def test_output_without_json_is_an_error_with_the_text_kept(tmp_path):
    exe = fake_cli(tmp_path, "Invalid API key · Please run /login\n", exit_code=1)

    r = agents.run("claude", "x", tmp_path, executable=str(exe))

    assert r.status == "error"
    assert r.raw is None
    assert "Invalid API key" in r.text


def test_nonzero_exit_overrides_a_success_payload(tmp_path):
    exe = fake_cli(tmp_path, json.dumps(AGY_OK), exit_code=3)

    assert agents.run("agy", "x", tmp_path, executable=str(exe)).status == "error"


def test_coordinator_timeout_kills_the_agent(tmp_path):
    exe = fake_cli(tmp_path, json.dumps(CLAUDE_OK), sleep_s=30)

    started = time.monotonic()
    r = agents.run("claude", "x", tmp_path, timeout_s=1, executable=str(exe))

    assert time.monotonic() - started < 10
    assert r.status == "timeout"
    assert r.exit_code is None


def test_bad_inputs_are_rejected_before_running(tmp_path):
    exe = str(fake_cli(tmp_path))
    with pytest.raises(AgentError, match="unknown agent"):
        agents.run("gemini", "x", tmp_path, executable=exe)
    with pytest.raises(AgentError, match="unknown mode"):
        agents.run("claude", "x", tmp_path, mode="yolo", executable=exe)
    with pytest.raises(AgentError, match="working directory"):
        agents.run("claude", "x", tmp_path / "missing", executable=exe)
    assert not (tmp_path / "call.json").exists()


def test_missing_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AgentError, match="not found on PATH"):
        agents.run("agy", "x", tmp_path)
