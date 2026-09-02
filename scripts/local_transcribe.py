#!/usr/bin/env python3
"""
local_transcribe.py — turn a video into a vtt locally, when no subtitle track
can be downloaded.

No APIs are involved: yt-dlp pulls the audio, faster-whisper (CTranslate2, CPU
int8) does the ASR, and the WebVTT is written here by hand. Model weights are
fetched from Hugging Face on first use into HF_HOME; after that the Actions
cache carries them between runs.

No ffmpeg binary is needed either. faster-whisper decodes audio through PyAV,
which bundles the FFmpeg libraries in its own wheel, so the downloaded stream
is handed to the model as-is instead of being transcoded to wav first. An
earlier version ran yt-dlp's --extract-audio postprocessor, which does shell
out to ffmpeg -- on a runner without it every item failed with
"ASR-SKIP (ffmpeg not found)".

Output filenames deliberately match what yt-dlp produces with
`-o {id}.%(language)s.%(ext)s`:

    {item_id}.{orig_lang}.{detected_lang}.vtt

The consumer side (summarize_feed.pick_subtitle) only ever looks at filenames,
so a file produced here is indistinguishable from a downloaded auto-caption
track as far as it is concerned, and file_rank() applies unchanged. ASR output
has no punctuation, which is the same handicap rank 3 (auto, original language)
already carries — no extra tier is needed for it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# A 15-minute video with the base model runs roughly 3–5 minutes on GitHub's
# 2-core runners. `small` is noticeably better but close to twice as slow,
# which long videos turn into a blown job budget.
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "base")
DEFAULT_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

AUDIO_TIMEOUT = 300
AUDIO_PLAYER_CLIENTS = "tv,web_safari,default"
AUDIO_FORMAT_SELECTOR = "bestaudio[abr<=64]/bestaudio/bestaudio*/best"
# Anything longer than this is not transcribed: the runner's 6-hour ceiling
# has to be left for the other items.
MAX_DURATION = 3600

_model_cache: dict[tuple[str, str], object] = {}


class TranscribeUnavailable(RuntimeError):
    """faster-whisper or ffmpeg is missing — an environment problem, not a
    problem with this particular video."""


def available() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "faster-whisper not installed"
    return True, ""


def _load_model(model_name: str, compute_type: str):
    key = (model_name, compute_type)
    if key not in _model_cache:
        from faster_whisper import WhisperModel
        _model_cache[key] = WhisperModel(
            model_name, device="cpu", compute_type=compute_type,
            cpu_threads=os.cpu_count() or 2,
        )
    return _model_cache[key]


def format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def segments_to_vtt(segments) -> str:
    """`segments` is any iterable of (start, end, text). Returns the full
    WebVTT text."""
    lines = ["WEBVTT", ""]
    prev_end = 0.0
    count = 0
    for start, end, text in segments:
        text = " ".join(str(text).split())
        if not text:
            continue
        start = max(float(start), 0.0)
        end = float(end)
        # Whisper occasionally emits intervals where end <= start, or ones that
        # overlap the previous cue. Neither players nor the downstream chunker
        # like those, so clamp them here.
        start = max(start, prev_end)
        if end <= start:
            end = start + 0.5
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        lines.append(text)
        lines.append("")
        prev_end = end
        count += 1
    if not count:
        return ""
    return "\n".join(lines)


def _audio_ytdlp_args(common_args: list[str]) -> list[str]:
    """Rewrite the shared yt-dlp args for a media download.

    Two things from the subtitle path actively hurt here: the
    `youtube:player_client=web` extractor arg (see AUDIO_PLAYER_CLIENTS) and
    `--ignore-no-formats`, which turns "this video has no formats" into a
    silent success and leaves nothing on disk to explain the failure.
    """
    out: list[str] = []
    skip_next = False
    for arg in common_args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--ignore-no-formats":
            continue
        if arg == "--extractor-args":
            skip_next = True
            continue
        out.append(arg)
    out += ["--extractor-args", f"youtube:player_client={AUDIO_PLAYER_CLIENTS}"]
    return out


def download_audio(url: str, cookies_path: Path, work_dir: Path,
                   common_args: list[str], timeout: float = AUDIO_TIMEOUT,
                   runner=None):
    """Grab the smallest audio-only stream, as-is.

    No transcoding: PyAV (inside faster-whisper) decodes m4a/webm/opus fine and
    resamples to 16 kHz itself. Returns (audio_path, err); audio_path is None
    on failure. When `runner` is given (download_sub.run_ytdlp), timeouts get
    the same process-group cleanup as the subtitle downloads.
    """
    out_tpl = str(work_dir / "audio.%(ext)s")
    cmd = [
        "yt-dlp",
        "--cookies", str(cookies_path),
        *_audio_ytdlp_args(common_args),
        "-f", AUDIO_FORMAT_SELECTOR,
        "-o", out_tpl,
        url,
    ]
    if runner is None:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc, output, timed_out = proc.returncode, proc.stdout + proc.stderr, False
    else:
        rc, out, err, timed_out = runner(cmd, timeout)
        output = out + err
    if timed_out:
        return None, f"audio download timed out after {timeout}s"
    if rc != 0:
        return None, output.strip()[-2000:]
    files = sorted(p for p in work_dir.glob("audio.*") if p.suffix != ".part")
    if not files:
        return None, "no audio file produced\n" + output.strip()[-1000:]
    return files[0], ""


def transcribe_to_vtt(
    url: str,
    cookies_path: Path,
    out_dir: Path,
    item_id: str,
    orig_lang: str | None,
    common_args: list[str],
    model_name: str = DEFAULT_MODEL,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    audio_timeout: float = AUDIO_TIMEOUT,
    max_duration: float = MAX_DURATION,
    runner=None,
):
    """The whole pipeline. Returns (vtt_path, detected_lang); raises
    RuntimeError on failure.

    On the duration check below: whisper runs in this process, not a
    subprocess, so there is no pid to kill and no way to enforce a wall-clock
    timeout on it. The length of the audio is used as a proxy for how long
    decoding will take instead. `model.transcribe()` returns a lazy generator,
    so `info.duration` is available before any segment is decoded — but not
    for free: feature extraction, language detection and the VAD pass have all
    already run by then. What the check saves is the decode, which is the
    expensive part. It is also only a proxy: silence-heavy audio or whisper's
    repetition loops can blow past the expected realtime factor, and nothing
    here can stop that once decoding starts. The per-run cap in download_sub
    (--max-transcribe) is the real backstop.
    """
    ok, why = available()
    if not ok:
        raise TranscribeUnavailable(why)

    with tempfile.TemporaryDirectory(prefix=f"asr-{item_id}-") as d:
        work_dir = Path(d)
        audio, err = download_audio(url, cookies_path, work_dir, common_args,
                                    audio_timeout, runner)
        if audio is None:
            raise RuntimeError(f"audio download failed: {err}")

        model = _load_model(model_name, compute_type)
        segments, info = model.transcribe(
            str(audio),
            beam_size=1,
            vad_filter=True,
            # Cuts down on whisper's repetition degeneration, where one phrase
            # is decoded over and over. Reduces the odds, does not remove it.
            condition_on_previous_text=False,
        )
        duration = getattr(info, "duration", 0.0) or 0.0
        if max_duration and duration > max_duration:
            raise RuntimeError(
                f"too long for local ASR: {duration / 60:.0f} min "
                f"> {max_duration / 60:.0f} min")

        detected = (getattr(info, "language", None) or "und").strip() or "und"
        vtt = segments_to_vtt((s.start, s.end, s.text) for s in segments)

    if not vtt:
        raise RuntimeError("transcription produced no speech segments")

    # The filename has to look exactly like yt-dlp's: {id}.{orig}.{sub}.vtt
    orig_tag = (orig_lang or detected).strip() or detected
    path = out_dir / f"{item_id}.{orig_tag}.{detected}.vtt"
    path.write_text(vtt, encoding="utf-8")
    return path, detected


# ─── self-test ───────────────────────────────────────────────────────────────

def _self_test() -> int:
    failures = []

    def eq(got, want, label):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    eq(format_timestamp(0), "00:00:00.000", "zero")
    eq(format_timestamp(1.5), "00:00:01.500", "sub-second")
    eq(format_timestamp(3661.007), "01:01:01.007", "over an hour")
    eq(format_timestamp(-3), "00:00:00.000", "negative clamps to zero")

    vtt = segments_to_vtt([(0.0, 1.0, " hello  there "), (1.0, 2.0, "world")])
    if not vtt.startswith("WEBVTT\n\n"):
        failures.append("must start with the WEBVTT header")
    if "00:00:00.000 --> 00:00:01.000\nhello there" not in vtt:
        failures.append(f"cue text not normalised: {vtt!r}")

    # Bad intervals: overlapping, inverted, blank
    vtt = segments_to_vtt([(0.0, 5.0, "a"), (2.0, 1.0, "b"), (9.0, 9.0, "   ")])
    if "00:00:05.000 --> 00:00:05.500\nb" not in vtt:
        failures.append(f"overlapping/inverted cue not fixed: {vtt!r}")
    if vtt.count("-->") != 2:
        failures.append(f"blank cue should be dropped: {vtt!r}")

    eq(segments_to_vtt([]), "", "no segments gives empty string")
    eq(segments_to_vtt([(0, 1, "  ")]), "", "all-blank gives empty string")

    ok, why = available()
    print(f"local_transcribe backend: {'ok' if ok else 'unavailable — ' + why}")

    for f in failures:
        print("FAIL:", f)
    print("local_transcribe self-test:", "FAILED" if failures else "ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_self_test())
