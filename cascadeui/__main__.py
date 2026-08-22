"""Support ``python -m cascadeui`` for version and environment info.

Prints a diagnostic block suitable for pasting into bug reports.
"""

import platform
import sys


def main():
    import importlib.metadata

    import cascadeui

    # The imported code, not the installed distribution's metadata. An
    # editable install keeps serving whatever version it recorded at
    # install time, so metadata can trail the working tree by releases --
    # and this block is a required field on the bug report template,
    # where a stale number arrives stated as fact.
    cascadeui_version = cascadeui.__version__
    try:
        installed = importlib.metadata.version("pycascadeui")
    except importlib.metadata.PackageNotFoundError:
        installed = None

    try:
        discord_version = importlib.metadata.version("discord.py")
    except importlib.metadata.PackageNotFoundError:
        discord_version = "not installed"

    # The drivers behind the two optional backends the library ships. A
    # driver named here that no backend uses reads as a backend that
    # exists, and one left out hides the only backend with a network
    # surface from every report a Postgres user files.
    backends = []
    try:
        import aiosqlite

        backends.append(f"aiosqlite {aiosqlite.__version__}")
    except ImportError:
        pass
    try:
        import asyncpg

        backends.append(f"asyncpg {asyncpg.__version__}")
    except ImportError:
        pass

    if installed is not None and installed != cascadeui_version:
        print(f"- CascadeUI v{cascadeui_version} (installed distribution reports {installed})")
    else:
        print(f"- CascadeUI v{cascadeui_version}")
    print(f"- discord.py v{discord_version}")
    print(f"- Python {sys.version}")
    print(f"- OS: {platform.system()} {platform.release()} ({platform.machine()})")
    if backends:
        print(f"- Backends: {', '.join(backends)}")


if __name__ == "__main__":
    main()
