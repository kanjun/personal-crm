"""GPT-only subprocess adapter. No model-provider fallback is permitted."""
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel

from people_archive.cleanup_models import ProviderUnavailable
from people_archive.codex_pairing import private_codex_command

Result = TypeVar("Result", bound=BaseModel)
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
SOL_MODEL = "gpt-5.6-sol"
TERRA_MODEL = "gpt-5.6-terra"
GPT_TIMEOUT_SECONDS = 600
GPT_ATTEMPTS = 2


class InvalidGptCommandOptionsError(ProviderUnavailable, ValueError):
    """The GPT subprocess adapter received an unsupported command shape."""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop the command and every descendant that inherited its process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def run_command_in_process_group(
    command: list[str],
    *,
    input: str,
    text: bool,
    capture_output: bool,
    cwd: Path,
    timeout: int,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    """Run a command so a timeout can terminate its complete process tree."""
    if not text or not capture_output:
        raise InvalidGptCommandOptionsError("GPT commands require captured text output")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        raise
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


def ask_gpt(
    prompt: str,
    result_type: type[Result],
    *,
    web: bool = False,
    model: str = SOL_MODEL,
    reasoning_effort: str = "medium",
    runner: CommandRunner = run_command_in_process_group,
) -> tuple[Result, dict[str, int], str]:
    """Use existing Codex authentication in an isolated directory, retaining the complete event log."""
    with tempfile.TemporaryDirectory(prefix="crm-gpt-") as directory:
        root = Path(directory)
        schema = root / "schema.json"
        output = root / "answer.json"
        schema.write_text(json.dumps(result_type.model_json_schema()))
        command = [private_codex_command(), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "-m", model, "-c", f'model_reasoning_effort="{reasoning_effort}"',
                   "-c", 'web_search="live"' if web else 'web_search="disabled"',
                   "--json", "--output-schema", str(schema), "-o", str(output), "-"]
        failure = ""
        for attempt in range(GPT_ATTEMPTS):
            output.unlink(missing_ok=True)
            try:
                completed = runner(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    cwd=root,
                    timeout=GPT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                failure = "GPT request exceeded ten minutes"
            else:
                if not completed.returncode and output.exists():
                    break
                failure = "GPT request failed; no provider fallback. " + completed.stderr[-1000:]
            if attempt == GPT_ATTEMPTS - 1:
                raise ProviderUnavailable(failure + "; run paused safely")
        usage: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            event = json.loads(line)
            if event.get("item", {}).get("type") in ("command_execution", "mcp_tool_call"):
                raise ProviderUnavailable("GPT used a disallowed tool; result rejected")
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {})
        if not usage:
            raise ProviderUnavailable("GPT did not report a completed turn")
        return result_type.model_validate_json(output.read_text()), usage, completed.stdout
