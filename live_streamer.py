"""
live_streamer.py
Handles continuous live streaming from an RTSP source to the browser.
Uses ffmpeg to transmux RTSP (usually H.264/H.265) into fragmented MP4.
"""

import asyncio
import logging
from asyncio import subprocess

log = logging.getLogger("live_streamer")

READ_SIZE = 65536  # 64 KB pipe read buffer

async def stream_live(rtsp_uri: str):
    """
    Async generator that yields fragmented MP4 bytes from a live RTSP stream.
    
    This uses 'copy' for video to avoid CPU-heavy re-encoding.
    Audio is converted to AAC to ensure browser compatibility.
    """
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_uri,
        "-map", "0:v:0",         # Map first video stream
        "-map", "0:a?",           # Map audio if it exists
        "-c:v", "copy",          # Passthrough video (no re-encoding)
        "-c:a", "aac",           # Transcode audio to AAC (standard for web)
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "pipe:1"
    ]

    log.info("Starting live stream for %s", rtsp_uri.split("@")[-1]) # Log host only for privacy

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if proc is None or proc.stdout is None or proc.stderr is None:
        log.error("Failed to initialize ffmpeg live stream")
        return

    total = 0
    try:
        while True:
            chunk = await proc.stdout.read(READ_SIZE)
            if not chunk:
                break
            total += len(chunk)
            yield chunk
    except asyncio.CancelledError:
        log.info("Live stream cancelled by client")
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()
        
        if proc.stderr:
            stderr_data = await proc.stderr.read()
            if stderr_data:
                err_text = stderr_data.decode(errors="replace")
                log.error("ffmpeg live error: %s", err_text[-500:])
        
        log.info("Live stream closed. Total bytes sent: %d", total)
