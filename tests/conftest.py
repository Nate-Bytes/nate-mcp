import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def create_directory_link():
    def create(link: Path, target: Path) -> bool:
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            if os.name != "nt":
                return False
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    return create
