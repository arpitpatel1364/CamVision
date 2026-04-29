"""
chunk_streamer.py
Uses ffmpeg to pull a time-bounded chunk from any RTSP source and pipe it
as fragmented MP4 directly to the HTTP response.

Zero bytes written to disk.  The chunk flows:
  Camera storage → ffmpeg → HTTP response → browser

Supports
--------
TIER_A (ONVIF Profile G)   : replay URI + wall-clock seek
TIER_B (RTSP fallback)     : manufacturer URI already contains time params
TIER_C / live              : live RTSP URI, no seek (for quick preview)
"""

import asyncio
import logging
from asyncio import subprocess
from datetime import datetime, timezone

log = logging.getLogger("chunk_streamer")

MAX_CHUNK_SECONDS = 300   # 5-minute limit for full stream fetching
READ_SIZE         = 65536  # 64 KB pipe read buffer


def _build_cmd(
    rtsp_uri: str,
    start_time: str | None,
    duration: int,
    seek_mode: str = "input",   # "input" = fast seek before open, "output" = precise
    low_res: bool = False,
) -> list[str]:
    """
    Build the ffmpeg command list.
    """
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-avoid_negative_ts", "make_zero",
    ]

    if seek_mode == "input" and start_time:
        cmd += ["-ss", start_time]

    cmd += ["-i", rtsp_uri]

    if seek_mode == "output" and start_time:
        cmd += ["-ss", start_time]

    cmd += [
        "-t",  str(duration),
        "-map", "0:v:0",         # Explicitly map the first video stream
        "-map", "0:a?",           # Map audio ONLY if it exists (?)
        "-c:v", "libx264",       # Transcode to H.264 for browser compatibility
        "-preset", "ultrafast",  # Ensure no delay in chunk generation
        "-tune", "zerolatency",
    ]

    if low_res:
        cmd += ["-vf", "scale=640:-2"]

    cmd += [
        "-pix_fmt", "yuv420p",   # Standard web pixel format
        "-c:a", "aac",           # convert audio to web-friendly AAC if present
        "-f",  "mp4",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "pipe:1",
    ]
    return cmd


async def stream_chunk(
    rtsp_uri: str,
    start_time: str | None,
    duration: int,
    seek_mode: str = "input",
    low_res: bool = False,
):
    """
    Async generator.  Yields raw MP4 bytes from ffmpeg stdout.
    The caller (FastAPI StreamingResponse) consumes this directly.

    Parameters
    ----------
    rtsp_uri    : full RTSP URI with credentials
    start_time  : ISO 8601 string or HH:MM:SS offset; None for live
    duration    : seconds (capped at MAX_CHUNK_SECONDS)
    seek_mode   : see _build_cmd()
    """
    duration = min(duration, MAX_CHUNK_SECONDS)
    cmd = _build_cmd(rtsp_uri, start_time, duration, seek_mode,low_res)

    log.info("ffmpeg seek_mode=%s start=%s dur=%ds", seek_mode, start_time, duration)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if proc is None or proc.stdout is None or proc.stderr is None:
        log.error("Failed to initialize ffmpeg subprocess streams")
        return

    total = 0
    try:
        while True:
            assert proc.stdout is not None
            chunk = await proc.stdout.read(READ_SIZE)
            if not chunk:
                break
            total += len(chunk)
            yield chunk
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        stderr_data = await proc.stderr.read()
        if stderr_data:
            err_text = stderr_data.decode(errors="replace")
            # Extract the end of the error log (usually contains the reason for failure)
            err_snippet = err_text[-500:] if len(err_text) > 500 else err_text

            if total == 0:
                log.error("ffmpeg failed to pull data! Stderr: %s", err_snippet)
            else:
                log.debug("ffmpeg stderr: %s", err_snippet)
        
        if total == 0:
            log.warning("Chunk complete — sent 0 bytes. Check camera credentials and storage availability.")
        else:
            log.info("Chunk complete — sent %d bytes", total)


# ── Smart dispatcher ──────────────────────────────────────────────────────

def pick_seek_mode(tier: str, start_time: str | None) -> str:
    """
    Choose the right ffmpeg seek strategy based on device tier.

    TIER_A  ONVIF Profile G:   replay URI is open-ended, use output seek
                               for wall-clock accuracy.
    TIER_B  RTSP fallback:     URI already contains starttime param,
                               ffmpeg only needs to limit duration.
    TIER_C  Live only:         no seek at all.
    """
    if tier == "PROFILE_G" and start_time:
        return "output"
    elif tier == "RTSP_FALLBACK" and start_time:
        return "input"
    else:
        return "none"


def iso_to_ffmpeg_offset(start: datetime, reference: datetime) -> str:
    """
    Convert a wall-clock datetime to an HH:MM:SS offset relative to
    the stream's reference point.  Used for TIER_A ONVIF replay where
    ffmpeg opens the stream at the recording's beginning and seeks forward.
    """
    delta = (start - reference).total_seconds()
    delta = float(max(0.0, delta))
    h = int(delta // 3600)
    m = int((delta % 3600) // 60)
    s = int(delta % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
