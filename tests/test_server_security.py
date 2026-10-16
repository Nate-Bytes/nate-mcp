from pathlib import Path

import pytest

import server


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setattr(server, "ROOT", root.resolve())
    return root


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        r"..\outside.txt",
        r"C:\Windows\win.ini",
        "C:/Windows/win.ini",
        r"\\server\share\secret.txt",
        "\x00bad.txt",
    ],
)
def test_safe_path_rejects_traversal_absolute_unc_and_null(
    sandbox: Path, path: str
) -> None:
    with pytest.raises(ValueError):
        server.safe_path(path)


def test_safe_path_rejects_sibling_prefix(sandbox: Path) -> None:
    sibling = Path(f"{sandbox}_evil")
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path(str(sibling))


def test_symlink_escape_is_rejected(
    sandbox: Path, tmp_path: Path, create_directory_link
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sandbox / "outside-link"
    if not create_directory_link(link, outside):
        pytest.skip("Creating a directory link is unavailable.")
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path("outside-link")


def test_handoff_files_and_git_metadata_cannot_be_written(sandbox: Path) -> None:
    git_dir = sandbox / ".git"
    git_dir.mkdir()
    handoff_dir = sandbox / "work"
    handoff_dir.mkdir()
    protected = handoff_dir / "PROJECT_STATE"
    protected.write_text("state\n", encoding="utf-8")

    with pytest.raises(ValueError, match="handoff file"):
        server.write_file("work/PROJECT_STATE", "overwrite")
    with pytest.raises(ValueError, match="handoff file"):
        server.str_replace("work/PROJECT_STATE", "state", "changed", if_version="stale")
    with pytest.raises(ValueError, match="handoff file"):
        server.delete_file("work/PROJECT_STATE", confirm=True, if_version="stale")
    with pytest.raises(ValueError, match="inside .git"):
        server.write_file(".git/config", "unsafe")


def test_git_and_env_files_cannot_be_read(sandbox: Path) -> None:
    git_dir = sandbox / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("private", encoding="utf-8")
    (sandbox / ".env").write_text("SECRET=private", encoding="utf-8")

    with pytest.raises(ValueError, match="Reading .git"):
        server.read_file(".git/config")
    with pytest.raises(ValueError, match="Reading .env"):
        server.read_file(".env")
    assert ".env" not in server.list_directory(".")
    assert "SECRET" not in server.search_files("SECRET", content=True)


def test_text_allowlist_rejects_unknown_and_executable_types(sandbox: Path) -> None:
    with pytest.raises(ValueError, match="allowed text file type"):
        server.write_file("payload.bin", "data")
    with pytest.raises(ValueError, match="allowed text file type"):
        server.write_file("payload.exe", "data")


def test_delete_requires_confirmation_and_current_version(sandbox: Path) -> None:
    target = sandbox / "remove.txt"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="confirm=true"):
        server.delete_file("remove.txt")

    version = server.read_file("remove.txt").split("[version: ")[-1].split("]")[0]
    target.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Version conflict"):
        server.delete_file("remove.txt", confirm=True, if_version=version)
    assert target.exists()


def test_state_update_requires_all_handoff_fields(sandbox: Path) -> None:
    handoff = server.handoff_read()
    version = next(
        line.removeprefix("[version: ").removesuffix("]")
        for line in handoff.splitlines()
        if line.startswith("[version: ")
    )
    with pytest.raises(ValueError, match="Every state field"):
        server.handoff_update_state(
            completed_work="done",
            verification="passed",
            unresolved_items="",
            next_action="next",
            if_version=version,
        )
