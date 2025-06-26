from pathlib import Path
import subprocess

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_version() -> str:
    """Return the latest git tag without the leading 'v'."""
    try:
        result = subprocess.run(
            ["git", "-C", str(BASE_DIR), "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip().lstrip("v")
    except Exception:
        return "unknown"


__version__ = _get_version()
