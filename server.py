from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pc-files")

ROOT = Path(r"D:\TestThyChambers").resolve() 

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
    return "\n".join(f"  {item.name}" for item in directory.iterdir())

if __name__ == "__main__":
    mcp.run(transport="stdio")