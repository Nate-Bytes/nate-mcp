# MCP File Server

A Python MCP server that exposes a sandboxed workspace over stdio or Streamable
HTTP. Its 14 tools list, read, search, write, and delete files; read PDFs and
images; inspect Git; and maintain four handoff files.

## Requirements and installation

- Windows 10/11 or another OS supported by Python and the MCP SDK
- Python 3.11+
- Git for the read-only Git tools
- ngrok only if you need remote HTTPS access

From the repository directory, create a virtual environment and install pinned
dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Copy the example configuration and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

`MCP_ROOT` is the only required setting for choosing the workspace. If omitted,
the server uses a `workspace` folder beside `server.py`. Create the folder if
needed. Values in the process environment take precedence over `.env`.

| Setting | Purpose |
| --- | --- |
| `MCP_ROOT` | Workspace folder the server may access. Use an absolute path for a service. |
| `MCP_TOKEN` | Required HTTP bearer token, at least 32 characters. |
| `MCP_URL_SECRET` | Optional 32+ character URL credential for clients that cannot send headers. |
| `MCP_HOST` | HTTP bind address; defaults to `127.0.0.1`. |
| `MCP_PORT` | HTTP port; defaults to `8000`. |
| `MCP_ALLOWED_HOSTS` | Comma-separated tunnel hostnames, without scheme or path. |

Generate distinct strong secrets in PowerShell; run the command separately for
each configured secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not commit or share `.env`. The committed `.env.example` contains placeholders
only.

## Run locally over stdio

Run from the repository directory:

```powershell
python server.py
```

This is the appropriate transport when an MCP client launches the server as a
local process. The process reads/writes the MCP protocol on standard input and
output; diagnostic logs go to stderr and `mcp-server.log`.

## Run over Streamable HTTP

Ensure `.env` has a strong `MCP_TOKEN`, then start the server:

```powershell
python server.py http
```

It listens on `http://127.0.0.1:8000/mcp` by default. The HTTP endpoint accepts
`Authorization: Bearer <MCP_TOKEN>`. Do not bind to a public interface directly;
put an HTTPS tunnel or reverse proxy in front of the local listener.

### ngrok with a reserved static domain

1. Install ngrok and authenticate its CLI using your ngrok account:

   ```powershell
   ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
   ```

2. Reserve a static domain in ngrok.
3. Set `MCP_ALLOWED_HOSTS` in `.env` to that hostname alone, for example
   `mcp-yourname.ngrok-free.app`. Restart the MCP server after changing `.env`.
4. Start the tunnel in another terminal:

   ```powershell
   ngrok http --url=YOUR-RESERVED-DOMAIN.ngrok-free.app 8000
   ```

5. The remote MCP endpoint is `https://YOUR-RESERVED-DOMAIN/mcp`.

For a temporary ngrok domain, run `ngrok http 8000`, then put the assigned
hostname in `MCP_ALLOWED_HOSTS` and restart the server. Temporary domains can
change between runs.

### Connect Claude

In Claude, add a custom connector for a remote MCP server and use the HTTPS
endpoint above. Select **Streamable HTTP**. This server does not implement
OAuth:

- If Claude lets you configure an authorization header, choose **No sign-in**
  (API-key authentication) and configure `Authorization: Bearer <MCP_TOKEN>`.
- If Claude cannot send a custom header, set a separate `MCP_URL_SECRET` and use
  `https://YOUR-RESERVED-DOMAIN/s/YOUR_MCP_URL_SECRET/mcp`. Select **No sign-in**.

The URL-secret option is weaker: the secret is part of the URL and may be
recorded by client, proxy, or tunnel infrastructure. Use HTTPS, disable request
URL logging where possible, and rotate the secret if it is exposed. Never paste
the literal secrets into documentation, screenshots, or transcripts.

## Tools and safety behavior

The server currently exposes these tools; retain these names in clients:

`list_directory`, `read_file`, `read_image`, `read_pdf`, `write_file`,
`str_replace`, `delete_file`, `search_files`, `git_status`, `git_diff`,
`git_log`, `handoff_read`, `handoff_append`, `handoff_update_state`.

- Paths are resolved against `MCP_ROOT`; traversal, absolute paths outside the
  root, and symlinks escaping the root are rejected.
- File reads and writes use an explicit text-extension allowlist. Images and
  PDFs have their own bounded read tools.
- Direct reads of `.git` and `.env` are blocked; `.git` writes and all writes to
  the four handoff files in `work\` are blocked.
- Replacing a file requires `overwrite=true` and the current `if_version` from
  `read_file`. `str_replace` also requires that version. Deletion requires both
  `confirm=true` and that version. `handoff_read` returns a version for
  `handoff_update_state`; stale versions are rejected.
- Each MCP tool call is logged with a UTC timestamp, tool name, and redacted
  argument summary. File content, tokens, and secret values are not logged.
- URL-secret authentication remains less safe than bearer headers because the
  credential is present in the request path.

## Persistent handoff across conversations

The server creates and maintains the same four plain-text files under
`<MCP_ROOT>\work\`. Their filenames have **no extension**:

- `work\PROJECT_STATE`
- `work\PLAN_LOG`
- `work\CHECKPOINTS`
- `work\DECISIONS`

These are workspace files shared by conversations connected to the same
`MCP_ROOT`; they are not per-session notes and are protected from deletion or
ordinary writes through the file tools.

At the beginning of **every** conversation, call `handoff_read` to read all four
files. Before stopping, call `handoff_update_state` with:

1. completed work,
2. verification actually performed,
3. unresolved items (use `None` if there are none), and
4. one concrete next action.

Use the `PROJECT_STATE` version returned by `handoff_read`; if another
conversation updated it, the stale update is rejected. Record a plan before
acting and progress in `PLAN_LOG` with `handoff_append`; log decisions in
`DECISIONS` and verified milestones in `CHECKPOINTS`. These files remain
ordinary readable text on disk and can also be inspected directly in the
workspace folder.

## Tests and verification

Run the full test suite:

```powershell
python -m pytest -q
```

The tests cover path traversal, null bytes, Windows/UNC paths, symlink escapes,
protected files, extension allowlisting, destructive confirmation and version
checks, bearer-token responses, and an official Python MCP SDK client making a
Streamable HTTP connection.

For MCP Inspector, run `python server.py` for local stdio or `python server.py
http` behind HTTPS. Verify a valid file read and a rejected traversal/read of
`.env`. Automated tests do not simulate a signed-in Claude connector session or
a conversation resumed in a separate session; verify those in your client.
