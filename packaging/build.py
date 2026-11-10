"""Build the standalone desktop app with PyInstaller.

    uv sync --extra gui && uv pip install pyinstaller
    uv run python packaging/build.py          # -> dist/flyover-scrub(.app)

The result needs no Python and no ffmpeg on the user's machine: imageio-ffmpeg's
ffmpeg binary is bundled alongside.
"""

import sys
from pathlib import Path

import PyInstaller.__main__

here = Path(__file__).parent
PyInstaller.__main__.run([
    str(here / "app.py"),
    "--name", "flyover-scrub",
    "--windowed",  # no console window; on macOS this makes a .app bundle
    "--noconfirm",
    "--clean",
    "--collect-binaries", "imageio_ffmpeg",
    "--collect-binaries", "soundfile",
    "--collect-data", "_soundfile_data",
    "--exclude-module", "tkinter",
    *(["--osx-bundle-identifier", "dev.flyover-scrub.app"] if sys.platform == "darwin" else []),
])
