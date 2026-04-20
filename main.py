"""
main.py — ONVIF Playback Hub
Central API server.  Brokers requests between browser and cameras.
No video is ever written to disk on this server.

Endpoints
---------
GET  /cameras                         list registered devices
POST /cameras                         add + auto-detect device
DELETE /cameras/{id}                  remove device
GET  /cameras/{id}/info               full device info + capabilities
GET  /cameras/{id}/channels           list channels / ONVIF profiles
GET  /cameras/{id}/recordings         list recordings (Profile G)
GET  /cameras/{id}/live-uri           get live RTSP URI for a channel
GET  /cameras/{id}/chunk              stream a time-bounded MP4 chunk
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import device_detector as dd
from onvif_client import ONVIFClient
from rtsp_fallback import build_playback_uri, channel_from_profile_name
from chunk_streamer import stream_chunk, pick_seek_mode, iso_to_ffmpeg_offset
from live_streamer import stream_live

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("hub")

app = FastAPI(title="CamVision", version="2.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

DB_FILE = Path("cameras.json")


# ── In-memory camera registry ─────────────────────────────────────────────

def _load() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}

def _save(data: dict):
    DB_FILE.write_text(json.dumps(data, indent=2, default=str))

CAMERAS: dict = _load()


# ── Pydantic models ───────────────────────────────────────────────────────

class CameraIn(BaseModel):
    name:      str
    host:      str
    port:      int = 80
    rtsp_port: int = 554
    username:  str
    password:  str


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_cam(cam_id: str) -> dict:
    if cam_id not in CAMERAS:
        raise HTTPException(404, "Camera not found")
    return CAMERAS[cam_id]

def _make_client(cam: dict) -> ONVIFClient:
    return ONVIFClient(cam["host"], cam["port"], cam["username"], cam["password"])

def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 string to timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, f"Invalid datetime: {s!r} — use ISO 8601")


# ── Camera management ─────────────────────────────────────────────────────

@app.get("/cameras")
def list_cameras():
    return [
        {
            "id":          cid,
            "name":        c["name"],
            "host":        c["host"],
            "device_type": c.get("device_type", "UNKNOWN"),
            "brand":       c.get("brand", "generic"),
            "tier":        c.get("tier", "LIVE_ONLY"),
            "channels":    len(c.get("profiles", [])),
        }
        for cid, c in CAMERAS.items()
    ]


@app.post("/cameras", status_code=201)
def add_camera(body: CameraIn):
    """
    Add a device.  Auto-detects NVR / DVR / XVR / IP CAM type and
    ONVIF capability tier.
    """
    try:
        caps = dd.detect(body.host, body.port, body.username, body.password)
    except ConnectionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Detection error: {e}")

    cam_id = uuid.uuid4().hex[:8]
    CAMERAS[cam_id] = {
        "name":        body.name,
        "host":        body.host,
        "port":        body.port,
        "rtsp_port":   body.rtsp_port,
        "username":    body.username,
        "password":    body.password,
        # detected fields
        "device_type": caps.device_type,
        "brand":       caps.brand,
        "manufacturer":caps.manufacturer,
        "model":       caps.model,
        "firmware":    caps.firmware,
        "serial":      caps.serial,
        "tier":        caps.tier,
        "profiles":    caps.profiles,
        "has_recording": caps.has_recording,
        "has_replay":    caps.has_replay,
    }
    _save(CAMERAS)
    log.info("Added %s: %s %s | tier=%s | channels=%d",
             cam_id, caps.device_type, caps.brand, caps.tier, caps.channel_count)
    return {"id": cam_id, "device_type": caps.device_type, "tier": caps.tier,
            "brand": caps.brand, "channels": len(caps.profiles)}


@app.delete("/cameras/{cam_id}")
def remove_camera(cam_id: str):
    _get_cam(cam_id)
    del CAMERAS[cam_id]
    _save(CAMERAS)
    return {"status": "removed"}


@app.get("/cameras/{cam_id}/info")
def camera_info(cam_id: str):
    cam = _get_cam(cam_id)
    return {k: v for k, v in cam.items() if k not in ("username", "password")}


# ── Channels ──────────────────────────────────────────────────────────────

@app.get("/cameras/{cam_id}/channels")
def get_channels(cam_id: str):
    """Return list of channels / ONVIF profiles for this device."""
    cam = _get_cam(cam_id)
    # Stored profiles are good enough; re-probe only if empty
    if cam.get("profiles"):
        return cam["profiles"]
    try:
        client = _make_client(cam)
        profiles = client.get_profiles()
        CAMERAS[cam_id]["profiles"] = profiles
        _save(CAMERAS)
        return profiles
    except Exception as e:
        err_msg = str(e)
        if "authorize" in err_msg.lower():
            raise HTTPException(401, f"Camera authentication failed. Check credentials: {e}")
        raise HTTPException(500, f"Cannot fetch channels: {e}")


# ── Live URI ─────────────────────────────────────────────────────────────

@app.get("/cameras/{cam_id}/live-uri")
def live_uri(
    cam_id:        str,
    profile_token: Optional[str] = Query(None, description="ONVIF profile token; defaults to first channel"),
):
    """
    Return the RTSP live stream URI for a channel.
    The client (browser extension, VLC, or ffplay) opens this directly.
    """
    cam = _get_cam(cam_id)
    # Default to first profile if none given
    token = profile_token
    if not token and cam.get("profiles"):
        token = cam["profiles"][0]["token"]
    if not token:
        raise HTTPException(400, "No profile_token provided and no profiles cached")

    try:
        client = _make_client(cam)
        uri = client.get_live_uri(token)
        return {"uri": uri, "profile_token": token}
    except Exception as e:
        raise HTTPException(500, f"Cannot get live URI: {e}")


@app.get("/cameras/{cam_id}/live-stream")
async def live_stream(
    cam_id:        str,
    profile_token: Optional[str] = Query(None),
):
    """
    Continuous live stream in fragmented MP4 format.
    """
    cam = _get_cam(cam_id)
    token = profile_token
    if not token and cam.get("profiles"):
        token = cam["profiles"][0]["token"]
    if not token:
        raise HTTPException(400, "No profile_token provided")

    try:
        client = _make_client(cam)
        uri = client.get_live_uri(token)
        return StreamingResponse(
            stream_live(uri),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
            }
        )
    except Exception as e:
        raise HTTPException(500, f"Live stream error: {e}")


# ── Recordings list ───────────────────────────────────────────────────────

@app.get("/cameras/{cam_id}/recordings")
def list_recordings(cam_id: str, channel: int = Query(1)):
    """
    List recordings from the device's own storage.
    Only available on TIER_A (ONVIF Profile G) devices.
    TIER_B devices return a time range based on their brand's RTSP capability.
    """
    cam = _get_cam(cam_id)
    tier = cam.get("tier", dd.TIER_C)

    if tier == dd.TIER_A:
        try:
            client = _make_client(cam)
            summary = client.get_recording_summary()
            recordings = client.list_recordings()
            return {"tier": tier, "summary": summary, "recordings": recordings}
        except Exception as e:
            raise HTTPException(500, f"Cannot list recordings: {e}")

    elif tier == dd.TIER_B:
        if cam.get("brand") == "hikvision":
            try:
                client = _make_client(cam)
                recordings = client.list_hikvision_recordings(channel=channel)
                return {
                    "tier": tier,
                    "message": f"Fetched recordings from Hikvision SD Card (Channel {channel}).",
                    "recordings": recordings
                }
            except Exception as e:
                log.warning("Hikvision ISAPI fetch failed: %s", e)

        # Fallback for other Tier B brands or if ISAPI fails
        return {
            "tier": tier,
            "message": f"{cam['brand'].upper()} supports RTSP time-based playback. "
                       "Enter a date/time to fetch a chunk directly.",
            "recordings": [],
        }
    else:
        return {
            "tier": tier,
            "message": "This device only supports live streaming.",
            "recordings": [],
        }


# ── Chunk stream ──────────────────────────────────────────────────────────

@app.get("/cameras/{cam_id}/chunk")
async def get_chunk(
    cam_id:          str,
    start:           str             = Query(...,  description="Start time ISO 8601, e.g. 2024-01-15T10:30:00"),
    duration:        int             = Query(30,   description="Chunk duration in seconds (max 120)"),
    profile_token:   Optional[str]   = Query(None, description="ONVIF profile token (for channel selection)"),
    recording_token: Optional[str]   = Query(None, description="ONVIF recording token (TIER_A only)"),
    channel:         int             = Query(1,    description="1-based channel number (TIER_B fallback)"),
    subtype:         int             = Query(0,    description="0=main stream, 1=sub stream"),
):
    """
    Stream a time-bounded MP4 chunk pulled from the camera's own storage.
    Nothing is written to disk on this server — pure ffmpeg passthrough.

    Flow
    ----
    TIER_A  → get ONVIF Replay URI → ffmpeg output-seek → pipe MP4
    TIER_B  → build brand RTSP URI with embedded time params → ffmpeg → pipe MP4
    TIER_C  → 400 error (playback not supported)
    """
    cam  = _get_cam(cam_id)
    tier = cam.get("tier", dd.TIER_C)
    duration = min(max(1, duration), 300)

    if tier == dd.TIER_C:
        raise HTTPException(400, "This device only supports live streaming; playback is not available.")

    start_dt = _parse_dt(start)
    end_dt   = start_dt.replace(second=start_dt.second + duration)

    rtsp_uri  = None
    seek_mode = "none"
    seek_ts   = None

    # ── TIER A: ONVIF Profile G ──────────────────────────────────────────
    if tier == dd.TIER_A:
        client = _make_client(cam)
        if not recording_token:
            # Smart auto-select using Search Service
            recording_token = client.find_recording_for_time(start_dt)
            if not recording_token:
                raise HTTPException(404, "No recording found for the requested time range.")

        try:
            rtsp_uri = client.get_replay_uri(recording_token)

            # Compute seek offset from recording start
            try:
                # We need the earliest start time of this specific recording to seek correctly
                recs      = client.list_recordings()
                rec_entry = next((r for r in recs if r["token"] == recording_token), None)
                earliest  = _parse_dt(rec_entry["earliest"]) if rec_entry and rec_entry.get("earliest") else start_dt
            except Exception:
                earliest = start_dt

            seek_ts   = iso_to_ffmpeg_offset(start_dt, earliest)
            seek_mode = "output"
            log.info("TIER_A chunk: seek=%s dur=%ds", seek_ts, duration)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Cannot get replay URI: {e}")

    # ── TIER B: Manufacturer RTSP fallback ───────────────────────────────
    elif tier == dd.TIER_B:
        # Determine channel number from profile token or explicit param
        if profile_token and cam.get("profiles"):
            prof = next((p for p in cam["profiles"] if p["token"] == profile_token), None)
            if prof:
                channel = channel_from_profile_name(prof["name"])

        brand = cam.get("brand", "generic")
        rtsp_uri = build_playback_uri(
            brand    = brand,
            host     = cam["host"],
            username = cam["username"],
            password = cam["password"],
            channel  = channel,
            start    = start_dt,
            end      = end_dt,
            rtsp_port= cam.get("rtsp_port", 554),
            subtype  = subtype,
        )
        seek_mode = "input"
        seek_ts   = None   # time is in the URI itself
        log.info("TIER_B chunk: brand=%s ch=%d start=%s", brand, channel, start)

    if not rtsp_uri:
        raise HTTPException(500, "Could not construct RTSP URI")

    return StreamingResponse(
        stream_chunk(rtsp_uri, seek_ts, duration, seek_mode),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="chunk_{start}_{duration}s.mp4"',
            "Cache-Control":       "no-cache, no-store",
            "X-Device-Type":       cam.get("device_type", ""),
            "X-Tier":              tier,
        },
    )


# ── Static frontend ───────────────────────────────────────────────────────

FRONTEND = Path(__file__).parent
if (FRONTEND / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
