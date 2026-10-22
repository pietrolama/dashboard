import subprocess
from pathlib import Path

import pytest

from project_os.db import Database
from project_os.registry import Registry


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def make_repo(path: Path, readme: str | None = "# Demo\n\nA demo project.\n") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    if readme is not None:
        (path / "README.md").write_text(readme)
    (path / "main.py").write_text("print('hi')\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(Database(tmp_path / "test.db"))
