"""Filesystem discovery: find projects under a root and observe them.

This module never writes to the database and never modifies the scanned
directories. It reads only directory listings, file metadata, Git state and
README files — never `.env`, keys or other configuration files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gitinfo

# Directories that are never projects and are expensive to walk.
SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", "env", "dist", "build", "target",
    "site-packages", "vendor", "Pods", "DerivedData",
}

# Files whose presence at the top level marks a directory as a project.
MARKER_FILES = {
    "readme", "readme.md", "readme.rst", "readme.txt", "package.json",
    "pyproject.toml", "requirements.txt", "setup.py", "cargo.toml", "go.mod",
    "cmakelists.txt", "makefile", "index.html", "dockerfile", "claude.md",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "pom.xml", "composer.json", "gemfile", "pubspec.yaml", "project.godot",
}

# Extension → stack label. Also used as project markers.
STACK_BY_EXT = {
    ".py": "python", ".ipynb": "jupyter", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".html": "web", ".rs": "rust", ".go": "go", ".c": "c", ".cpp": "c++",
    ".h": "c", ".java": "java", ".kt": "kotlin", ".swift": "swift",
    ".pd": "puredata", ".scd": "supercollider", ".glsl": "glsl", ".frag": "glsl",
    ".blend": "blender", ".sh": "shell", ".lua": "lua", ".rb": "ruby",
}
STACK_BY_FILE = {
    "package.json": "node", "pyproject.toml": "python", "requirements.txt": "python",
    "cargo.toml": "rust", "go.mod": "go", "dockerfile": "docker",
    "build.gradle": "gradle", "build.gradle.kts": "gradle", "pom.xml": "maven",
    "pubspec.yaml": "flutter", "project.godot": "godot",
}

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")
README_MAX_BYTES = 64 * 1024
README_EXCERPT_CHARS = 4000
MTIME_WALK_LIMIT = 5000


@dataclass
class Candidate:
    path: Path
    vcs: str  # 'git' | 'none'


@dataclass
class Observation:
    """Everything the scanner saw for one candidate, grouped by evidence source."""

    path: Path
    vcs: str
    git: dict | None
    readme: dict | None
    filesystem: dict
    last_activity_at: str | None


def _skip(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


def _subdirs(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            return sorted(
                Path(e.path) for e in it
                if e.is_dir(follow_symlinks=False) and not _skip(e.name)
            )
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return []


def _looks_like_project(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    name = e.name.lower()
                    if name in MARKER_FILES or Path(name).suffix in STACK_BY_EXT:
                        return True
                elif e.is_dir(follow_symlinks=False) and e.name == "src":
                    return True
    except (PermissionError, FileNotFoundError):
        pass
    return False


def find_candidates(root: Path, max_depth: int = 3) -> list[Candidate]:
    """Return the projects found under `root`.

    Git repositories are found at any depth up to `max_depth` and are not
    descended into. A direct child of the root that is not a Git repository
    still counts as a project when it contains project files, unless it only
    groups other Git repositories.

    Raises OSError if the root itself cannot be read.
    """
    root = root.expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"root is not a directory: {root}")
    os.listdir(root)  # surface PermissionError for the root itself

    found: list[Candidate] = []

    def walk(directory: Path, depth: int) -> bool:
        """Collect repos below `directory`; return True if any were found."""
        hit = False
        for child in _subdirs(directory):
            if gitinfo.is_git_repo(child):
                found.append(Candidate(child, "git"))
                hit = True
            elif depth < max_depth and walk(child, depth + 1):
                hit = True
            elif depth == 1 and _looks_like_project(child):
                found.append(Candidate(child, "none"))
        return hit

    if gitinfo.is_git_repo(root):
        return [Candidate(root, "git")]
    walk(root, 1)
    return found


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _detect_stack(path: Path) -> list[str]:
    stack: set[str] = set()
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.is_file(follow_symlinks=False):
                    continue
                name = e.name.lower()
                if name in STACK_BY_FILE:
                    stack.add(STACK_BY_FILE[name])
                ext = Path(name).suffix
                if ext in STACK_BY_EXT:
                    stack.add(STACK_BY_EXT[ext])
    except OSError:
        pass
    return sorted(stack)


def _newest_mtime(path: Path) -> tuple[float | None, int, bool]:
    """Newest file mtime under `path`, skipping vendored/hidden dirs.

    Returns (mtime, files_seen, truncated). The walk stops after
    MTIME_WALK_LIMIT files so a huge directory cannot stall a scan.
    """
    newest: float | None = None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        for name in filenames:
            try:
                mtime = os.stat(os.path.join(dirpath, name), follow_symlinks=False).st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
            seen += 1
            if seen >= MTIME_WALK_LIMIT:
                return newest, seen, True
    return newest, seen, False


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_NOISE = re.compile(r"^\s*(!\[|\[!\[|<|\||```|---|===|\[.*\]:)")


def read_readme(path: Path) -> dict | None:
    for entry in _listdir_lower(path):
        if entry.lower() in README_NAMES:
            file = path / entry
            break
    else:
        return None
    try:
        raw = file.open("rb").read(README_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None

    title = None
    paragraph: list[str] = []
    for line in raw.splitlines():
        if title is None and (m := _HEADING.match(line)):
            title = m.group(1).strip()
            continue
        if _HEADING.match(line) or _NOISE.match(line):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        if _LINK_ONLY.match(line):  # e.g. "*[Read in English](README.en.md)*"
            continue
        paragraph.append(line.strip())

    summary = _plain_text(" ".join(paragraph))
    return {
        "file": file.name,
        "title": title,
        "summary": summary[:500] or None,
        "excerpt": raw[:README_EXCERPT_CHARS],
    }


_LINK_ONLY = re.compile(r"^\W*\[[^\]]*\]\([^)]*\)\W*$")


def _plain_text(md: str) -> str:
    """Reduce inline Markdown to readable text for one-line summaries."""
    md = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", md)  # links and images
    md = re.sub(r"<[^>]+>", "", md)  # inline HTML
    md = re.sub(r"(\*\*|__|\*|`|~~)", "", md)
    return re.sub(r"\s+", " ", md).strip()


def _listdir_lower(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def observe(candidate: Candidate) -> Observation:
    """Collect the current state of one project. Raises on Git failures."""
    path = candidate.path
    git_payload = None
    activity: list[str] = []

    if candidate.vcs == "git":
        info = gitinfo.inspect(path)
        git_payload = info.to_payload()
        if info.head_date:
            activity.append(_normalise_iso(info.head_date))
        # Uncommitted edits count as activity too: date them by mtime.
        dirty_mtimes = []
        for rel in info.dirty_paths[:200]:
            try:
                dirty_mtimes.append(os.stat(path / rel, follow_symlinks=False).st_mtime)
            except OSError:
                pass
        if dirty_mtimes:
            activity.append(_iso_from_ts(max(dirty_mtimes)))
        filesystem = {"stack": _detect_stack(path)}
    else:
        mtime, files_seen, truncated = _newest_mtime(path)
        if mtime is not None:
            activity.append(_iso_from_ts(mtime))
        filesystem = {
            "stack": _detect_stack(path),
            "files_seen": files_seen,
            "walk_truncated": truncated,
        }

    return Observation(
        path=path,
        vcs=candidate.vcs,
        git=git_payload,
        readme=read_readme(path),
        filesystem=filesystem,
        last_activity_at=max(activity) if activity else None,
    )


def _normalise_iso(value: str) -> str:
    """Convert any ISO-8601 timestamp to UTC so values compare as strings."""
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
