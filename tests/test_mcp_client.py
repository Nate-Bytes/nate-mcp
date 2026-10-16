import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def test_official_mcp_sdk_client_connects_over_streamable_http(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = "x" * 40

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env.update(
        {
            "MCP_ROOT": str(workspace),
            "MCP_TOKEN": token,
            "MCP_TEST_PORT": str(port),
        }
    )
    command = [
        sys.executable,
        "-c",
        "import os, uvicorn, server; "
        "uvicorn.run(server.build_http_app(os.environ['MCP_TOKEN']), "
        "host='127.0.0.1', port=int(os.environ['MCP_TEST_PORT']), "
        "log_level='error', access_log=False)",
    ]
    server_process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    try:
        for _ in range(100):
            if server_process.poll() is not None:
                raise AssertionError("Uvicorn exited before the MCP endpoint started.")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("MCP endpoint did not start within five seconds.")

        async def exercise_client() -> tuple[list[str], str, bool, str]:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=5
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool("list_directory", {"path": "."})
                        error = await session.call_tool("read_file", {"path": "../outside.txt"})
                        return (
                            [tool.name for tool in tools.tools],
                            result.content[0].text,
                            error.is_error,
                            error.content[0].text,
                        )

        names, listing, is_error, error_text = asyncio.run(exercise_client())
        assert len(names) == 14
        assert "list_directory" in names
        assert listing == ". is empty."
        assert is_error
        assert "not allowed" in error_text
        assert "Traceback" not in error_text
    finally:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_process.wait(timeout=5)
