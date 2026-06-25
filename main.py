"""
Thrilia testimony — FastAPI backend

Minimal flow:

  interview.html records a video -> POST /upload (video file + the
  logo_moments it tracked during recording) -> POST /auto-edit/{job_id}
  -> server (1) mixes static/m.mp3 under the original audio (music at
  0.15, voice untouched, music looped to cover the full video),
  (2) optionally burns in static/logo.png during the timestamp windows
  given in logo_moments, sliding in/out from off-screen-left at each
  window's edges (off by default — see LOGO_OVERLAY_ENABLED), and
  (3) appends a closing white card with the logo centered (see the
  OUTRO_* settings) -> poll GET /status/{job_id} -> play
  from GET /preview-output/{job_id} -> download from GET /download/{job_id}.

That's the entire pipeline. No transcription, no scene analysis, no
stickers, no captions, no AI sound design, no manual editor. The only
things this backend does to the video are: add background music,
optionally overlay the logo image at given moments, and append a
closing logo card.

Cleanup of a job's uploaded/rendered files is deferred — it does NOT
happen automatically after render. It only happens when the user
explicitly discards a job (POST /discard/{job_id}). This lets the
frontend keep a job_id in localStorage and safely resume after a
closed/reopened tab.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ─────────────────────────── config ──────────────────────────────────────────

MUSIC_PATH = Path("static/m.mp3")    # the one fixed background-music track
MUSIC_VOLUME = 0.13                # background music level; voice stays at 1.0

LOGO_OVERLAY_ENABLED = False          # set True to bring back the sliding logo overlay during the video itself
LOGO_PATH = Path("static/logo.png")  # short transparent logo image
LOGO_MARGIN_PX = 30                  # distance from the screen edge (bottom-left corner)
LOGO_WIDTH_PX = 500                 # logo is scaled to this width (height auto, keeps aspect ratio)
LOGO_OPACITY = 0.75                 # logo's own opacity over the video (1.0 = fully solid, 0 = invisible);
                                      # applies on top of the PNG's existing per-pixel alpha, so areas the
                                      # PNG already made transparent stay fully transparent
LOGO_SLIDE_SECONDS = 0.8             # time to slide in from off-screen-left / slide back out, at each window's edges
                                      # (only used while LOGO_OVERLAY_ENABLED is True)

# ── closing outro: white logo card, appended after the interview ──
OUTRO_ENABLED = True                  # set False to skip the outro entirely
OUTRO_LOGO_SECONDS = 2.5              # how long the centered-logo white card stays on screen
OUTRO_TRANSITION_SECONDS = 0.6        # crossfade duration: interview -> logo card (and logo card -> thank-you card, if enabled below)
OUTRO_THANKS_ENABLED = False          # set True to bring back the second "thank you" card after the logo card
OUTRO_THANKS_SECONDS = 0           # how long the thank-you card stays on screen (only used while OUTRO_THANKS_ENABLED is True)
OUTRO_THANKS_TEXT = "Thank You"       # message shown on the second card
OUTRO_THANKS_SUBTEXT = "for sharing your story"  # smaller line under the main message (omit by setting to "")
OUTRO_BG_COLOR = "white"              # outro background color (ffmpeg color name or 0xRRGGBB)
OUTRO_TEXT_COLOR = "0x0e5b36"         # main message color (Thrilia brand green)
OUTRO_SUBTEXT_COLOR = "0x6b7280"      # subtext color (muted grey, matches interview.html --muted)
OUTRO_CENTER_LOGO_WIDTH_FRAC = 0.68   # centered logo width as a fraction of the video's own width (height auto) — bigger logo on the closing card
OUTRO_FONT_PATH = "static/Inter-SemiBold.ttf"  # falls back to a system font automatically if missing

# ─────────────────────────── directories ─────────────────────────────────────

UPLOADS_DIR = Path("uploads")
OUTPUTS_DIR = Path("outputs")
TEMP_DIR = Path("temp")
JOBS_FILE = TEMP_DIR / "jobs_store.json"

for d in [UPLOADS_DIR, OUTPUTS_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────── app ─────────────────────────────────────────────

app = FastAPI(title="Thrilia testimony")

# Enable CORS for all origins (needed for S3 + ngrok setup)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://thrilia.com",
        "https://www.thrilia.com",
        "https://sharp-more-mackerel.ngrok-free.app",
        "https://g8k260x6-8000.inc1.devtunnels.ms"
    ],
    allow_origin_regex=".*",  # Also allow all other origins
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# Serve the frontend (html/css/js in the project root + static/)
app.mount("/static", StaticFiles(directory="."), name="static")


# ─────────────────────────── job store (disk-backed) ─────────────────────────

def _load_jobs_from_disk() -> dict:
    if not JOBS_FILE.exists():
        return {}
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        print(f"[main] Reloaded {len(jobs)} job(s) from {JOBS_FILE} on startup")
        return jobs
    except Exception as e:
        print(f"[main] Could not load {JOBS_FILE}, starting with empty job store: {e}")
        return {}


def _save_jobs_to_disk():
    """Best-effort persist of the full job dict. Never raises."""
    try:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(_jobs, f)
    except Exception as e:
        print(f"[main] Failed to persist jobs to {JOBS_FILE}: {e}")


_jobs: dict[str, dict] = _load_jobs_from_disk()


def _update_job(job_id: str, **kwargs):
    _jobs.setdefault(job_id, {}).update(kwargs)
    _save_jobs_to_disk()


def _get_job_or_404(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ─────────────────────────── CORS preflight handler ──────────────────────────

@app.options("/{path_name:path}")
async def preflight_handler(path_name: str):
    return {"status": "ok"}

# ─────────────────────────── page routes ─────────────────────────────────────

@app.get("/")
async def serve_landing_page():
    return FileResponse("landing.html")


@app.get("/interview")
async def serve_interview():
    return FileResponse("interview.html")


# ─────────────────────────── upload / add-music ──────────────────────────────

@app.post("/upload")
async def upload_video(file: UploadFile = File(...), logo_moments: str = Form("[]")):
    """
    Accept the recorded interview video, plus the logo_moments the client
    tracked during recording (JSON list of {"start": seconds, "duration":
    seconds}, relative to the top of the recording) — these mark when the
    logo overlay should appear in the final video.
    """
    allowed = {".mp4", ".mov", ".webm"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported format: {suffix}. Use {allowed}")

    try:
        moments = json.loads(logo_moments) if logo_moments else []
        if not isinstance(moments, list):
            moments = []
    except (json.JSONDecodeError, TypeError):
        moments = []
    # keep only well-formed {start, duration} numeric pairs
    moments = [
        {"start": float(m["start"]), "duration": float(m["duration"])}
        for m in moments
        if isinstance(m, dict) and "start" in m and "duration" in m
    ]

    job_id = uuid.uuid4().hex
    video_path = UPLOADS_DIR / f"{job_id}{suffix}"

    with open(video_path, "wb") as f:
        content = await file.read()
        f.write(content)

    _update_job(job_id,
                status="uploaded",
                video_path=str(video_path),
                video_suffix=suffix,
                logo_moments=moments,
                progress=0,
                step="Uploaded")

    return {"job_id": job_id}


def _build_logo_overlay_filter(logo_moments: list[dict]) -> str:
    """
    Build the `overlay=...` filter fragment that slides the logo in from
    off-screen-left to its resting position (bottom-left corner) at the
    start of each window, holds it, then slides it back out to
    off-screen-left at the end of each window — only during the given
    (start, duration) windows; invisible otherwise.

    x(t) is built as a sum of per-window ramps (each non-zero only within
    its own [start, start+duration] range, via FFmpeg's between()):
      - slide-in:  t in [start, start+slide]        x: -logo_w -> margin
      - resting:   t in [start+slide, end-slide]    x: margin
      - slide-out: t in [end-slide, end]             x: margin -> -logo_w
    `enable` uses the same [start, end] window so the logo is only ever
    drawn while one of these ramps is actually active.

    logo_moments must be non-empty (callers check this before calling).
    """
    windows = []
    x_terms = []
    for m in logo_moments:
        s = m["start"]
        d = m["duration"]
        sl = min(LOGO_SLIDE_SECONDS, d / 2)  # keep ramps from overlapping on very short windows
        e = s + 5.01
        windows.append(f"between(t,{s:.3f},{e:.3f})")
        in_ramp = (
            f"between(t,{s:.3f},{s + sl:.3f})*"
            f"(-{LOGO_WIDTH_PX}+({LOGO_WIDTH_PX}+{LOGO_MARGIN_PX})*(t-{s:.3f})/{sl:.4f})"
        )
        rest = f"between(t,{s + sl:.3f},{e - sl:.3f})*{LOGO_MARGIN_PX}"
        out_ramp = (
            f"between(t,{e - sl:.3f},{e:.3f})*"
            f"({LOGO_MARGIN_PX}-({LOGO_WIDTH_PX}+{LOGO_MARGIN_PX})*(t-({e - sl:.3f}))/{sl:.4f})"
        )
        x_terms.append(f"({in_ramp}+{rest}+{out_ramp})")

    enable_expr = "+".join(windows) if len(windows) == 1 else "(" + "+".join(windows) + ")"
    x_expr = "+".join(x_terms) if len(x_terms) == 1 else "(" + "+".join(x_terms) + ")"
    y_expr = f"main_h-overlay_h-{LOGO_MARGIN_PX}"  # bottom-left: margin up from the bottom edge

    return f"overlay=x='{x_expr}':y='{y_expr}':enable='{enable_expr}'"


def _probe_duration(video_path: str) -> float:
    """
    Get the video's real playback duration, with fallbacks for files that
    don't carry it in the container header — notably browser-recorded
    MediaRecorder .webm output, which is written in a streaming style and
    often has no duration in its format/EBML header at all (ffprobe then
    omits "duration" from `format` entirely rather than returning 0).
    """
    # 1) Fast path: container-level duration, when present.
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        duration = json.loads(result.stdout).get("format", {}).get("duration")
        if duration is not None:
            return float(duration)

    # 2) Fallback: last video packet's end time (pts_time + duration_time).
    # Much cheaper than a full decode, and accurate for the streaming-webm case.
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,duration_time",
        "-of", "csv=p=0",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        last_line = result.stdout.strip().splitlines()[-1]
        parts = last_line.split(",")
        try:
            pts = float(parts[0])
            dur = float(parts[1]) if len(parts) > 1 and parts[1] not in ("", "N/A") else 0.0
            if pts + dur > 0:
                return pts + dur
        except (ValueError, IndexError):
            pass

    # 3) Last resort: fully decode the video stream and read the time ffmpeg
    # reports once it hits end-of-stream. Slowest, but always works.
    cmd = ["ffmpeg", "-i", video_path, "-map", "0:v:0", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    match = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)

    raise RuntimeError(f"Could not determine duration of {video_path}")


def _probe_video(video_path: str) -> dict:
    """
    Read width, height, fps, and duration off the source video via ffprobe.
    Needed because the outro cards (white background + logo / thank-you text)
    have to be generated at the exact same resolution/fps as the uploaded
    video for `xfade` to accept them, and the crossfade `offset` has to land
    at the real end of the clip.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    stream = data["streams"][0]

    def _parse_rate(rate_str: str | None) -> float:
        if not rate_str or rate_str == "0/0":
            return 0.0
        num, den = rate_str.split("/")
        return float(num) / float(den) if float(den) else 0.0

    # r_frame_rate can be "0/0" on variable-frame-rate recordings (common
    # from browser MediaRecorder); avg_frame_rate is the more reliable
    # fallback in that case. 25fps is a last-resort sane default.
    fps = _parse_rate(stream.get("r_frame_rate")) or _parse_rate(stream.get("avg_frame_rate")) or 25.0

    duration = _probe_duration(video_path)

    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps,
        "duration": duration,
    }


