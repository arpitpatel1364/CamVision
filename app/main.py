"""
main.py — CamVision Advance
Central API server. Brokers requests between browser and cameras.
"""

import json
import logging
import os
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

# Internal Imports
from app.services import device_detector as dd
from app.services.onvif_client import ONVIFClient
from app.services.rtsp_fallback import build_playback_uri, channel_from_profile_name
from app.services.chunk_streamer import stream_chunk, iso_to_ffmpeg_offset
from app.services.live_streamer import stream_live
from app.services.ai_processor import processor as ai
from app.db import database as db
from app.core import auth

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("hub")

app = FastAPI(title="CamVision Advance", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()
db.migrate_if_needed()

def log_action(username: str, action: str, details: str = ""):
    conn = db.get_db()
    conn.execute("INSERT INTO audit_logs (username, action, details) VALUES (?, ?, ?)", 
                 (username, action, details))
    conn.commit()
    conn.close()

# ── Models ─────────────────────────────────────────────────────────────────

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    bio: Optional[str] = None

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class CameraIn(BaseModel):
    name:      str
    host:      str
    port:      int = 80
    rtsp_port: int = 554
    username:  str
    password:  str

# ── Helpers ───────────────────────────────────────────────────────────────

def _get_cam(cam_id: str) -> dict:
    conn = db.get_db()
    row = conn.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Camera not found")
    res = dict(row)
    res["profiles"] = json.loads(res["profiles"])
    return res

def _make_client(cam: dict) -> ONVIFClient:
    return ONVIFClient(cam["host"], int(cam["port"]), cam["username"], cam["password"])

# ── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    return {"status": "online", "version": "2.0.4", "timestamp": time.time()}

@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (form_data.username,)).fetchone()
    conn.close()
    
    if not user or not auth.verify_password(form_data.password, user["password_hash"]):
        # Auto-create first user for bootstrap
        conn = db.get_db()
        users_count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        if users_count == 0:
            hashed_pass = auth.get_password_hash(form_data.password)
            conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", 
                         (form_data.username, hashed_pass, "admin"))
            conn.commit()
            conn.close()
            return {"access_token": auth.create_access_token(data={"sub": form_data.username}), "token_type": "bearer"}
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid credentials")
    
    return {"access_token": auth.create_access_token(data={"sub": form_data.username}), "token_type": "bearer"}

@app.get("/cameras")
def list_cameras():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM cameras").fetchall()
    conn.close()
    return [
        {
            "id": r["id"], "name": r["name"], "host": r["host"],
            "device_type": r["device_type"], "brand": r["brand"],
            "tier": r["tier"], "channels": len(json.loads(r["profiles"])),
            "primary_res": json.loads(r["profiles"])[0].get("resolution", "") if r["profiles"] else ""
        } for r in rows
    ]

@app.post("/cameras", status_code=201)
def add_camera(body: CameraIn, current_user: str = Depends(auth.get_current_user)):
    try:
        caps = dd.detect(body.host, body.port, body.username, body.password)
    except Exception as e:
        raise HTTPException(400, f"Detection failed: {e}")

    cam_id = uuid.uuid4().hex[:8]
    conn = db.get_db()
    conn.execute("INSERT INTO cameras VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
        (cam_id, body.name, body.host, body.port, body.rtsp_port, body.username, body.password, 
         caps.device_type, caps.brand, caps.manufacturer, caps.model, caps.firmware, caps.tier,
         json.dumps(caps.profiles), 1 if caps.has_recording else 0, 1 if caps.has_replay else 0))
    conn.commit()
    conn.close()
    log_action(current_user, "ADD_CAMERA", f"ID: {cam_id}")
    return {"id": cam_id}

@app.get("/cameras/{cam_id}/info")
def camera_info(cam_id: str):
    cam = _get_cam(cam_id)
    return {k: v for k, v in cam.items() if k not in ("username", "password")}

@app.get("/cameras/{cam_id}/live-stream")
async def live_stream(cam_id: str, profile_token: Optional[str] = Query(None), low_res: bool = Query(False)):
    cam = _get_cam(cam_id)
    token = profile_token or (cam["profiles"][0]["token"] if cam["profiles"] else None)
    if not token: raise HTTPException(400, "No profile token")
    client = _make_client(cam)
    uri = client.get_live_uri(token)
    return StreamingResponse(stream_live(uri, low_res=low_res), media_type="video/mp4")

@app.get("/api/me")
def get_me(current_user: str = Depends(auth.get_current_user)):
    conn = db.get_db()
    user = conn.execute("SELECT username, role, full_name, email, bio FROM users WHERE username = ?", (current_user,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(404, "User not found")
    return dict(user)

@app.put("/api/me")
def update_me(body: UserUpdate, current_user: str = Depends(auth.get_current_user)):
    conn = db.get_db()
    conn.execute("""
        UPDATE users 
        SET full_name = COALESCE(?, full_name), 
            email = COALESCE(?, email), 
            bio = COALESCE(?, bio) 
        WHERE username = ?
    """, (body.full_name, body.email, body.bio, current_user))
    conn.commit()
    conn.close()
    log_action(current_user, "UPDATE_PROFILE", "User updated profile info")
    return {"status": "success"}

@app.put("/api/me/password")
def change_password(body: PasswordChange, current_user: str = Depends(auth.get_current_user)):
    conn = db.get_db()
    user = conn.execute("SELECT password_hash FROM users WHERE username = ?", (current_user,)).fetchone()
    
    if not user or not auth.verify_password(body.old_password, user["password_hash"]):
        conn.close()
        raise HTTPException(400, "Invalid old password")
    
    new_hash = auth.get_password_hash(body.new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hash, current_user))
    conn.commit()
    conn.close()
    log_action(current_user, "CHANGE_PASSWORD", "User changed password")
    return {"status": "success"}

# Fallback for legacy /api/stream requests seen in logs
@app.get("/api/stream")
async def legacy_stream(cam_id: str = Query(...), token: str = Query(...)):
    # Simple validation of token (not fully secure for legacy fallback, but functional)
    try:
        auth.get_current_user(token)
        return await live_stream(cam_id)
    except Exception:
        raise HTTPException(401, "Invalid legacy token")

@app.get("/cameras/{cam_id}/detect")
async def detect_objects(cam_id: str):
    _get_cam(cam_id)
    detections = await ai.analyze_frame(cam_id)
    return {"detections": detections}

# Mounting Static Files
app.mount("/", StaticFiles(directory="app/static", html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)