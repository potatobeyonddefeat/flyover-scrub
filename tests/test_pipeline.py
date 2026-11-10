import numpy as np
import pytest
import soundfile as sf

from flyover_scrub.pipeline import ScrubError, default_output, find_ffmpeg, scrub_file

SR = 8000


def _wav(path, seconds=8.0):
    rng = np.random.default_rng(0)
    t = np.arange(int(seconds * SR)) / SR
    x = 0.05 * rng.standard_normal(len(t)) + 0.02 * np.sin(2 * np.pi * 180 * t)
    sf.write(path, x, SR)
    return x


def test_default_output_sits_beside_the_original_and_never_collides(tmp_path):
    src = tmp_path / "clip.MOV"
    assert default_output(src) == tmp_path / "clip-scrubbed.mov"
    (tmp_path / "clip-scrubbed.mov").touch()
    assert default_output(src) == tmp_path / "clip-scrubbed-2.mov"
    assert default_output(tmp_path / "a.wma").suffix == ".wav"  # no encoder to rely on
    assert default_output(tmp_path / "a.avi").suffix == ".mkv"


def test_scrub_file_writes_a_copy_and_leaves_the_original(tmp_path):
    src = tmp_path / "a.wav"
    _wav(src)
    before = src.read_bytes()
    seen = []
    res = scrub_file(src, status=seen.append)
    assert res.output == tmp_path / "a-scrubbed.wav" and res.output.exists()
    assert src.read_bytes() == before
    assert res.ridges_found >= 1 and res.metadata_stripped
    assert seen == ["Reading audio", "Scrubbing", "Writing"]
    y, sr = sf.read(res.output)
    assert sr == SR and len(y) == int(8.0 * SR)


def test_check_writes_nothing_and_bad_input_is_a_readable_error(tmp_path):
    src = tmp_path / "a.wav"
    _wav(src)
    assert scrub_file(src, check=True).output is None
    assert list(tmp_path.iterdir()) == [src]
    with pytest.raises(ScrubError, match="overwrite"):
        scrub_file(src, src)
    with pytest.raises(ScrubError, match="not found"):
        scrub_file(tmp_path / "missing.wav")


@pytest.mark.skipif(find_ffmpeg() is None, reason="needs ffmpeg")
def test_video_keeps_its_picture_and_loses_its_gps_tag(tmp_path):
    import subprocess

    wav, mov = tmp_path / "a.wav", tmp_path / "v.mov"
    _wav(wav, 4.0)
    made = subprocess.run(
        [find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=d=4:s=160x120:r=10", "-i", str(wav),
         "-c:v", "mpeg4", "-c:a", "aac", "-metadata", "location=+42.36-083.13/", "-shortest", str(mov)],
        capture_output=True,
    )
    if made.returncode:
        pytest.skip("this ffmpeg cannot make the test clip")
    res = scrub_file(mov)
    assert res.has_video and res.metadata_stripped and res.output.suffix == ".mov"
    info = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(res.output)], capture_output=True, text=True).stderr
    assert " Video: " in info and " Audio: " in info and "42.36" not in info

    # A failed encode must not destroy a file already at the destination.
    keep = tmp_path / "keep.xyz"
    keep.write_text("precious")
    with pytest.raises(ScrubError):
        scrub_file(mov, keep)
    assert keep.read_text() == "precious" and not list(tmp_path.glob("*.partial*"))

    notmedia = tmp_path / "notes.mp4"
    notmedia.write_text("not a recording")
    with pytest.raises(ScrubError):
        scrub_file(notmedia)
