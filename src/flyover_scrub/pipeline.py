"""File-in, file-out scrubbing shared by the command line and the desktop app.

``scrub_file`` reads any audio or video file, scrubs the audio and writes a new
file. Video and compressed audio go through ffmpeg: the one on PATH if there is
one, otherwise the copy that ships with the ``imageio-ffmpeg`` package (which is
what the packaged desktop app uses, so its users install nothing).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .enf import ENFResult, detect_enf
from .scrub import ScrubConfig, scrub

NATIVE = {".wav", ".flac", ".aiff", ".aif", ".ogg"}
VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".m2ts", ".wmv"}
LOSSY_AUDIO = {".mp3", ".m4a", ".aac", ".opus"}
AUDIO = NATIVE | LOSSY_AUDIO | {".wma", ".amr", ".caf", ".oga"}
MEDIA = VIDEO | AUDIO


class ScrubError(RuntimeError):
    """A problem worth showing to the person as-is (no traceback needed)."""


@dataclass
class FileResult:
    input: Path
    output: Path | None  # None for a check
    duration_s: float
    sample_rate: int
    channels: int
    ridges_found: int
    replaced_fraction: float
    enf_before: ENFResult
    enf_after: ENFResult | None = None
    has_video: bool = False
    metadata_stripped: bool = False


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _ffmpeg() -> str:
    exe = find_ffmpeg()
    if not exe:
        raise ScrubError("ffmpeg is required for this file type (brew install ffmpeg / apt install ffmpeg)")
    return exe


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    # CREATE_NO_WINDOW: a windowed Windows app must not flash a console per ffmpeg call.
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       stdin=subprocess.DEVNULL, creationflags=flags)
    if check and p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise ScrubError(f"ffmpeg failed: {tail}")
    return p


def _streams(path: Path) -> tuple[bool, bool]:
    """(has a real video stream, has an audio stream). Cover art does not count as video."""
    err = _run([_ffmpeg(), "-hide_banner", "-i", str(path)], check=False).stderr
    lines = [ln for ln in err.splitlines() if re.match(r"\s*Stream #\d+:\d+", ln)]
    if not lines:
        raise ScrubError("this doesn't look like an audio or video file")
    video = any(" Video: " in ln and "(attached pic)" not in ln for ln in lines)
    audio = any(" Audio: " in ln for ln in lines)
    return video, audio


def default_output(path: Path) -> Path:
    """Where the scrubbed copy goes: next to the original, never on top of anything."""
    ext = path.suffix.lower()
    if ext not in NATIVE | LOSSY_AUDIO | {".mp4", ".mov", ".m4v", ".mkv", ".webm"}:
        ext = ".mkv" if ext in VIDEO else ".wav"  # containers that take whatever is copied in
    out = path.with_name(f"{path.stem}-scrubbed{ext}")
    n = 2
    while out.exists():
        out = path.with_name(f"{path.stem}-scrubbed-{n}{ext}")
        n += 1
    return out


def scrub_file(
    input: Path,
    output: Path | None = None,
    cfg: ScrubConfig | None = None,
    *,
    keep_metadata: bool = False,
    check: bool = False,
    status: Callable[[str], None] = lambda _msg: None,
    analysed: Callable[[FileResult], None] = lambda _res: None,
) -> FileResult:
    """Scrub ``input`` into ``output`` (default: ``default_output(input)``).

    With ``check`` nothing is written. ``status`` gets short progress messages;
    ``analysed`` gets the result once detection is done, before anything is written.
    """
    input = Path(input)
    if not input.is_file():
        raise ScrubError(f"file not found: {input}")
    cfg = cfg or ScrubConfig()
    native_in = input.suffix.lower() in NATIVE

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status("Reading audio")
        video = False
        if native_in:
            src = input
        else:
            video, audio = _streams(input)
            if not audio:
                raise ScrubError("this file has no audio track, so there is nothing to scrub")
            src = tmp / "in.wav"
            _run([_ffmpeg(), "-v", "error", "-y", "-i", str(input), "-map", "0:a:0", "-c:a", "pcm_f32le", str(src)])
        try:
            x, sr = sf.read(src, always_2d=True)
        except sf.LibsndfileError as e:
            raise ScrubError(f"could not read the audio: {e}") from e
        if x.shape[0] < sr // 2:
            raise ScrubError("the audio is too short to scrub")

        status("Scrubbing")
        y, rep = scrub(x, sr, cfg)
        res = FileResult(
            input=input, output=None, duration_s=x.shape[0] / sr, sample_rate=sr, channels=x.shape[1],
            ridges_found=rep.ridges_found, replaced_fraction=rep.replaced_fraction,
            enf_before=detect_enf(x.mean(axis=1), sr), has_video=video,
        )
        analysed(res)
        if check:
            return res
        if res.enf_before.present:
            res.enf_after = detect_enf(y.mean(axis=1), sr)

        peak = float(np.abs(y).max())
        if peak > 1.0:
            y = y / peak
        out = Path(output) if output else default_output(input)
        if out.resolve() == input.resolve():
            raise ScrubError("the output would overwrite the original; choose a different name")
        ext = out.suffix.lower()
        status("Writing")
        if ext in NATIVE:
            sf.write(out, y, sr, subtype="FLOAT" if ext == ".wav" else None)
            res.output = out
            res.metadata_stripped = True  # written from samples alone; no tags are carried over
            return res

        wav = tmp / "scrubbed.wav"
        sf.write(wav, y, sr, subtype="FLOAT")
        # Encode into a fresh private folder beside the destination and move into place only
        # on success: a failed encode leaves no broken half-file, and nothing that was already
        # there (the destination, or any file with a similar name) can be overwritten or removed.
        try:
            partdir = Path(tempfile.mkdtemp(prefix=".flyover-scrub-", dir=out.parent))
        except OSError as e:
            raise ScrubError(f"cannot write to {out.parent}: {e.strerror or e}") from e
        part = partdir / out.name
        meta = [] if keep_metadata else ["-map_metadata", "-1", "-map_chapters", "-1"]
        if video:
            acodec = ["-c:a", "libopus", "-b:a", "160k"] if ext == ".webm" else ["-c:a", "aac", "-b:a", "256k"]
            cmd = [_ffmpeg(), "-v", "error", "-y", "-i", str(input), "-i", str(wav),
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *acodec, *meta, "-shortest", str(part)]
        else:
            rate = ["-b:a", "256k"] if ext in {".mp3", ".m4a", ".aac"} else []
            cmd = [_ffmpeg(), "-v", "error", "-y", "-i", str(wav), *rate, *meta, str(part)]
        try:
            _run(cmd)
            part.replace(out)
        finally:
            shutil.rmtree(partdir, ignore_errors=True)
        res.output = out
        res.metadata_stripped = not keep_metadata
        return res
