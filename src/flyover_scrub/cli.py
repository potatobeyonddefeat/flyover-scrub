"""flyover-scrub: remove aircraft Doppler signatures from audio and video files.

    flyover-scrub input.mov -o output.mov      # video: audio scrubbed, video stream copied
    flyover-scrub input.wav -o output.wav
    flyover-scrub --check input.mp4            # report long tonal ridges, write nothing

Video and compressed audio go through ffmpeg (must be on PATH). Metadata is
stripped from video outputs by default, since GPS tags are a far easier leak.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from .pipeline import NATIVE, FileResult, ScrubError, _ffmpeg, _run, scrub_file
from .scrub import ScrubConfig


def _read(path: Path, tmp: Path) -> tuple[np.ndarray, int]:
    """Decode any file's first audio track. Used by the audit and the research scripts."""
    if path.suffix.lower() in NATIVE:
        return sf.read(path, always_2d=True)
    wav = tmp / "in.wav"
    try:
        _run([_ffmpeg(), "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_f32le", str(wav)])
    except ScrubError as e:
        sys.exit(str(e))
    return sf.read(wav, always_2d=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="flyover-scrub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--check", action="store_true", help="only report detected tonal ridges")
    ap.add_argument("--band", type=float, nargs=2, default=(20.0, 2000.0), metavar=("LO", "HI"))
    ap.add_argument("--keep-hum", action="store_true", help="keep steady tones (mains hum, appliances)")
    ap.add_argument("--keep-metadata", action="store_true", help="do not strip container metadata (GPS, dates)")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)

    if not a.check and not a.output:
        ap.error("-o/--output is required unless --check")
    cfg = ScrubConfig(band=tuple(a.band), keep_stationary_tones=a.keep_hum, seed=a.seed)

    def analysed(r: FileResult) -> None:
        print(
            f"{a.input.name}: {r.duration_s:.1f}s @ {r.sample_rate} Hz, {r.channels} ch | "
            f"tonal ridges: {r.ridges_found} | in-band bins replaced: {r.replaced_fraction:.0%}"
        )
        print(f"  {r.enf_before.describe()}")

    try:
        r = scrub_file(a.input, a.output, cfg, keep_metadata=a.keep_metadata, check=a.check, analysed=analysed)
    except ScrubError as e:
        sys.exit(str(e))
    if a.check:
        if r.ridges_found:
            print("  long narrowband tones present (aircraft, drones, traffic, HVAC or hum).")
        print("  note: faint lines below the noise floor are not detectable per-frame but are still")
        print("  recoverable by a matched filter, so scrub anyway if location matters.")
        return
    if r.enf_after is not None:
        print(f"  after scrubbing: {r.enf_after.describe()}"
              + (" (--keep-hum keeps it)" if a.keep_hum and r.enf_after.present else ""))
    print(f"wrote {r.output}" + (" (metadata stripped)" if r.metadata_stripped else ""))


if __name__ == "__main__":
    main()
