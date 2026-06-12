"""Compatibility wrapper for the Linux release build.

Run:
    python build.py

This delegates to build_linux.py so each platform folder has a short, obvious
build command while keeping the Linux-specific implementation in one place.
"""

from build_linux import main


if __name__ == "__main__":
    main()
