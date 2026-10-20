"""Install the LivePilot Remote Script into Ableton's User Library (Windows and macOS).

    python scripts/install.py              # copy the Remote Script and write .mcp.json
    python scripts/install.py --uninstall  # remove it

Then in Live: Settings > Link, Tempo & MIDI > Control Surface > LivePilot (Input/Output: None).
"""
import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "remote_script" / "LivePilot"


def user_library() -> Path:
    home = Path.home()
    if platform.system() == "Darwin":
        candidates = [home / "Music" / "Ableton" / "User Library"]
    else:
        candidates = [home / "Documents" / "Ableton" / "User Library",
                      home / "OneDrive" / "Documents" / "Ableton" / "User Library"]
    for c in candidates:
        if c.is_dir():
            return c
    sys.exit("Could not find Ableton's User Library. Pass --user-library <path> "
             "(see Live > Settings > Library > Location of User Library).")


def venv_python() -> Path:
    if platform.system() == "Windows":
        candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def write_mcp_config() -> Path:
    """Write .mcp.json for Claude Code with this machine's Python path (it differs per OS)."""
    path = ROOT / ".mcp.json"
    config = {"mcpServers": {"livepilot": {"command": venv_python().as_posix(), "args": ["-m", "livepilot"]}}}
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-library", type=Path, help="override the User Library location")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    target = (args.user_library or user_library()) / "Remote Scripts" / "LivePilot"
    if args.uninstall:
        if target.exists():
            shutil.rmtree(target)
            print(f"Removed {target}")
        else:
            print("Not installed.")
        return

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"Installed Remote Script to {target}")
    print(f"Wrote {write_mcp_config()} (server: {venv_python()})")
    print("\nNext steps:")
    print("  1. Restart Ableton Live (or open a new set).")
    print("  2. Settings > Link, Tempo & MIDI > Control Surface: choose 'LivePilot', Input/Output: None.")
    print("  3. Live's status bar should read 'LivePilot: listening on 127.0.0.1:9877'.")


if __name__ == "__main__":
    main()