def _has_audio_stream(video_path: str) -> bool:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    return bool(data.get("streams"))


def _resolve_outro_font() -> str | None:
    """
    Use the bundled font if present, else fall back to a common system font.
    Always returns a real file path (or None) — drawtext must never be left
    to resolve a font via fontconfig, since that silently fails with
    "Fontconfig error: Cannot load default config file" on many Windows
    installs (and any Linux box without a fontconfig setup), aborting the
    whole render instead of just falling back to a default font.
    """
    if Path(OUTRO_FONT_PATH).exists():
        return OUTRO_FONT_PATH
    for fallback in (
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        # Windows (checked via the %WINDIR% env var so this also works if
        # Windows isn't installed on the C: drive)
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf"),
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"),
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "calibrib.ttf"),
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"),
    ):
        if Path(fallback).exists():
            return fallback
    return None


def _escape_filter_path(path: str) -> str:
    """
    Escape a filesystem path for safe use as an ffmpeg filter option value
    (e.g. drawtext's fontfile=...). Backslashes and colons are both
    syntactically meaningful to the filter parser — this matters
    specifically for Windows paths like C:\\Windows\\Fonts\\arialbd.ttf,
    where the drive-letter colon would otherwise be read as the end of
    the option value and the backslashes as (mis-)escapes.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _escape_drawtext(text: str) -> str:
    """Escape characters that are meaningful inside an ffmpeg drawtext filter argument."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")


