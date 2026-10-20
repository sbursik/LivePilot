"""Install the LivePilot Remote Script into Ableton's User Library (Windows and macOS).

    python scripts/install.py              # copy the Remote Script
    python scripts/install.py --uninstall  # remove it

Then in Live: Settings > Link, Tempo & MIDI > Control Surface > LivePilot (Input/Output: None).
"""
import argparse
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
    print("\nNext steps:")
    print("  1. Restart Ableton Live (or open a new set).")
    print("  2. Settings > Link, Tempo & MIDI > Control Surface: choose 'LivePilot', Input/Output: None.")
    print("  3. Live's status bar should read 'LivePilot: listening on 127.0.0.1:9877'.")


if __name__ == "__main__":
    main()
