import datetime
import functools
import hashlib
import hmac
import io
import inspect
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pypdf import PdfReader
from config import (
    BLOCKED_EXTENSIONS,
    DEFAULT_READ_LINES,
    HANDOFF_TEMPLATES,
    IMAGE_EXTENSIONS,
    MAX_GIT_OUTPUT,
    MAX_HANDOFF_ENTRY,
    MAX_HANDOFF_VIEW,
    MAX_IMAGE_SIZE,
    MAX_LIST_ENTRIES,
    MAX_PDF_CHARS,
    MAX_PDF_SIZE,
    MAX_READ_CHARS,
    MAX_SEARCH_FILE,
    MAX_TEXT_FILE,
    MAX_WRITE_SIZE,
    ROOT,
    SKIP_DIRS,
    TEXT_EXTENSIONS,
    TEXT_FILENAMES,
)

log = logging.getLogger("mcp-file-server")

# Serialises every check-then-write so two requests cannot both pass the version
# check and then overwrite each other.
_WRITE_LOCK = threading.Lock()


class SafeToolError(ToolError, ValueError):
    """Expected validation failure surfaced to MCP clients without a traceback."""


def _argument_summary(fn, args, kwargs) -> str:
    bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    parts = []
    for name, value in bound.arguments.items():
        if isinstance(value, str):
            label = "path" if name in {"path", "file"} else "text"
            summary = f"<{label} length={len(value)}>"
        elif isinstance(value, (bool, int, float)):
            summary = repr(value)
        elif value is None:
            summary = "null"
        else:
            summary = f"<{type(value).__name__}>"
        parts.append(f"{name}={summary}")
    return "{" + ", ".join(parts) + "}"


def _logged_tool():
    def decorate(fn):
        @functools.wraps(fn)
        def logged(*args, **kwargs):
            log.info(
                "tool_call timestamp=%s name=%s args=%s",
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                fn.__name__,
                _argument_summary(fn, args, kwargs),
            )
            try:
                return fn(*args, **kwargs)
            except ValueError as exc:
                log.info("tool_rejected name=%s reason=%s", fn.__name__, exc)
                raise SafeToolError(str(exc)) from None
            except Exception as exc:
                log.warning("tool_failed name=%s error=%s", fn.__name__, exc)
                raise

        return mcp.tool()(logged)

    return decorate


mcp = MCPServer(
    "pc-files",
    instructions=(
        "Sandboxed workspace for reading, writing and searching files. All paths are "
        "relative to the workspace root. The four persistent handoff files are plain "
        "text files with no extension under work/: PROJECT_STATE, PLAN_LOG, CHECKPOINTS, "
        "and DECISIONS. At the start of every conversation call handoff_read to read all "
        "four. Before stopping, update PROJECT_STATE with completed work, verification, "
        "unresolved items, and one concrete next action; use the version returned by "
        "handoff_read. Record plans and steps in PLAN_LOG, decisions in DECISIONS, and "
        "verified milestones in CHECKPOINTS. Prefer str_replace over write_file for small "
        "edits. Pass the version from read_file when editing, replacing, or deleting a "
        "file; deleting also requires confirm=true."
    ),
)


# --------------------------------------------------------------------------
# Sandbox helpers
# --------------------------------------------------------------------------
def safe_path(p: str) -> Path:
    """Resolve p against ROOT and refuse anything that lands outside it."""
    if "\x00" in p:
        raise ValueError("Invalid path.")
    root = ROOT.resolve()
    resolved = (root / p).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Access to this path is not allowed.")
    return resolved


def _rel(p: Path) -> str:
    return p.relative_to(ROOT.resolve()).as_posix()


def _check_readable(p: Path) -> None:
    relative = p.relative_to(ROOT.resolve())
    if any(part.lower() == ".git" for part in relative.parts):
        raise ValueError("Reading .git content is not allowed.")
    if any(part.lower() == ".env" for part in relative.parts):
        raise ValueError("Reading .env files is not allowed.")


def _check_text_file(p: Path, user_path: str) -> None:
    if p.suffix.lower() not in TEXT_EXTENSIONS and p.name.lower() not in TEXT_FILENAMES:
        raise ValueError(f"{user_path} is not an allowed text file type.")


