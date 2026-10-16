import json
from pathlib import Path

from starlette.testclient import TestClient

import server


def test_mcp_http_endpoint_requires_a_bearer_token(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(server, "ROOT", root.resolve())
    token = "t" * 40
    app = server.build_http_app(token)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "auth-test", "version": "1.0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "127.0.0.1:8000",
    }
    with TestClient(app) as client:
        assert client.post("/mcp", json=payload, headers=headers).status_code == 401
        assert client.post(
            "/mcp",
            json=payload,
            headers={**headers, "Authorization": "Bearer wrong-token"},
        ).status_code == 401
        response = client.post(
            "/mcp",
            json=payload,
            headers={**headers, "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        event = next(
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
        assert json.loads(event)["result"]["serverInfo"]["name"] == "pc-files"
