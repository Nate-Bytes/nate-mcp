import base64
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setattr(server, "ROOT", root.resolve())
    return root


def test_write_file_creates_parent_and_rejects_existing_file(sandbox: Path) -> None:
    assert server.write_file("nested/file.txt", "hello").startswith("Wrote 5 bytes to nested/file.txt.")
    with pytest.raises(ValueError, match="already exists"):
        server.write_file("nested/file.txt", "again")
    version = server.read_file("nested/file.txt").split("[version: ")[-1].split("]")[0]
    assert server.write_file(
        "nested/file.txt", "again", overwrite=True, if_version=version
    ).startswith("Wrote")
    assert (sandbox / "nested/file.txt").read_text(encoding="utf-8") == "again"


def test_write_file_rejects_oversized_content_and_protected_paths(sandbox: Path) -> None:
    with pytest.raises(ValueError, match="too large"):
        server.write_file("big.txt", "x" * (server.MAX_WRITE_SIZE + 1))
    with pytest.raises(ValueError, match="workspace root"):
        server.write_file(".", "no")


def test_str_replace_requires_one_unique_match(sandbox: Path) -> None:
    (sandbox / "sample.txt").write_text("first\nneedle\nlast\n", encoding="utf-8")
    version = server.read_file("sample.txt").split("[version: ")[-1].split("]")[0]
    assert "line 2" in server.str_replace(
        "sample.txt", "needle", "updated", if_version=version
    )
    assert "updated" in (sandbox / "sample.txt").read_text(encoding="utf-8")
    version = server.read_file("sample.txt").split("[version: ")[-1].split("]")[0]
    with pytest.raises(ValueError, match="not found"):
        server.str_replace("sample.txt", "absent", "replacement", if_version=version)
    (sandbox / "sample.txt").write_text("repeat repeat", encoding="utf-8")
    version = server.read_file("sample.txt").split("[version: ")[-1].split("]")[0]
    with pytest.raises(ValueError, match="matches 2 times"):
        server.str_replace("sample.txt", "repeat", "replacement", if_version=version)
    with pytest.raises(ValueError, match="must not be empty"):
        server.str_replace("sample.txt", "", "x", if_version=version)


def test_delete_file_only_deletes_unprotected_files(sandbox: Path) -> None:
    (sandbox / "remove.txt").write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="confirm=true"):
        server.delete_file("remove.txt")
    version = server.read_file("remove.txt").split("[version: ")[-1].split("]")[0]
    with pytest.raises(ValueError, match="if_version"):
        server.delete_file("remove.txt", confirm=True)
    assert server.delete_file("remove.txt", confirm=True, if_version=version) == "Deleted remove.txt."
    with pytest.raises(ValueError, match="workspace root"):
        server.delete_file(".", confirm=True)
    with pytest.raises(ValueError, match="not allowed"):
        server.delete_file("..\\outside.txt")


def test_search_files_matches_names_and_content_and_skips_ignored_dirs(sandbox: Path) -> None:
    (sandbox / "Needle-name.txt").write_text("line one\nneedle in content\n", encoding="utf-8")
    (sandbox / "other.txt").write_text("no match\n", encoding="utf-8")
    ignored = sandbox / ".git"
    ignored.mkdir()
    (ignored / "needle.txt").write_text("needle", encoding="utf-8")
    output = server.search_files("needle", content=True)
    assert "Needle-name.txt" in output
    assert "Needle-name.txt:2" in output
    assert ".git" not in output
    assert "No matches" in server.search_files("not-there")


def test_search_files_reports_truncation_only_when_needed(sandbox: Path) -> None:
    (sandbox / "match-one.txt").touch()
    (sandbox / "match-two.txt").touch()
    truncated = server.search_files("match", max_results=1)
    assert "[Results truncated." in truncated
    (sandbox / "match-two.txt").unlink()
    exact = server.search_files("match", max_results=1)
    assert "[Results truncated" not in exact


def test_git_commands_use_argument_list_and_handle_non_repository(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="## main\n", stderr=""),
    )
    assert server.git_status() == "The workspace is not a git repository."
    (sandbox / ".git").mkdir()
    assert server.git_status() == "## main"
    assert server.git_diff() == "## main"
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert server.git_status() == "No changes."