def _is_handoff(p: Path) -> bool:
    names = {n.upper() for n in HANDOFF_TEMPLATES}
    return p.parent == (ROOT / "work").resolve() and p.name.upper() in names


def _in_git_dir(p: Path) -> bool:
    return ".git" in {part.lower() for part in p.relative_to(ROOT).parts}


def _check_writable(p: Path, user_path: str) -> None:
    """Rules shared by every tool that changes or deletes files."""
    if p == ROOT:
        raise ValueError("The workspace root itself cannot be modified.")
    if _is_handoff(p):
        raise ValueError(
            f"{user_path} is a handoff file. Use handoff_append or handoff_update_state."
        )
    if _in_git_dir(p):
        raise ValueError("Changing anything inside .git is not allowed.")
    _check_text_file(p, user_path)
    if p.suffix.lower() in BLOCKED_EXTENSIONS:
        raise ValueError(f"{p.suffix.lower()} files are a blocked file type.")


def _require_file(p: Path, user_path: str) -> None:
    if not p.exists():
        raise ValueError(f"{user_path} not found.")
    if not p.is_file():
        raise ValueError(f"{user_path} is not a file.")


def _read_text_file(p: Path, user_path: str, max_bytes: int) -> str:
    _require_file(p, user_path)
    size = p.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{user_path} is too large ({size} bytes). Limit is {max_bytes} bytes.")
    try:
        return p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{user_path} is not a UTF-8 text file.") from None


def _version_of(data: bytes) -> str:
    """Short content hash used as the file version for optimistic locking."""
    return hashlib.sha256(data).hexdigest()[:12]


def _check_version(p: Path, user_path: str, if_version: str) -> None:
    """Refuse the change if the file is no longer the version the caller read."""
    supplied = if_version.strip()
    current = _version_of(p.read_bytes())
    if supplied != current:
        raise ValueError(
            f"Version conflict: {user_path} changed since you read it "
            f"(your version {supplied}, current {current}). Call read_file again, "
            f"merge your change, and retry with the new version."
        )


def _atomic_write(p: Path, data: bytes) -> None:
    """Write via a temp file in the same folder so a crash never leaves a half file."""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".mcp-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_handoff_files() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    handoff_dir = ROOT / "work"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    for name, header in HANDOFF_TEMPLATES.items():
        f = handoff_dir / name
        if not f.exists():
            f.write_text(header, encoding="utf-8")


# --------------------------------------------------------------------------
# Reading tools
# --------------------------------------------------------------------------
@_logged_tool()
def list_directory(path: str = ".") -> str:
    """List the files and folders inside a directory. Folders end with '/'."""
    directory = safe_path(path)
    _check_readable(directory)
    if not directory.exists():
        raise ValueError(f"{path} not found.")
    if not directory.is_dir():
        raise ValueError(f"{path} is not a directory.")
    try:
        items = sorted(directory.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower()))
    except PermissionError:
        raise ValueError(f"No permission to list {path}.") from None
    items = [i for i in items if i.name.lower() != ".env"]
    if not items:
        return f"{path} is empty."
    lines = [f"{i.name}/" if i.is_dir() else i.name for i in items[:MAX_LIST_ENTRIES]]
    if len(items) > MAX_LIST_ENTRIES:
        lines.append(f"[Showing {MAX_LIST_ENTRIES} of {len(items)} entries.]")
    return "\n".join(lines)


