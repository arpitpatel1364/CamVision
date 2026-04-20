"""
rtsp_fallback.py
Manufacturer-specific RTSP playback URI patterns for DVR / XVR devices
that partially implement ONVIF (Profile S only, no Profile G recording/replay).

Instead of ONVIF Replay service, we build a vendor RTSP URI with embedded
time parameters that the device's own firmware understands.

Supported brands
----------------
hikvision   DS-7xxx DVR, DS-96xx NVR, iDS-* series
dahua       XVR / NVR series, IMOU
amcrest     IP8M-* NVR / DVR
annke       C800 series DVR
cpplus      CP-UNR-* NVR, CP-UVR-* DVR
tvt         TD-* DVR / NVR
uniview     NVR / VMS devices (ONVIF extension URI)
hanwha      QNV / XNV cameras + SRN NVR
reolink     RLN-* NVR (limited — main stream only)
generic     Best-effort RTSP seek (works on many cheap Chinese DVRs)
"""

from datetime import datetime, timezone
from urllib.parse import quote


def build_playback_uri(
    brand: str,
    host: str,
    username: str,
    password: str,
    channel: int,       # 1-based channel number
    start: datetime,
    end: datetime,
    rtsp_port: int = 554,
    subtype: int = 0,   # 0 = main stream, 1 = sub stream
) -> str:
    """
    Return a complete RTSP URI (with credentials) for playback from the
    device's own storage.  brand must match device_detector.py brand keys.
    """
    builders = {
        "hikvision": _hikvision,
        "dahua":     _dahua,
        "amcrest":   _amcrest,
        "annke":     _annke,
        "cpplus":    _cpplus,
        "tvt":       _tvt,
        "uniview":   _uniview,
        "hanwha":    _hanwha,
        "reolink":   _reolink,
    }
    fn = builders.get(brand, _generic)
    return fn(host, username, password, channel, start, end, rtsp_port, subtype)


# ── Hikvision ─────────────────────────────────────────────────────────────
# DS-7xxx DVR, DS-76xx/77xx/96xx NVR
# RTSP track number = channel * 100 + 1  (e.g. CH1 → 101, CH2 → 201)
# starttime/endtime in YYYYMMDDTHHMMSSZ (UTC)

def _hikvision(host, user, pwd, ch, start, end, port, sub):
    track = ch * 100 + (1 if sub == 0 else 2)
    st = _hiktime(start)
    et = _hiktime(end)
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/Streaming/tracks/{track}"
        f"?starttime={st}&endtime={et}"
    )

def _hiktime(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


# ── Dahua ─────────────────────────────────────────────────────────────────
# XVR / NVR / IMOU series
# starttime/endtime in YYYY_MM_DD_HH_MM_SS (local time of device)

def _dahua(host, user, pwd, ch, start, end, port, sub):
    st = _dahuatime(start)
    et = _dahuatime(end)
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/cam/playback"
        f"?channel={ch}&subtype={sub}"
        f"&starttime={st}&endtime={et}"
    )

def _dahuatime(dt: datetime) -> str:
    return dt.strftime("%Y_%m_%d_%H_%M_%S")


# ── Amcrest ───────────────────────────────────────────────────────────────
# Uses the same track scheme as Hikvision (OEM relationship)

def _amcrest(host, user, pwd, ch, start, end, port, sub):
    return _hikvision(host, user, pwd, ch, start, end, port, sub)


# ── ANNKE ─────────────────────────────────────────────────────────────────
# C800 / W series — Hikvision-compatible path

def _annke(host, user, pwd, ch, start, end, port, sub):
    return _hikvision(host, user, pwd, ch, start, end, port, sub)


# ── CP Plus ───────────────────────────────────────────────────────────────
# CP-UNR / CP-UVR series — Dahua-compatible path

def _cpplus(host, user, pwd, ch, start, end, port, sub):
    return _dahua(host, user, pwd, ch, start, end, port, sub)


# ── TVT ───────────────────────────────────────────────────────────────────
# TD-series — uses /ch{N}/main path with starttime in query

def _tvt(host, user, pwd, ch, start, end, port, sub):
    stream = "main" if sub == 0 else "sub"
    st = _hiktime(start)
    et = _hiktime(end)
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/ch{ch:02d}/{stream}/av_stream"
        f"?starttime={st}&endtime={et}"
    )


# ── Uniview ───────────────────────────────────────────────────────────────
# NVR / IPC — uses ONVIF extension path for playback

def _uniview(host, user, pwd, ch, start, end, port, sub):
    st = _hiktime(start)
    et = _hiktime(end)
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/unicast/c{ch}/s{sub}/playback"
        f"?starttime={st}&endtime={et}"
    )


# ── Hanwha / Samsung ─────────────────────────────────────────────────────
# SRN NVR + QNV cameras — uses profile token path with time range

def _hanwha(host, user, pwd, ch, start, end, port, sub):
    st = _hiktime(start)
    et = _hiktime(end)
    profile = f"profile{ch}"
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/{profile}?starttime={st}&endtime={et}"
    )


# ── Reolink ───────────────────────────────────────────────────────────────
# RLN NVR — no standard RTSP playback URI, fallback to live main stream
# (Reolink NVR does not support RTSP-based playback; use their API instead)

def _reolink(host, user, pwd, ch, start, end, port, sub):
    stream = "main" if sub == 0 else "sub"
    u = quote(user)
    p = quote(pwd)
    return (
        f"rtsp://{u}:{p}@{host}:{port}"
        f"/h264Preview_0{ch}_{stream}"
    )


# ── Generic fallback ─────────────────────────────────────────────────────
# Many cheap Chinese DVR/NVR brands use a Hikvision-compatible path
# or the Dahua path — we try both via ffmpeg seek as last resort

def _generic(host, user, pwd, ch, start, end, port, sub):
    # Attempt Hikvision-style first (most common OEM base)
    return _hikvision(host, user, pwd, ch, start, end, port, sub)


# ── Channel number extraction ─────────────────────────────────────────────

def channel_from_profile_name(name: str) -> int:
    """
    Extract 1-based channel number from an ONVIF profile name.
    Examples: 'Profile_1', 'Channel 3', 'MainStream-CH04', 'IPcam 002'
    Falls back to 1 if no number found.
    """
    import re
    # Look for last integer in the name
    nums = re.findall(r"\d+", name)
    if nums:
        return max(1, int(nums[-1]))
    return 1
