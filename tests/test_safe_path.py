from pathlib import Path

import pytest

import server


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "TestThyChambers"
    root.mkdir()
    monkeypatch.setattr(server, "ROOT", root.resolve())
    return root


def test_dot_resolves_to_sandbox_root(sandbox: Path) -> None:
    assert server.safe_path(".") == sandbox


def test_normal_subfolder_is_allowed(sandbox: Path) -> None:
    subfolder = sandbox / "documents"
    subfolder.mkdir()
    assert server.safe_path("documents") == subfolder


@pytest.mark.parametrize("path", ["..", r"..\.."])
def test_parent_traversal_is_rejected(sandbox: Path, path: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path(path)


def test_absolute_windows_path_is_rejected(sandbox: Path) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path(r"C:\Windows")


def test_sibling_sharing_root_prefix_is_rejected(sandbox: Path) -> None:
    sibling = Path(f"{sandbox}_evil")
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path(str(sibling))


def test_mixed_case_path_is_resolved_inside_sandbox(sandbox: Path) -> None:
    subfolder = sandbox / "MixedCase"
    subfolder.mkdir()
    result = server.safe_path(str(sandbox).swapcase() + r"\mixedcase")
    assert result.is_relative_to(sandbox)


@pytest.mark.parametrize("path", [".. ", r"..\.. ", "folder. ", "folder..."])
def test_trailing_dots_and_spaces_do_not_escape(sandbox: Path, path: str) -> None:
    try:
        result = server.safe_path(path)
    except ValueError:
        return
    assert result.is_relative_to(sandbox)


def test_symlink_to_outside_root_is_rejected(sandbox: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sandbox / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Creating a directory symlink is unavailable: {exc}")
    with pytest.raises(ValueError, match="not allowed"):
        server.safe_path("outside-link")
