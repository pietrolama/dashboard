"""Read-only Git inspection through the git CLI.

Arguments are always passed as a list (never through a shell), and
GIT_OPTIONAL_LOCKS=0 keeps `git status` from touching the index lock of a
repository the user may be working in.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIT_TIMEOUT_S = 10
_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class GitError(RuntimeError):
    pass


@dataclass
class GitInfo:
    branch: str | None
    head: str | None
    head_date: str | None
    head_subject: str | None
    dirty_files: int
    untracked_files: int
    remote_url: str | None
    dirty_paths: list[str] = field(default_factory=list, repr=False)

    def to_payload(self) -> dict:
        # dirty_paths is an implementation detail used to date uncommitted
        # work; it is not part of the stored observation.
        data = asdict(self)
        data.pop("dirty_paths")
        return data


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout if proc.returncode == 0 else ""


def is_git_repo(path: Path) -> bool:
    # .git is a directory for normal clones and a file for worktrees/submodules.
    return (path / ".git").exists()


def inspect(repo: Path) -> GitInfo:
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or None

    # Fails on a repository without commits: that is a valid state, not an error.
    log = _git(repo, "log", "-1", "--format=%H%x1f%cI%x1f%s", check=False).strip()
    head = head_date = head_subject = None
    if log:
        head, head_date, head_subject = log.split("\x1f", 2)

    status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    dirty, untracked, paths = 0, 0, []
    entries = iter(status.split("\0"))
    for entry in entries:
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            next(entries, None)  # the rename source follows as its own entry
        if code == "??":
            untracked += 1
        else:
            dirty += 1
        paths.append(path)

    remote = _git(repo, "remote", "get-url", "origin", check=False).strip() or None

    return GitInfo(
        branch=branch,
        head=head,
        head_date=head_date,
        head_subject=head_subject,
        dirty_files=dirty,
        untracked_files=untracked,
        remote_url=_strip_credentials(remote),
        dirty_paths=paths,
    )


def _strip_credentials(url: str | None) -> str | None:
    """Drop `user:token@` from https remotes so they never reach the database."""
    if not url or "://" not in url or "@" not in url.split("://", 1)[1].split("/", 1)[0]:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('@', 1)[1]}"


def recent_commits(repo: Path, limit: int = 10) -> list[dict]:
    """The last `limit` commits on HEAD, newest first. Empty for a repo without commits."""
    out = _git(repo, "log", f"-{limit}", "--format=%H%x1f%cI%x1f%s", check=False)
    commits = []
    for line in out.splitlines():
        sha, date, subject = line.split("\x1f", 2)
        commits.append({"commit": sha, "date": date, "subject": subject})
    return commits