@_logged_tool()
def read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LINES) -> str:
    """Read a UTF-8 text file in chunks of lines. offset is the first line (1-based),
    limit is how many lines to return (default 200). Long files end with a note saying
    which offset to use next. The output always ends with [version: ...]; pass that
    value as if_version when you later modify the file. Text files only: use read_image for images and read_pdf
    for PDFs. Executables are blocked."""
    p = safe_path(path)
    _check_readable(p)
    _require_file(p, path)
    _check_text_file(p, path)
    if offset < 1 or limit < 1:
        raise ValueError("offset and limit must be 1 or greater.")
    data = p.read_bytes()
    size = len(data)
    if size > MAX_TEXT_FILE:
        raise ValueError(f"{path} is too large ({size} bytes). Limit is {MAX_TEXT_FILE} bytes.")
    version = _version_of(data)
    try:
        file_text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not a text file. Try read_image or read_pdf.") from None

    shown: list[str] = []
    chars = 0
    total = 0
    shown_end = offset - 1
    clipped = False
    lines = io.StringIO(file_text, newline="").readlines()
    total = len(lines)
    for i, line in enumerate(lines, start=1):
        if i < offset or i >= offset + limit or clipped:
            continue
        if chars + len(line) > MAX_READ_CHARS:
            if not shown:
                shown.append(line[:MAX_READ_CHARS])
                shown_end = i
            clipped = True
            continue
        shown.append(line)
        chars += len(line)
        shown_end = i

    if total == 0:
        return f"{path} is empty.\n[version: {version}]"
    if offset > total:
        raise ValueError(f"offset {offset} is past the end of the file ({total} lines).")

    text = "".join(shown)
    if shown_end < total:
        text += (
            f"\n\n[Showing lines {offset}-{shown_end} of {total}. "
            f"Call read_file again with offset={shown_end + 1} to continue.]"
        )
    elif offset > 1:
        text += f"\n\n[Showing lines {offset}-{shown_end} of {total}. End of file.]"
    text += f"\n[version: {version}] (version of the whole file)"
    return text


@_logged_tool()
def read_image(path: str) -> Image:
    """View an image file (png, jpg, jpeg, gif, webp), up to 5 MB."""
    p = safe_path(path)
    _check_readable(p)
    _require_file(p, path)
    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"{path} is not a supported image type.")
    size = p.stat().st_size
    if size > MAX_IMAGE_SIZE:
        raise ValueError(f"{path} is too large ({size} bytes). Limit is {MAX_IMAGE_SIZE} bytes.")
    return Image(path=p)


@_logged_tool()
def read_pdf(path: str, max_pages: int = 20) -> str:
    """Extract the text of a PDF. Reads up to max_pages pages (default 20, max 100).
    Scanned PDFs without a text layer return no text."""
    if not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be between 1 and 100.")
    p = safe_path(path)
    _check_readable(p)
    _require_file(p, path)
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"{path} is not a PDF.")
    size = p.stat().st_size
    if size > MAX_PDF_SIZE:
        raise ValueError(f"{path} is too large ({size} bytes). Limit is {MAX_PDF_SIZE} bytes.")

    try:
        reader = PdfReader(p)
        if reader.is_encrypted:
            raise ValueError(f"{path} is password-protected.")
        total = len(reader.pages)
        count = min(max_pages, total)
        texts = [reader.pages[i].extract_text() or "" for i in range(count)]
    except ValueError:
        raise
    except Exception as e:  # pypdf raises many error types for damaged files
        raise ValueError(f"Could not read {path}: {e}") from e

    if not any(t.strip() for t in texts):
        return f"{path} has no extractable text. It may be a scanned PDF."

    result = "\n\n".join(f"--- Page {i + 1} ---\n{t}" for i, t in enumerate(texts))
    if len(result) > MAX_PDF_CHARS:
        result = result[:MAX_PDF_CHARS] + "\n\n[Text truncated.]"
    if count < total:
        result += f"\n\n[Showing {count} of {total} pages. Raise max_pages to read more.]"
    return result


# --------------------------------------------------------------------------
# Writing tools
# --------------------------------------------------------------------------
@_logged_tool()
def write_file(path: str, content: str, overwrite: bool = False, if_version: str = "") -> str:
    """Create a text file (UTF-8, up to 1 MB). Parent folders are created as needed.
    A new file needs no version. Replacing an existing file needs overwrite=true AND
    if_version (the [version: ...] shown by read_file). If the file changed since you
    read it, the write is refused so you can re-read and merge. Returns the new version."""
    p = safe_path(path)
    _check_writable(p, path)
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_SIZE:
        raise ValueError(f"Content is too large ({len(data)} bytes). Limit is {MAX_WRITE_SIZE} bytes.")
    if p.is_dir():
        raise ValueError(f"{path} is a directory.")
    with _WRITE_LOCK:
        if p.exists():
            if not overwrite:
                raise ValueError(
                    f"{path} already exists. Pass overwrite=true and if_version to replace it."
                )
            if not if_version.strip():
                raise ValueError(
                    "Replacing a file requires if_version. Call read_file first and pass "
                    "the version it shows."
                )
            _check_version(p, path, if_version)
        elif if_version.strip():
            raise ValueError(f"{path} no longer exists, but if_version was given. It may have been deleted.")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(p, data)
        except OSError as e:
            raise ValueError(f"Could not write {path}: {e.strerror}") from e
    log.info("write_file %s (%d bytes)", _rel(p), len(data))
    return f"Wrote {len(data)} bytes to {path}. [version: {_version_of(data)}]"


