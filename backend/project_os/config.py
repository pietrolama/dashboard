"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_db() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "project-os" / "project_os.db"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    scan_interval_s: int
    frontend_dist: Path
    exports_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = Path(os.environ.get("PROJECT_OS_DB", _default_db())).expanduser()
        return cls(
            db_path=db_path,
            # 0 disables the background scheduler.
            scan_interval_s=int(os.environ.get("PROJECT_OS_SCAN_INTERVAL", 15 * 60)),
            frontend_dist=Path(os.environ.get("PROJECT_OS_FRONTEND", REPO_ROOT / "frontend" / "dist")),
            exports_dir=Path(os.environ.get("PROJECT_OS_EXPORTS", db_path.parent / "exports")).expanduser(),
        )
