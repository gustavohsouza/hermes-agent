"""Install the Watchdog bridge into a fixed Hermes profile path."""
from __future__ import annotations

import argparse
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

BRIDGE_FILES = ("watchdog_boundary.py", "watchdog_kanban.py", "watchdog_runtime.py")
LAUNCHER_NAME = "watchdog-kanban-intake"


def _backup_existing(profile: Path, path: Path, timestamp: str) -> Path | None:
    if not path.exists():
        return None
    relative = path.relative_to(profile)
    backup = profile / "backups" / "watchdog-bridge" / timestamp / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    return backup


def install(profile: Path, source: Path, *, timestamp: str | None = None) -> dict[str, Path]:
    """Install source and launcher; back up any replaced profile files."""
    profile = profile.expanduser().resolve()
    source = source.expanduser().resolve()
    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    deployment = profile / "lib" / "watchdog-bridge"
    launcher = profile / "bin" / LAUNCHER_NAME

    for target in (launcher, *(deployment / name for name in BRIDGE_FILES)):
        _backup_existing(profile, target, stamp)

    deployment.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    for name in BRIDGE_FILES:
        source_file = source / name
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        shutil.copy2(source_file, deployment / name)

    launcher.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"PYTHONPATH={str(deployment)!r}${{PYTHONPATH:+:$PYTHONPATH}} "
        f"exec {str(Path(sys.executable).resolve())!r} -m watchdog_runtime --json-stdin\n",
        encoding="utf-8",
    )
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return {"deployment": deployment, "launcher": launcher}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    result = install(args.profile, args.source)
    print(result["launcher"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