@_logged_tool()
def str_replace(path: str, old_str: str, new_str: str, if_version: str) -> str:
    """Replace one exact piece of text in a text file. old_str must appear exactly once;
    if it appears zero or several times the call fails, so include enough surrounding
    context to make it unique. Pass if_version (from read_file) to refuse the edit if the
    file changed since you read it. if_version from read_file is required. Returns the new version."""
    p = safe_path(path)
    _check_writable(p, path)
    _check_readable(p)
    if old_str == "":
        raise ValueError("old_str must not be empty.")
    with _WRITE_LOCK:
        _require_file(p, path)
        if not if_version.strip():
            raise ValueError("str_replace requires if_version from read_file.")
        _check_version(p, path, if_version)
        text = _read_text_file(p, path, MAX_WRITE_SIZE)

        # Keep the file's own line endings if it uses CRLF and the caller sent LF.
        if "\r\n" in text and "\r\n" not in old_str:
            old_str = old_str.replace("\n", "\r\n")
            new_str = new_str.replace("\n", "\r\n")

        count = text.count(old_str)
        if count == 0:
            raise ValueError(f"old_str was not found in {path}.")
        if count > 1:
            raise ValueError(
                f"old_str matches {count} times in {path}. Add more surrounding text so it is unique."
            )
        index = text.index(old_str)
        line_no = text.count("\n", 0, index) + 1
        updated = text[:index] + new_str + text[index + len(old_str):]
        data = updated.encode("utf-8")
        if len(data) > MAX_WRITE_SIZE:
            raise ValueError("The result would be larger than the write size limit.")
        try:
            _atomic_write(p, data)
        except OSError as e:
            raise ValueError(f"Could not write {path}: {e.strerror}") from e
    log.info("str_replace %s (line %d)", _rel(p), line_no)
    return f"Replaced 1 occurrence in {path} at line {line_no}. [version: {_version_of(data)}]"


@_logged_tool()
def delete_file(path: str, confirm: bool = False, if_version: str = "") -> str:
    """Delete one file. Requires confirm=true. Pass if_version (from read_file) to refuse
    the delete if the file changed since you read it. Folders, handoff files and anything
    inside .git cannot be deleted."""
    p = safe_path(path)
    if not confirm:
        raise ValueError("Deleting requires confirm=true. Call again with confirm=true to proceed.")
    _check_writable(p, path)
    if not if_version.strip():
        raise ValueError("Deleting a file requires if_version from read_file.")
    with _WRITE_LOCK:
        _require_file(p, path)
        if if_version.strip():
            _check_version(p, path, if_version)
        try:
            p.unlink()
        except OSError as e:
            raise ValueError(f"Could not delete {path}: {e.strerror}") from e
    log.warning("delete_file %s", _rel(p))
    return f"Deleted {path}."


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
@_logged_tool()
def search_files(query: str, path: str = ".", content: bool = False, max_results: int = 50) -> str:
    """Find files and folders whose NAME contains query (case-insensitive). With
    content=true, also search inside text files and report matching lines as
    'path:line: text'. Skips .git, node_modules, .venv and similar folders."""
    q = query.strip().lower()
    if not q:
        raise ValueError("query must not be empty.")
    base = safe_path(path)
    _check_readable(base)
    if not base.is_dir():
        raise ValueError(f"{path} is not a directory.")
    max_results = max(1, min(max_results, 200))

    results: list[str] = []
    truncated = False

    def add(entry: str) -> bool:
        nonlocal truncated
        if len(results) >= max_results:
            truncated = True
            return False
        results.append(entry)
        return True

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d.lower() not in SKIP_DIRS and d.lower() != ".env"
        )
        here = Path(dirpath)
        for d in dirnames:
            if q in d.lower() and not add(_rel(here / d) + "/"):
                return _format_search(results, truncated)
        for name in sorted(filenames):
            if name.lower() == ".env":
                continue
            fp = here / name
            rel = _rel(fp)
            if q in name.lower() and not add(rel):
                return _format_search(results, truncated)
            if content and (
                fp.suffix.lower() in TEXT_EXTENSIONS or fp.name.lower() in TEXT_FILENAMES
            ):
                for hit in _search_in_file(fp, rel, q):
                    if not add(hit):
                        return _format_search(results, truncated)
    return _format_search(results, truncated)


