from pathlib import Path
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver import Image

mcp = MCPServer("pc-files")
ROOT = Path(r"D:\TestThyChambers").resolve() 

MAX_FILE_SIZE = 1024 * 1024
MAX_IMAGE_SIZE = 5 * 1024 * 1024

BLOCKED_EXTENSIONS = {".exe"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

def safe_path(p: str) -> Path:
    """Resolve the given path and ensure it is within the ROOT directory."""
    resolved_path = (ROOT / p).resolve()
    if not resolved_path.is_relative_to(ROOT):
        raise ValueError("Access to this path is not allowed.")
    return resolved_path

@mcp.tool()
def list_directory(path: str = ".") -> str:
    """List files and folders inside the given directory."""
    directory = safe_path(path)
    if not directory.is_dir():
        return f"{path} is not a directory."
    lines = []
    for item in directory.iterdir():
        if item.is_dir():
            lines.append(f"{item.name}/")
        else:
            lines.append(item.name)
    if lines == []:
        return f"{path} is empty."
    else:
        return "\n".join(lines)


@mcp.tool()
def read_file(path: str = ".") -> str:
    """Read the contents of a text file. Does not work on binary files such as images, PDFs, or executables."""
    file_path = safe_path(path)
    if not file_path.is_file():
        return f"{path} is not a file."
    size = file_path.stat().st_size
    if size > MAX_FILE_SIZE:
        return f"{path} is too large ({size} bytes). Limit is {MAX_FILE_SIZE} bytes."
    if file_path.suffix in BLOCKED_EXTENSIONS:
        return f"{path} has a blocked extension."
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

@mcp.tool()
def read_image(path: str) -> Image:
    """View an image file (png, jpg, gif, webp)."""
    file_path = safe_path(path)

    if not file_path.is_file():
        raise ValueError(f"{path} is not a file.")

    if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"{path} is not a supported image type.")

    size = file_path.stat().st_size
    if size > MAX_IMAGE_SIZE:
        raise ValueError(f"{path} is too large ({size} bytes). Limit is {MAX_IMAGE_SIZE} bytes.")

    return Image(path=file_path)

if __name__ == "__main__":
    mcp.run(transport="stdio")