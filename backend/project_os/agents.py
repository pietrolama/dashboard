"""Run a dev agent CLI (Claude Code or Antigravity) once and normalize its result.

Both CLIs have a non-interactive print mode with a JSON result: `claude -p`
and `agy -p`. The adapter starts one of them in a working directory, waits
for the turn to finish (or for the coordinator's time limit), and returns an
AgentResult with the same shape whichever agent ran. The agents never talk to
each other: whoever calls run() passes one agent's output to the next.

`mode` is the only permission knob exposed: "read" runs the agent in plan
mode (it may read files but not change them), "edit" lets it edit files in
`cwd` without asking. Neither mode auto-approves shell commands.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

AGENTS = ("claude", "agy")
MODES = ("read", "edit")
DEFAULT_TIMEOUT_S = 600

_PERMISSION_FLAGS = {
    "claude": {"read": ["--permission-mode", "plan"], "edit": ["--permission-mode", "acceptEdits"]},
    "agy": {"read": ["--mode", "plan"], "edit": ["--mode", "accept-edits"]},
}
# agy gets its own print timeout a little before ours, so it can stop the turn
# itself and still print its JSON instead of being killed mid-write.
_AGY_GRACE_S = 15
# Written to stderr by agy; it still reports status SUCCESS and exits 0.
_AGY_TIMEOUT_MARKER = "print timeout"


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    agent: str
    status: str  # success | error | timeout
    text: str
    session_id: str | None
    duration_s: float
    num_turns: int | None
    cost_usd: float | None  # reported by claude only
    tokens: dict  # {"input": int | None, "output": int | None}
    exit_code: int | None  # None when the process was killed on timeout
    stderr: str
    cwd: str
    raw: dict | None = field(default=None, repr=False)
    # The reply as an object, when a JSON schema was passed (claude only: see build_command).
    structured: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_payload(self) -> dict:
        return asdict(self)


def build_command(
    agent: str, prompt: str, mode: str, timeout_s: int, executable: str, model: str | None = None,
    json_schema: dict | None = None,
) -> tuple[list[str], str]:
    """The argv to run and the text to write to its stdin."""
    if agent not in AGENTS:
        raise AgentError(f"unknown agent {agent!r}, expected one of {', '.join(AGENTS)}")
    if mode not in MODES:
        raise AgentError(f"unknown mode {mode!r}, expected one of {', '.join(MODES)}")
    if agent == "claude":
        # With no prompt argument claude reads it from stdin, so a prompt that
        # starts with "-" cannot be mistaken for a flag.
        cmd, stdin = [executable, "-p"], prompt
    else:
        # agy takes the prompt as the value of -p and does not read stdin.
        cmd, stdin = [executable, "-p", prompt, "--print-timeout", f"{max(timeout_s - _AGY_GRACE_S, 1)}s"], ""
    cmd += ["--output-format", "json", *_PERMISSION_FLAGS[agent][mode]]
    if model:
        cmd += ["--model", model]
    # agy accepts --json-schema too, but fills the object in a separate pass
    # that loses what the reply said; its JSON is asked for in the prompt instead.
    if json_schema is not None and agent == "claude":
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd, stdin


def run(
    agent: str,
    prompt: str,
    cwd: str | Path,
    mode: str = "read",
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str | None = None,
    executable: str | None = None,
    json_schema: dict | None = None,
) -> AgentResult:
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise AgentError(f"working directory not found: {cwd}")
    exe = executable or shutil.which(agent)
    if not exe:
        raise AgentError(f"{agent} executable not found on PATH")

    cmd, stdin = build_command(agent, prompt, mode, timeout_s, exe, model, json_schema)
    started = time.monotonic()
    # A new session makes the agent the leader of its own process group, so a
    # timeout also stops the tools and shells it started.
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout_s)
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        exit_code = None
    elapsed = time.monotonic() - started

    data = _parse_json(stdout)
    parse = _parse_claude if agent == "claude" else _parse_agy
    result = parse(data, stderr)
    result.update(agent=agent, exit_code=exit_code, stderr=stderr.strip()[-2000:], cwd=str(cwd), raw=data)
    result.setdefault("duration_s", round(elapsed, 3))

    if exit_code is None:
        result["status"] = "timeout"
    elif exit_code != 0 and result["status"] == "success":
        result["status"] = "error"
    elif data is None:
        result["status"] = "error"
        # Without JSON the only clue to what went wrong is what the CLI printed.
        result["text"] = result["text"] or stdout.strip()[-2000:]
    return AgentResult(**result)


def _parse_json(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _empty() -> dict:
    return {"status": "error", "text": "", "session_id": None, "num_turns": None,
            "cost_usd": None, "tokens": {"input": None, "output": None}}


def _parse_claude(data: dict | None, stderr: str) -> dict:
    if data is None:
        return _empty()
    usage = data.get("usage") or {}
    # Cached tokens are still tokens the model read; agy reports them inside
    # input_tokens, so they are summed here to keep the two comparable.
    input_parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    ok = data.get("subtype") == "success" and not data.get("is_error")
    result = {
        "status": "success" if ok else "error",
        "text": data.get("result") or "",
        "session_id": data.get("session_id"),
        "num_turns": data.get("num_turns"),
        "cost_usd": data.get("total_cost_usd"),
        "structured": data.get("structured_output") if isinstance(data.get("structured_output"), dict) else None,
        "tokens": {
            "input": sum(p for p in input_parts if p) if any(p is not None for p in input_parts) else None,
            "output": usage.get("output_tokens"),
        },
    }
    if data.get("duration_ms") is not None:
        result["duration_s"] = data["duration_ms"] / 1000
    return result


def _parse_agy(data: dict | None, stderr: str) -> dict:
    result = _empty()
    if data is not None:
        usage = data.get("usage") or {}
        result = {
            "status": "success" if data.get("status") == "SUCCESS" else "error",
            "text": data.get("response") or "",
            "session_id": data.get("conversation_id"),
            "num_turns": data.get("num_turns"),
            "cost_usd": None,
            "tokens": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")},
        }
        if data.get("duration_seconds"):
            result["duration_s"] = data["duration_seconds"]
    if _AGY_TIMEOUT_MARKER in stderr:
        result["status"] = "timeout"
    return result
