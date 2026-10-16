"""Environment loading and limits for the MCP file server."""

import os
from pathlib import Path


def load_dotenv() -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

ROOT = Path(os.environ.get("MCP_ROOT", Path(__file__).with_name("workspace"))).resolve()

MAX_TEXT_FILE = 10 * 1024 * 1024
DEFAULT_READ_LINES = 200
MAX_READ_CHARS = 50_000
MAX_WRITE_SIZE = 1 * 1024 * 1024
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_PDF_SIZE = 20 * 1024 * 1024
MAX_PDF_CHARS = 100_000
MAX_SEARCH_FILE = 1 * 1024 * 1024
MAX_LIST_ENTRIES = 500
MAX_GIT_OUTPUT = 50_000
MAX_HANDOFF_VIEW = 20_000
MAX_HANDOFF_ENTRY = 10_000

TEXT_EXTENSIONS = {
    ".c", ".cc", ".cfg", ".cpp", ".cs", ".css", ".csv", ".dart", ".go",
    ".h", ".hpp", ".htm", ".html", ".ini", ".ipynb", ".java", ".js",
    ".json", ".jsx", ".kt", ".log", ".md", ".php", ".py", ".pyi", ".rb",
    ".rs", ".rst", ".sql", ".swift", ".toml", ".ts", ".tsx", ".txt",
    ".xml", ".yaml", ".yml",
}
TEXT_FILENAMES = {
    ".dockerignore", ".editorconfig", ".gitignore", "dockerfile", "license",
    "makefile", "notice",
}
BLOCKED_EXTENSIONS = {
    ".bat", ".cmd", ".com", ".dll", ".exe", ".lnk", ".msi", ".ps1", ".scr", ".vbs",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

HANDOFF_TEMPLATES = {
    "PROJECT_STATE": (
        "Completed work:\n\n"
        "Verification:\n\n"
        "Unresolved items:\n\n"
        "Next action:\n"
    ),
    "PLAN_LOG": "Plan and work log:\n",
    "CHECKPOINTS": "Verified checkpoints:\n",
    "DECISIONS": "Decisions and rationale:\n",
}
