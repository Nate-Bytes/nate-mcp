from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pc-files")

ROOT = Path(r"C:\Users\YOU\Documents\shared").resolve() 

def safe_path(p: str) -> Path:
    # TODO
    ...

@mcp.tool()
def list_directory(path: str = ".") -> str:
    """List files and folders inside the given directory."""
    # TODO
    ...

if __name__ == "__main__":
    mcp.run(transport="stdio")