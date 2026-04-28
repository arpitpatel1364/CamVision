"""
live_streamer.py
Handles continuous live streaming from an RTSP source to the browser.
Uses ffmpeg to transmux RTSP (usually H.264/H.265) into fragmented MP4.
"""

import asyncio
import logging
from asyncio import subprocess

log = logging.getLogger("live_streamer")

READ_SIZE = 65536  # Increased to 64KB to ensure full headers are sent

async def stream_live(rtsp_uri: str, low_res: bool = False):
    """
    Async generator that yields fragmented MP4 bytes from a live RTSP stream.
    
    This uses 'copy' for video to avoid CPU-heavy re-encoding unless transcoding is needed.
    Audio is converted to AAC to ensure browser compatibility.
    """
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_uri,
        "-map", "0:v:0",         # Map first video stream
        "-map", "0:a?",           # Map audio if it exists
        "-c:v", "libx264",       # Force H.264 for universal browser compatibility
        "-preset", "ultrafast",  # Minimum latency
        "-tune", "zerolatency",
        "-crf", "28",            # Controlled quality/bandwidth (lower is higher quality)
        "-maxrate", "1024k",     # Cap bandwidth to 1Mbps
        "-bufsize", "2048k",
    ]

    if low_res:
        cmd += ["-vf", "scale=640:-2"] # Resize to 640px width, maintaining aspect ratio (must be even for libx264)

    cmd += [
        "-pix_fmt", "yuv420p",   # Required for many browsers
        "-c:a", "aac",           # Transcode audio to AAC
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
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
            assert proc.stdout is not None
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
                # Ensure we have a string before indexing
                log.error("ffmpeg live error: %s", str(err_text)[-500:])
        
        log.info("Live stream closed. Total bytes sent: %d", total)