def _search_in_file(fp: Path, rel: str, q: str, per_file: int = 5) -> list[str]:
    try:
        real = fp.resolve()
        _check_readable(real)
        _check_text_file(real, rel)
        if not real.is_relative_to(ROOT.resolve()) or real.stat().st_size > MAX_SEARCH_FILE:
            return []
        hits = []
        with open(real, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if q in line.lower():
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= per_file:
                        break
        return hits
    except (UnicodeDecodeError, OSError, ValueError):
        return []  # binary or unreadable: skip quietly


def _format_search(results: list[str], truncated: bool) -> str:
    if not results:
        return "No matches."
    text = "\n".join(results)
    if truncated:
        text += "\n[Results truncated. Narrow the query or raise max_results.]"
    return text


# --------------------------------------------------------------------------
# git-lite (read-only)
# --------------------------------------------------------------------------
def _git(args: list[str]) -> str:
    if not (ROOT / ".git").exists():
        return "The workspace is not a git repository."
    cmd = ["git", "-c", "core.fsmonitor=false", "--no-pager", *args]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, env=env,
        )
    except FileNotFoundError:
        raise ValueError("git is not installed or not on PATH.") from None
    except subprocess.TimeoutExpired:
        raise ValueError("git took too long and was stopped.") from None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "does not have any commits" in msg:
            return "No commits yet."
        raise ValueError(f"git failed: {msg[:500]}")
    out = r.stdout.strip()
    if len(out) > MAX_GIT_OUTPUT:
        out = out[:MAX_GIT_OUTPUT] + "\n[Output truncated.]"
    return out


@_logged_tool()
def git_status() -> str:
    """Show the branch and changed or untracked files in the workspace (read-only)."""
    return _git(["status", "--short", "--branch"]) or "No changes."


@_logged_tool()
def git_diff(path: str = ".", staged: bool = False) -> str:
    """Show uncommitted changes for a file or folder (read-only). staged=true shows
    what is staged for commit instead."""
    p = safe_path(path)
    _check_readable(p)
    args = ["diff", "--no-ext-diff", "--no-textconv"]
    if staged:
        args.append("--cached")
    args += ["--", _rel(p)]
    return _git(args) or "No changes."


@_logged_tool()
def git_log(max_count: int = 10) -> str:
    """Show the most recent commits, one per line (read-only)."""
    n = max(1, min(max_count, 50))
    return _git(["log", "--oneline", f"-{n}"])


# --------------------------------------------------------------------------
# Handoff protocol
# --------------------------------------------------------------------------
def _canonical_handoff(name: str) -> str:
    key = name.strip().upper()
    for canonical in HANDOFF_TEMPLATES:
        if canonical.upper() == key:
            return canonical
    allowed = ", ".join(HANDOFF_TEMPLATES)
    raise ValueError(f"Unknown handoff file '{name}'. Use an extensionless name: {allowed}.")


@_logged_tool()
def handoff_read() -> str:
    """Read all four persistent, extensionless files in work/. Call first in every
    conversation. Each file's content version is included for safe state updates."""
    _ensure_handoff_files()
    parts = []
    for name in HANDOFF_TEMPLATES:
        data = (ROOT / "work" / name).read_bytes()
        text = data.decode("utf-8", errors="replace")
        if len(text) > MAX_HANDOFF_VIEW:
            omitted = len(text) - MAX_HANDOFF_VIEW
            text = f"[...{omitted} older characters omitted...]\n" + text[-MAX_HANDOFF_VIEW:]
        parts.append(f"===== {name} =====\n{text.strip()}\n[version: {_version_of(data)}]")
    return "\n\n".join(parts)


