"""Offline rejection checks for the Python worker's untrusted pipe boundary."""
import asyncio
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


def test_sdk_receives_ordered_image_blocks(monkeypatch):
    """Images reach the SDK as image blocks alongside quoted conversation text."""
    worker = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'runtime/claude_agent/worker.py'))
    namespace = worker['main'].__globals__
    captured = []
    class Client:
        """Capture the SDK request without contacting a provider."""
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get_server_info(self): return {}
        async def query(self, prompt):
            captured.extend([message async for message in prompt])
        async def receive_response(self):
            yield worker['ResultMessage'](subtype='success', duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id='fixture', result='done')
    monkeypatch.setitem(namespace, 'ClaudeSDKClient', Client)
    monkeypatch.setitem(namespace, 'send', lambda event: None)
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Inspect'}, image]},
               {'role': 'assistant', 'content': 'Earlier reply'}]
    asyncio.run(worker['main']({'mode': 'chat', 'model': 'fixture', 'executable': 'unused', 'history': history}, {}))
    parts = captured[0]['message']['content']
    assert parts[2] == image
    assert json.loads(parts[1]['text']) == {'role': 'user', 'content': 'Inspect'}
    assert json.loads(parts[3]['text']) == history[1]


@pytest.mark.parametrize("payload", [
    b"[]\n", b'{"mode":\n', b'{"mode":"models","value":NaN}\n',
    b'{"mode":"models","value":1e999}\n', b'{"mode":"not-a-mode"}\n',
    b'{"mode":"models","output_schema":{"type":"array"}}\n',
    b'{"mode":"chat","tools":[{"name":"bad/tool","input_schema":{"type":"object"}}]}\n',
    b'{"mode":"chat","tools":[null]}\n',
    (json.dumps({"mode": "chat", "tools": [{"name": "tool_" + str(n)} for n in range(257)]}) + "\n").encode(),
    b"x" * (2 * 1024 * 1024 + 1) + b"\n",
])
def test_invalid_pipe_input_fails_without_native_execution_or_secret_output(tmp_path, payload):
    """Malformed input cannot submit a prompt, dispatch a tool or reflect its payload."""
    worker = Path(__file__).resolve().parents[1] / "runtime/claude_agent/worker.py"
    response = subprocess.run([sys.executable, "-I", str(worker)], input=payload, capture_output=True,
        cwd=tmp_path, env={"PATH": "", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path)}, timeout=10)
    assert response.returncode == 1
    events = [json.loads(line) for line in response.stdout.splitlines()]
    assert len(events) == 1 and events[0]["type"] == "error"
    assert events[0]["message"] in {
        "Claude runtime protocol failed.", "Claude did not return the required structured decision.",
        "Claude enabled an unexpected tool.", "Claude runtime failed. Check dependencies and subscription sign-in.",
        "Claude tool catalogue is invalid or exceeds its size limit.",
    }
    assert not any(tmp_path.iterdir()), "Rejected input started native profile work"


@pytest.mark.parametrize("enabled", ["mcp__pbgui__fixture", "mcp__pbgui__unknown", "Bash"])
def test_large_catalog_keeps_permission_and_init_allowlists(monkeypatch, enabled):
    """A larger catalogue never authorizes builtins or unadvertised MCP capabilities."""
    path = Path(__file__).resolve().parents[1] / "runtime/claude_agent/worker.py"
    worker = runpy.run_path(str(path))
    namespace = worker["main"].__globals__
    events = []

    class Client:
        """Supply SDK metadata without starting a process or contacting a provider."""

        def __init__(self, options):
            """Capture the production options and permission callback."""
            self.options = options

        async def __aenter__(self):
            """Check permission decisions independently of the provider's init metadata."""
            permission = self.options.can_use_tool
            assert isinstance(await permission("mcp__pbgui__fixture", {}, None), worker["PermissionResultAllow"])
            for name in ("Bash", "mcp__pbgui__unknown", "StructuredOutput"):
                assert isinstance(await permission(name, {}, None), worker["PermissionResultDeny"])
            return self

        async def __aexit__(self, *args):
            """The synthetic client owns no external resources."""

        async def get_server_info(self):
            """Return metadata without account credentials."""
            return {}

        async def query(self, prompt):
            """Accept only the fixture prompt."""

        async def receive_response(self):
            """Exercise the production enabled-tool validation before completion."""
            yield worker["SystemMessage"](subtype="init", data={"model": "fixture", "tools": [enabled]})
            yield worker["ResultMessage"](subtype="success", duration_ms=0, duration_api_ms=0,
                                          is_error=False, num_turns=1, session_id="fixture", result="done")

    monkeypatch.setitem(namespace, "ClaudeSDKClient", Client)
    monkeypatch.setitem(namespace, "send", events.append)
    tools = [{"name": "fixture"}] + [{"name": "tool_" + str(n)} for n in range(255)]
    request = {"mode": "chat", "model": "fixture", "executable": "unused", "tools": tools}
    if enabled == "mcp__pbgui__fixture":
        asyncio.run(worker["main"](request, {}))
        assert events == [{"type": "result", "result": "done"}]
    else:
        with pytest.raises(worker["WorkerError"], match="^tools$"):
            asyncio.run(worker["main"](request, {}))
        assert not events