def _build_outro_filters(
    video_in_label: str,
    audio_in_label: str,
    logo_input_idx: int | None,
    silence_input_idx: int,
    video_info: dict,
    *,
    have_audio: bool,
) -> tuple[list[str], str, str]:
    """
    Build the filter_complex fragments that append the closing outro —
    a centered-logo white card, optionally followed by a "thank you"
    white card (OUTRO_THANKS_ENABLED) — onto the end of the interview,
    with a smooth crossfade at each join.

    Returns (filter_parts, final_video_label, final_audio_label).

    video_in_label / audio_in_label: labels of the video/audio to append
    the outro after (e.g. "0:v"/"0:a", or "[vout]"/"[aout]" if the logo
    overlay / music mix stages already ran).
    logo_input_idx: ffmpeg -i input index of static/logo.png, or None if
    that asset is missing (the logo card then just shows blank white).
    silence_input_idx: -i input index of an anullsrc input, used to pad
    audio under the two silent outro cards (and as the whole audio track
    when the source has no audio stream at all).
    """
    w, h, fps = video_info["width"], video_info["height"], video_info["fps"]
    dur = video_info["duration"]
    t = OUTRO_TRANSITION_SECONDS

    filters = []

    # Re-timebase the source onto a clean fps so xfade's frame-accumulation
    # lines up exactly with the still cards below.
    filters.append(f"{video_in_label}fps={fps},format=yuv420p[outro_v0]")

    # ---- Card 1: white background + centered logo (held for OUTRO_LOGO_SECONDS, plus
    # one transition's worth of extra hold so the crossfade has real frames to blend with) ----
    card1_dur = OUTRO_LOGO_SECONDS + t
    if logo_input_idx is not None:
        logo_w = max(2, int(round(w * OUTRO_CENTER_LOGO_WIDTH_FRAC)) // 2 * 2)
        filters.append(f"[{logo_input_idx}:v]scale={logo_w}:-1,format=rgba[outro_logo_sc]")
        filters.append(
            f"color=c={OUTRO_BG_COLOR}:s={w}x{h}:d={card1_dur},format=rgba[outro_bg1]"
        )
        filters.append(
            f"[outro_bg1][outro_logo_sc]overlay=(W-w)/2:(H-h)/2:shortest=1,"
            f"format=yuv420p,fps={fps}[outro_v1]"
        )
    else:
        filters.append(
            f"color=c={OUTRO_BG_COLOR}:s={w}x{h}:d={card1_dur}:r={fps},format=yuv420p[outro_v1]"
        )

    # ---- Card 2: white background + "Thank You" message (optional) ----
    if OUTRO_THANKS_ENABLED:
        card2_dur = OUTRO_THANKS_SECONDS + t
        font = _resolve_outro_font()
        if font:
            # A real fontfile is always passed explicitly — drawtext is never
            # left to resolve a font via fontconfig, which isn't reliably
            # configured on every machine (notably many Windows installs) and
            # fails the whole ffmpeg run rather than just falling back quietly.
            font_clause = f"fontfile='{_escape_filter_path(font)}':"
            main_text = _escape_drawtext(OUTRO_THANKS_TEXT)
            if OUTRO_THANKS_SUBTEXT:
                sub_text = _escape_drawtext(OUTRO_THANKS_SUBTEXT)
                text_filter = (
                    f",drawtext={font_clause}text='{main_text}':fontcolor={OUTRO_TEXT_COLOR}:"
                    f"fontsize={int(h * 0.09)}:x=(w-text_w)/2:y=h/2-(text_h):line_spacing=10,"
                    f"drawtext={font_clause}text='{sub_text}':fontcolor={OUTRO_SUBTEXT_COLOR}:"
                    f"fontsize={int(h * 0.035)}:x=(w-text_w)/2:y=h/2+(text_h)"
                )
            else:
                text_filter = (
                    f",drawtext={font_clause}text='{main_text}':fontcolor={OUTRO_TEXT_COLOR}:"
                    f"fontsize={int(h * 0.09)}:x=(w-text_w)/2:y=(h-text_h)/2"
                )
        else:
            # No usable font found anywhere on this machine — show a plain
            # white card rather than risk a fontconfig-dependent drawtext call.
            print("[render] No usable font file found for the outro text — showing a blank white card instead")
            text_filter = ""
        filters.append(
            f"color=c={OUTRO_BG_COLOR}:s={w}x{h}:d={card2_dur}:r={fps},format=yuv420p"
            f"{text_filter}[outro_v2]"
        )

    # ---- Crossfade chain: interview -> card1 [-> card2, if enabled] ----
    offset1 = max(0.0, dur - t)
    filters.append(
        f"[outro_v0][outro_v1]xfade=transition=fade:duration={t}:offset={offset1:.3f}[outro_x1]"
    )

    if OUTRO_THANKS_ENABLED:
        offset2 = offset1 + OUTRO_LOGO_SECONDS
        filters.append(
            f"[outro_x1][outro_v2]xfade=transition=fade:duration={t}:offset={offset2:.3f}[outro_vout]"
        )
        total_video_dur = offset2 + card2_dur
        final_video_label = "[outro_vout]"
    else:
        # No second card — the logo card (card1) is the last thing on screen,
        # so the video simply ends once it's held for its full duration.
        total_video_dur = offset1 + card1_dur
        final_video_label = "[outro_x1]"

    # ---- Audio: keep the original track under the interview + first transition,
    # fade it to silence, then silence under the still card(s) ----
    silence_needed = max(0.0, total_video_dur - dur)
    if have_audio:
        filters.append(f"{audio_in_label}afade=t=out:st={offset1:.3f}:d={t}[outro_a0]")
        filters.append(f"[{silence_input_idx}:a]atrim=duration={silence_needed:.3f}[outro_asil]")
        filters.append("[outro_a0][outro_asil]concat=n=2:v=0:a=1[outro_aout]")
    else:
        filters.append(f"[{silence_input_idx}:a]atrim=duration={total_video_dur:.3f}[outro_aout]")

    return filters, final_video_label, "[outro_aout]"


def _render_output(video_path: str, output_path: str, logo_moments: list[dict]):
    """
    Produce the final video:
      - background music: static/m.mp3 looped under the original audio,
        voice untouched at full volume, music at MUSIC_VOLUME underneath
        (skipped if MUSIC_PATH is missing)
      - logo overlay: static/logo.png, bottom-left corner, sliding in
        from off-screen-left / sliding back out at the edges of each
        window in logo_moments (off by default — see
        LOGO_OVERLAY_ENABLED; also skipped if LOGO_PATH is missing or
        logo_moments is empty)
      - closing outro: a white card with the logo centered, appended
        after the interview (skipped if OUTRO_ENABLED is False; a
        second "thank you" card can be re-enabled via
        OUTRO_THANKS_ENABLED)

    Falls back gracefully: if none of the above assets/steps apply, the
    source video is copied through untouched rather than failing the job.
    """
    have_music = MUSIC_PATH.exists()
    have_logo = LOGO_OVERLAY_ENABLED and LOGO_PATH.exists() and bool(logo_moments)
    have_outro = OUTRO_ENABLED
    source_has_audio = _has_audio_stream(video_path)

    if not have_music:
        print(f"[render] {MUSIC_PATH} not found — no background music will be added")
    if not LOGO_OVERLAY_ENABLED:
        print("[render] in-video logo overlay disabled (LOGO_OVERLAY_ENABLED=False) — skipping")
    elif LOGO_PATH.exists() and not logo_moments:
        print("[render] logo.png present but no logo_moments supplied — skipping logo overlay")
    elif not LOGO_PATH.exists():
        print(f"[render] {LOGO_PATH} not found — no logo overlay will be added")
    if have_outro and not LOGO_PATH.exists():
        print(f"[render] {LOGO_PATH} not found — outro logo card will show a blank white card")

    if not have_music and not have_logo and not have_outro:
        shutil.copyfile(video_path, output_path)
        return

    inputs = ["-i", video_path]
    filter_parts = []
    video_label = "0:v"
    next_input_idx = 1
    logo_input_idx = None  # shared between the slide-in overlay and the outro's centered card

    # The source video might have no audio stream at all (silent recording).
    # Synthesize a silent track up front so every later stage (music mix,
    # outro padding) can always rely on a real audio_label, instead of each
    # stage having to special-case "what if there's no [0:a]". anullsrc is
    # an infinite generator, so it must be trimmed to the source's actual
    # duration here — otherwise ffmpeg never reaches end-of-stream on it.
    if source_has_audio:
        audio_label = "0:a"
    else:
        source_duration = _probe_video(video_path)["duration"]
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        silence_src_idx = next_input_idx
        next_input_idx += 1
        filter_parts.append(f"[{silence_src_idx}:a]atrim=duration={source_duration:.3f}[srcaudio]")
        audio_label = "[srcaudio]"

    if have_logo or (have_outro and LOGO_PATH.exists()):
        inputs += ["-loop", "1", "-i", str(LOGO_PATH)]
        logo_input_idx = next_input_idx
        next_input_idx += 1

    if have_logo:
        # Scale, then force RGBA and multiply the alpha channel by
        # LOGO_OPACITY — this makes the logo see-through over the video
        # even on its "solid" parts, while pixels the PNG already made
        # transparent (alpha=0) stay fully transparent (0 * anything = 0).
        filter_parts.append(
            f"[{logo_input_idx}:v]scale={LOGO_WIDTH_PX}:-1,format=rgba,"
            f"colorchannelmixer=aa={LOGO_OPACITY}[logo]"
        )
        overlay_filter = _build_logo_overlay_filter(logo_moments)
        filter_parts.append(f"[{video_label}][logo]{overlay_filter}[vout]")
        video_label = "[vout]"

    if have_music:
        inputs += ["-stream_loop", "-1", "-i", str(MUSIC_PATH)]
        music_idx = next_input_idx
        next_input_idx += 1
        filter_parts.append(f"[{music_idx}:a]volume={MUSIC_VOLUME}[bgm]")
        filter_parts.append(f"[{audio_label}][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        audio_label = "[aout]"

    if have_outro:
        video_info = _probe_video(video_path)
        # Separate, fresh silent input used purely to pad the two outro
        # cards (kept distinct from the source-silence track above, since
        # that one may already be feeding into a music mix at this point).
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        pad_silence_idx = next_input_idx
        next_input_idx += 1

        def _bracketed(label: str) -> str:
            return label if label.startswith("[") else f"[{label}]"

        outro_filters, video_label, audio_label = _build_outro_filters(
            video_in_label=_bracketed(video_label),
            audio_in_label=_bracketed(audio_label),
            logo_input_idx=logo_input_idx,
            silence_input_idx=pad_silence_idx,
            video_info=video_info,
            have_audio=True,  # audio_label is now always real audio (source or synthesized above)
        )
        filter_parts += outro_filters

    cmd = ["ffmpeg", "-y", *inputs]
    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
    
    cmd += ["-map", video_label, "-map", audio_label]
    
    # ALWAYS transcode to H.264 with yuv420p since the source is WebM
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    
    cmd += ["-c:a", "aac", "-b:a", "192k"]
    
    if not have_outro:
        cmd += ["-shortest"]
    cmd += [output_path]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


async def _run_render(job_id: str):
    """The entire pipeline: take the uploaded video, add background music + logo overlay, done."""
    job = _jobs[job_id]
    video_path = job["video_path"]
    logo_moments = job.get("logo_moments", [])

    def progress(pct: int, step: str):
        _update_job(job_id, progress=pct, step=step)

    try:
        progress(20, "Adding background music & logo…")
        output_video = str(OUTPUTS_DIR / f"{job_id}_final.mp4")

        await asyncio.to_thread(_render_output, video_path, output_video, logo_moments)

        progress(100, "Done")
        _update_job(job_id, status="done", progress=100, step="Done",
                    output_video=output_video)

    except Exception as exc:
        import traceback
        _update_job(job_id, status="error", step=f"Error: {exc}", error=traceback.format_exc())


@app.post("/auto-edit/{job_id}")
async def auto_edit(job_id: str):
    """Kick off the only processing step there is: adding background music + logo overlay."""
    job = _get_job_or_404(job_id)
    if job.get("status") in ("processing",):
        raise HTTPException(409, "Job already running")

    _update_job(job_id, status="processing", progress=0, step="Starting…")
    asyncio.create_task(_run_render(job_id))

    return {"job_id": job_id, "status": "processing"}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Poll job status and progress."""
    return _get_job_or_404(job_id)


# ─────────────────────────── output / download / discard ────────────────────

@app.get("/preview-output/{job_id}")
async def preview_output(job_id: str):
    """Stream the rendered output inline (no Content-Disposition) so the result page's video can play it directly."""
    job = _get_job_or_404(job_id)
    output_path = job.get("output_video")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(404, "Output not ready")
    return FileResponse(str(output_path), media_type="video/mp4")


@app.get("/download/{job_id}")
async def download_video(job_id: str):
    job = _get_job_or_404(job_id)
    if job.get("status") != "done":
        raise HTTPException(404, "Video not ready")
    output_path = Path(job["output_video"])
    if not output_path.exists():
        raise HTTPException(404, "Output file missing")
    return FileResponse(str(output_path), media_type="video/mp4", filename="testimony.mp4")


@app.post("/discard/{job_id}")
async def discard_job(job_id: str):
    """
    Explicit cleanup. Deletes this job's uploaded source video and its
    rendered output, then drops the job from the store entirely.
    """
    job = _jobs.get(job_id)
    if not job:
        return {"ok": True, "already_gone": True}

    for key in ("video_path", "output_video"):
        path = job.get(key)
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception as e:
                print(f"[discard] Could not remove {path}: {e}")

    _jobs.pop(job_id, None)
    _save_jobs_to_disk()

    return {"ok": True}