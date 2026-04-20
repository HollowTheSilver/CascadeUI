"""Support ``python -m cascadeui`` for version and environment info.

Prints a diagnostic block suitable for pasting into bug reports.
"""

import platform
import sys


def main():
    import importlib.metadata

    try:
        cascadeui_version = importlib.metadata.version("pycascadeui")
    except importlib.metadata.PackageNotFoundError:
        cascadeui_version = "unknown (not installed via pip)"

    try:
        discord_version = importlib.metadata.version("discord.py")
    except importlib.metadata.PackageNotFoundError:
        discord_version = "not installed"

    # Optional backends
    backends = []
    try:
        import aiosqlite

        backends.append(f"aiosqlite {aiosqlite.__version__}")
    except ImportError:
        pass
    try:
        import redis

        backends.append(f"redis {redis.__version__}")
    except ImportError:
        pass

    print(f"- CascadeUI v{cascadeui_version}")
    print(f"- discord.py v{discord_version}")
    print(f"- Python {sys.version}")
    print(f"- OS: {platform.system()} {platform.release()} ({platform.machine()})")
    if backends:
        print(f"- Backends: {', '.join(backends)}")


if __name__ == "__main__":
    main()
