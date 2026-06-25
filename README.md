# Thrilia testimony

A voice-led testimonial recorder that edits itself. Built on the same AI
editing engine as AutoSticker (Whisper transcription, Groq vision scene
analysis, sticker/caption generation, FFmpeg render, AI sound design), but
with a different flow:

```
Landing  →  Interview (voice-only questions, hidden text)  →  one automatic
edit pass (single 0–100% progress)  →  Result (Actual vs Edited toggle)  →
optional manual Editor for further tweaks
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your API keys
uvicorn main:app --reload
```

Open `http://localhost:8000`. FFmpeg and ffprobe must be on your PATH.

## What's different from AutoSticker

- **Interview is voice-only.** Questions are read aloud (Web Speech API,
  no API key needed) and are never shown as on-screen text — the setup
  screen lets you pick a voice and **preview** it before you start.
- **One pipeline, one progress bar.** `/auto-edit/{job_id}` runs analysis
  and render back-to-back and reports a single continuous 0–100%, instead
  of stopping after analysis for a mandatory manual review.
- **Result page with a toggle.** `/result` shows the rendered video with
  an Actual/Edited switch, a download button, and an "Edit further" button
  that opens the same manual timeline editor as before.
- **Deferred, explicit cleanup.** Nothing is deleted automatically after
  render. `job_id` is kept in `localStorage`, so closing the tab mid-flow
  and coming back later just resumes polling — temp files and uploads are
  only removed when you hit **"Start a new testimony"** (calls
  `POST /discard/{job_id}`).
- **Job-scoped temp dirs.** Per-job audio/frame scratch space now lives
  under `temp/{job_id}/` so discard is well-defined. Shared caches
  (`temp/gifs`, `temp/music`) are untouched by discard since other jobs
  may reference them.
- **Jobs persist to disk** (`temp/jobs_store.json`) so a server restart
  doesn't orphan an in-progress job a client is still polling for.

## Two files you'll need to carry over yourself

These weren't included in what you sent me, and I didn't want to guess at
working API integrations and risk breaking them:

- **`feature/searchKlipy.py`** — only needed if you want `STICKER_SOURCE=klipy`.
  This project defaults to `STICKER_SOURCE=giphy` and runs fine without it.
  If you add it back, it needs `search_klipy_stickers(query, limit)` and
  `pick_unique_sticker(results, used_ids)` with the same shapes as
  `searchGiphy.py`'s `search_giphy`/`pick_unique_gif`.
- **`feature/cleanupTempFiles.py`** — no longer used. The old
  "clean up after every render" behavior was intentionally replaced by the
  job-scoped discard flow described above, so this file isn't imported
  anywhere in the new `main.py`.

## Route map

| Route | Purpose |
|---|---|
| `GET /` | Landing page |
| `GET /interview` | Voice setup + recording (single page, screen-switching) |
| `POST /upload` | Accepts the merged recording, creates a job |
| `POST /auto-edit/{job_id}` | Kicks off the combined analysis+render pass |
| `GET /status/{job_id}` | Poll progress (used by `/processing`) |
| `GET /processing` | Single progress bar page |
| `GET /result` | Actual/Edited toggle, download, edit-further, discard |
| `GET /editor` | Manual timeline editor (unchanged from AutoSticker) |
| `POST /render/{job_id}` | Re-render after manual edits (editor's Export) |
| `GET /download/{job_id}` | Force-download the final video |
| `GET /preview-output/{job_id}` | Inline (non-download) stream of the edited output |
| `GET /preview-video/{job_id}` | Inline stream of the original recording |
| `POST /discard/{job_id}` | Deletes this job's video/output/temp scratch space |

`/search-stickers`, `/download-sticker`, `/upload-music`, `/timeline/{job_id}`
(GET+POST) are unchanged — they're what the manual editor uses.

## Notes

- This project does **not** include a generic "upload your own video"
  page — the only entry point is the interview. If you want that back,
  the original `app.html` + `/upload`+`/generate` pattern is straightforward
  to re-add as a separate route.
- The shared visual identity (`static/theme.css`) is used by the landing,
  processing, and result pages. `interview.html` keeps its own embedded
  styles (it's a larger, more interactive page) but uses matching colors
  and fonts (Fraunces + DM Mono) for visual consistency.