def test_handoff_files_are_read_appended_and_protected(sandbox: Path) -> None:
    handoff = server.handoff_read()
    assert "===== PROJECT_STATE =====" in handoff
    for name in ("PROJECT_STATE", "PLAN_LOG", "CHECKPOINTS", "DECISIONS"):
        assert (sandbox / "work" / name).is_file()
        assert not (sandbox / "work" / f"{name}.md").exists()
        assert not (sandbox / f"{name}.md").exists()
    assert "Added" in server.handoff_append("PROJECT_STATE", "Working on tests")
    handoff = server.handoff_read()
    assert "Working on tests" in handoff
    version = next(
        line.removeprefix("[version: ").removesuffix("]")
        for line in handoff.splitlines()
        if line.startswith("[version: ")
    )
    state_fields = {
        "completed_work": "Updated handoff layout",
        "verification": "Handoff tests passed",
        "unresolved_items": "None",
        "next_action": "Continue with user demo",
        "if_version": version,
    }
    assert "work/PROJECT_STATE" in server.handoff_update_state(**state_fields)
    with pytest.raises(ValueError, match="Version conflict"):
        server.handoff_update_state(**{**state_fields, "completed_work": "Stale update"})
    with pytest.raises(ValueError, match="extensionless name"):
        server.handoff_append("PROJECT_STATE.md", "not allowed")
    with pytest.raises(ValueError, match="Unknown handoff file"):
        server.handoff_append("../outside", "not allowed")
    with pytest.raises(ValueError, match="handoff file"):
        server.delete_file("work/PROJECT_STATE", confirm=True, if_version=version)


def test_read_file_text_size_encoding_and_extension_guards(sandbox: Path) -> None:
    (sandbox / "text.txt").write_text("hello", encoding="utf-8")
    assert server.read_file("text.txt").startswith("hello")
    with pytest.raises(ValueError, match="not a text file"):
        (sandbox / "invalid.txt").write_bytes(b"\xff")
        server.read_file("invalid.txt")
    (sandbox / "program.exe").write_bytes(b"binary")
    with pytest.raises(ValueError, match="allowed text file type"):
        server.read_file("program.exe")
    (sandbox / "large.txt").write_bytes(b"x" * (server.MAX_TEXT_FILE + 1))
    with pytest.raises(ValueError, match="too large"):
        server.read_file("large.txt")


def test_read_image_rejects_unsupported_extension_and_missing_file(sandbox: Path) -> None:
    (sandbox / "image.txt").write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="supported image"):
        server.read_image("image.txt")
    with pytest.raises(ValueError, match="not found"):
        server.read_image("missing.png")


def test_read_image_accepts_png(sandbox: Path) -> None:
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
        "lXcAAAAASUVORK5CYII="
    )
    (sandbox / "pixel.png").write_bytes(image_bytes)
    assert server.read_image("pixel.png").path == sandbox / "pixel.png"


def test_read_pdf_rejects_invalid_arguments_and_file(sandbox: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        server.read_pdf("missing.pdf", max_pages=0)
    with pytest.raises(ValueError, match="not found"):
        server.read_pdf("missing.pdf")
    (sandbox / "not-pdf.txt").write_text("text", encoding="utf-8")
    with pytest.raises(ValueError, match="not a PDF"):
        server.read_pdf("not-pdf.txt")


def test_read_pdf_reads_pdf_pages(sandbox: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with (sandbox / "blank.pdf").open("wb") as pdf_file:
        writer.write(pdf_file)
    assert "no extractable text" in server.read_pdf("blank.pdf")


def test_list_directory_lists_files_and_reports_missing_directory(sandbox: Path) -> None:
    (sandbox / "entry.txt").touch()
    assert server.list_directory(".") == "entry.txt"
    with pytest.raises(ValueError, match="not found"):
        server.list_directory("missing")


def test_registered_tool_count_and_instructions() -> None:
    assert len(server.mcp._tool_manager.list_tools()) == 14
    assert "call handoff_read" in server.mcp.instructions


def test_calls_are_logged_without_logging_file_contents(
    sandbox: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (sandbox / "note.txt").write_text("do not log this private content", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="mcp-file-server"):
        server.read_file("note.txt")
    assert "tool_call timestamp=" in caplog.text
    assert "name=read_file" in caplog.text
    assert "do not log this private content" not in caplog.text


def test_bearer_middleware_rejects_missing_and_accepts_valid_token() -> None:
    async def endpoint(_: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    protected_app = server.AuthMiddleware(app, "test-token")
    with TestClient(protected_app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = client.get("/", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert response.text == "ok"