@_logged_tool()
def handoff_append(file: str, entry: str) -> str:
    """Append a timestamped plain-text entry to one extensionless handoff file in work/.
    Entries are only ever added, never edited or removed (max 10,000 characters)."""
    name = _canonical_handoff(file)
    entry = entry.strip()
    if not entry:
        raise ValueError("entry must not be empty.")
    if len(entry) > MAX_HANDOFF_ENTRY:
        raise ValueError(f"entry is too long. Limit is {MAX_HANDOFF_ENTRY} characters.")
    _ensure_handoff_files()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _WRITE_LOCK:
        with open(ROOT / "work" / name, "a", encoding="utf-8") as f:
            f.write(f"\n[{stamp}]\n{entry}\n")
    log.info("handoff_append %s", name)
    return f"Added an entry to {name}."


@_logged_tool()
def handoff_update_state(
    completed_work: str,
    verification: str,
    unresolved_items: str,
    next_action: str,
    if_version: str,
) -> str:
    """Update work/PROJECT_STATE before stopping. Record completed work, verification,
    unresolved items, and one concrete next action. Pass its current version from
    handoff_read; stale updates are rejected. Use 'None' when no items remain."""
    values = {
        "Completed work": completed_work,
        "Verification": verification,
        "Unresolved items": unresolved_items,
        "Next action": next_action,
    }
    if any(not value.strip() for value in values.values()):
        raise ValueError("Every state field must be non-empty; use 'None' where applicable.")
    content = "\n\n".join(f"{label}:\n{value.strip()}" for label, value in values.items())
    if len(content) > MAX_HANDOFF_VIEW:
        raise ValueError(f"content is too long. Limit is {MAX_HANDOFF_VIEW} characters.")
    _ensure_handoff_files()
    with _WRITE_LOCK:
        if not if_version.strip():
            raise ValueError("handoff_update_state requires if_version from handoff_read.")
        state_file = ROOT / "work" / "PROJECT_STATE"
        _check_version(state_file, "work/PROJECT_STATE", if_version)
        _atomic_write(state_file, content.encode("utf-8") + b"\n")
    log.info("handoff_update_state")
    return "work/PROJECT_STATE updated."


# --------------------------------------------------------------------------
# HTTP serving: bearer-token auth around the MCP app
# --------------------------------------------------------------------------
class AuthMiddleware:
    """Pure-ASGI gate. Accepts either 'Authorization: Bearer <token>' or, when a URL
    secret is configured, a path of the form /s/<secret>/mcp (secret is stripped)."""

    def __init__(self, app, token: str, url_secret: str | None = None):
        self.app = app
        self._token = token.encode("utf-8")
        self._secret = url_secret.encode("utf-8") if url_secret else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._secret:
            parts = scope["path"].split("/")  # ['', 's', '<secret>', 'mcp', ...]
            if len(parts) >= 3 and parts[1] == "s" and hmac.compare_digest(
                parts[2].encode("utf-8"), self._secret
            ):
                new_path = "/" + "/".join(parts[3:])
                scope = dict(scope, path=new_path, raw_path=new_path.encode("utf-8"))
                await self.app(scope, receive, send)
                return

        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if hmac.compare_digest(supplied, b"Bearer " + self._token):
            await self.app(scope, receive, send)
            return

        body = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_http_app(token: str, url_secret: str | None = None, allowed_hosts=()):
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for h in allowed_hosts:
        hosts += [h, f"{h}:*"]
        origins.append(f"https://{h}")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
    app = mcp.streamable_http_app(stateless_http=True, transport_security=security)
    app.add_middleware(AuthMiddleware, token=token, url_secret=url_secret)
    return app


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),  # never stdout: stdio mode uses it
            logging.FileHandler(Path(__file__).with_name("mcp-server.log"), encoding="utf-8"),
        ],
        force=True,
    )


def serve_http() -> None:
    import uvicorn

    token = os.environ.get("MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_TOKEN is missing or shorter than 32 characters. Refusing to start.")
    url_secret = os.environ.get("MCP_URL_SECRET") or None
    if url_secret and len(url_secret) < 32:
        raise SystemExit("MCP_URL_SECRET must be at least 32 characters. Refusing to start.")
    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))

    _setup_logging()
    _ensure_handoff_files()
    app = build_http_app(token, url_secret, allowed)
    log.info("Workspace root: %s", ROOT)
    log.info("Serving MCP on http://%s:%d/mcp (allowed tunnel hosts: %s)", host, port, allowed or "none")
    # access_log is off on purpose: with a URL secret the path itself is a credential.
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="info")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        serve_http()
    else:
        _setup_logging()
        _ensure_handoff_files()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
