"""
device_detector.py
Auto-detects device type (NVR / DVR / XVR / IP CAM) and capability tier
by probing ONVIF services and parsing manufacturer info.

Tier system
-----------
TIER_A  Full ONVIF Profile G  → use Recording + Replay services
TIER_B  Partial ONVIF         → use Media service for live, manufacturer RTSP for playback
TIER_C  ONVIF live-only       → live stream only, no playback via this system
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("device_detector")

# ── Device type constants ────────────────────────────────────────────────
TYPE_NVR    = "NVR"
TYPE_DVR    = "DVR"
TYPE_XVR    = "XVR"
TYPE_IPCAM  = "IP_CAM"

TIER_A = "PROFILE_G"       # Full recording + replay via ONVIF
TIER_B = "RTSP_FALLBACK"   # Manufacturer-specific RTSP playback
TIER_C = "LIVE_ONLY"       # Live only

# ── Manufacturer fingerprints ─────────────────────────────────────────────
# Maps lowercase substrings in manufacturer/model to (brand_key, device_hint)
MANUFACTURER_MAP = {
    "hikvision":  ("hikvision", TYPE_NVR),
    "hikvisio":   ("hikvision", TYPE_NVR),
    "ds-":        ("hikvision", TYPE_NVR),   # model prefix
    "dahua":      ("dahua",     TYPE_NVR),
    "dh-":        ("dahua",     TYPE_NVR),
    "imou":       ("dahua",     TYPE_NVR),
    "reolink":    ("reolink",   TYPE_IPCAM),
    "axis":       ("axis",      TYPE_IPCAM),
    "hanwha":     ("hanwha",    TYPE_NVR),
    "samsung":    ("hanwha",    TYPE_NVR),
    "uniview":    ("uniview",   TYPE_NVR),
    "unv":        ("uniview",   TYPE_NVR),
    "tiandy":     ("tiandy",    TYPE_NVR),
    "cp plus":    ("cpplus",    TYPE_DVR),
    "cpplus":     ("cpplus",    TYPE_DVR),
    "tvt":        ("tvt",       TYPE_DVR),
    "provision":  ("provision", TYPE_DVR),
    "annke":      ("annke",     TYPE_DVR),
    "swann":      ("swann",     TYPE_DVR),
    "lorex":      ("lorex",     TYPE_DVR),
    "amcrest":    ("amcrest",   TYPE_NVR),
    "vivotek":    ("vivotek",   TYPE_IPCAM),
    "bosch":      ("bosch",     TYPE_IPCAM),
    "pelco":      ("pelco",     TYPE_IPCAM),
}

# Model keywords that indicate recorder type
XVR_KEYWORDS = ["xvr", "hdcvi", "hdtvi", "hd-tvi", "hdcvi", "ahd", "cvbs", "hybrid"]
DVR_KEYWORDS  = ["dvr", "analog", "anl"]
NVR_KEYWORDS  = ["nvr", "poe", "nd-", "ds-76", "ds-77", "ds-96"]


@dataclass
class DeviceCapabilities:
    device_type:    str = TYPE_IPCAM
    brand:          str = "generic"
    manufacturer:   str = ""
    model:          str = ""
    firmware:       str = ""
    serial:         str = ""
    tier:           str = TIER_C
    channel_count:  int = 1
    profiles:       list = field(default_factory=list)  # [{token, name, resolution}]
    has_recording:  bool = False   # ONVIF Recording service available
    has_replay:     bool = False   # ONVIF Replay service available
    has_media2:     bool = False   # Media2 service available
    rtsp_port:      int = 554
    onvif_port:     int = 80
    capabilities_raw: dict = field(default_factory=dict)


def detect(host: str, port: int, username: str, password: str) -> DeviceCapabilities:
    """
    Connect to device, probe ONVIF services, return DeviceCapabilities.
    Gracefully degrades — never raises on service unavailability.
    """
    from onvif import ONVIFCamera
    caps = DeviceCapabilities(onvif_port=port)

    try:
        cam = ONVIFCamera(host, port, username, password)
    except Exception as e:
        raise ConnectionError(f"Cannot reach {host}:{port} via ONVIF — {e}")

    # 1. Device info
    try:
        dev = cam.create_devicemgmt_service()
        info = dev.GetDeviceInformation()
        caps.manufacturer = str(getattr(info, "Manufacturer", ""))
        caps.model        = str(getattr(info, "Model", ""))
        caps.firmware     = str(getattr(info, "FirmwareVersion", ""))
        caps.serial       = str(getattr(info, "SerialNumber", ""))
        log.info("Device: %s %s", caps.manufacturer, caps.model)
    except Exception as e:
        log.warning("GetDeviceInformation failed: %s", e)

    # 2. Manufacturer + device-type fingerprint
    caps.brand, caps.device_type = _fingerprint(caps.manufacturer, caps.model)

    # 3. Enumerate media profiles → discover channels
    try:
        media = cam.create_media_service()
        raw_profiles = media.GetProfiles()
        caps.profiles = _parse_profiles(raw_profiles)
        caps.channel_count = len(caps.profiles)
        log.info("Found %d profiles/channels", caps.channel_count)
    except Exception as e:
        err_msg = str(e).lower()
        if "authorize" in err_msg or "auth" in err_msg or "credential" in err_msg:
            raise ConnectionError(f"Authentication failed: {e}")
        log.warning("GetProfiles failed: %s", e)

    # Refine device type based on channel count
    if caps.channel_count >= 4 and caps.device_type == TYPE_IPCAM:
        caps.device_type = TYPE_NVR   # many channels → likely a recorder

    # 4. Probe capabilities to find Recording / Replay / Media2
    try:
        dev = cam.create_devicemgmt_service()
        raw_caps = dev.GetCapabilities({"Category": "All"})
        caps.capabilities_raw = _caps_to_dict(raw_caps)

        cap_ext = getattr(raw_caps, "Extension", None)
        analytics_caps = getattr(raw_caps, "Analytics", None)

        # Check for Recording service
        rec_url = _find_url(raw_caps, "Recording")
        if rec_url:
            caps.has_recording = True
            log.info("Recording service found: %s", rec_url)

        # Check for Replay service
        rep_url = _find_url(raw_caps, "Replay")
        if rep_url:
            caps.has_replay = True
            log.info("Replay service found: %s", rep_url)

        # Check for Media2
        m2_url = _find_url(raw_caps, "Media")
        if m2_url and "media2" in m2_url.lower():
            caps.has_media2 = True

    except Exception as e:
        log.warning("GetCapabilities failed: %s", e)

    # Try actually instantiating Recording and Replay to confirm
    if not caps.has_recording:
        try:
            rec_svc = cam.create_recording_service()
            rec_svc.GetRecordingSummary()
            caps.has_recording = True
            log.info("Recording service confirmed via instantiation")
        except Exception:
            pass

    if not caps.has_replay:
        try:
            rep_svc = cam.create_replay_service()
            caps.has_replay = True
            log.info("Replay service confirmed via instantiation")
        except Exception:
            pass

    # 5. Determine capability tier
    if caps.has_recording and caps.has_replay:
        caps.tier = TIER_A
    elif caps.brand in ("hikvision", "dahua", "amcrest", "annke", "lorex",
                        "swann", "cpplus", "tvt", "uniview", "hanwha", "tiandy"):
        caps.tier = TIER_B   # known brands with RTSP fallback patterns
    else:
        caps.tier = TIER_C

    log.info("Detected: %s %s | tier=%s | channels=%d",
             caps.device_type, caps.brand, caps.tier, caps.channel_count)
    return caps


# ── Helpers ───────────────────────────────────────────────────────────────

def _fingerprint(manufacturer: str, model: str) -> tuple[str, str]:
    """Return (brand_key, device_type) from manufacturer + model strings."""
    combined = (manufacturer + " " + model).lower()

    brand = "generic"
    device_type = TYPE_IPCAM

    for keyword, (b, dt) in MANUFACTURER_MAP.items():
        if keyword in combined:
            brand = b
            device_type = dt
            break

    # Refine device type from model keywords
    model_lower = model.lower()
    if any(k in model_lower for k in XVR_KEYWORDS):
        device_type = TYPE_XVR
    elif any(k in model_lower for k in NVR_KEYWORDS):
        device_type = TYPE_NVR
    elif any(k in model_lower for k in DVR_KEYWORDS):
        device_type = TYPE_DVR

    return brand, device_type


def _parse_profiles(raw_profiles) -> list[dict]:
    """Parse ONVIF profiles into clean channel dicts."""
    channels = []
    for p in raw_profiles:
        token = str(getattr(p, "token", ""))
        name  = str(getattr(p, "Name", token))

        # Resolution from VideoEncoderConfiguration or VideoEncoderConfiguration2
        resolution = ""
        vec = getattr(p, "VideoEncoderConfiguration", None)
        if vec:
            res = getattr(vec, "Resolution", None)
            if res:
                resolution = f"{getattr(res,'Width','')}x{getattr(res,'Height','')}"

        channels.append({
            "token":      token,
            "name":       name,
            "resolution": resolution,
        })
    return channels


def _find_url(raw_caps, service_name: str) -> str | None:
    """Recursively search capability object for a service URL containing service_name."""
    name_lower = service_name.lower()
    try:
        for attr_name in dir(raw_caps):
            if attr_name.startswith("_"):
                continue
            try:
                val = getattr(raw_caps, attr_name)
            except Exception:
                continue
            if isinstance(val, str) and name_lower in val.lower() and val.startswith("http"):
                return val
            if hasattr(val, "__dict__"):
                nested = _find_url(val, service_name)
                if nested:
                    return nested
    except Exception:
        pass
    return None


def _caps_to_dict(raw_caps) -> dict:
    """Best-effort conversion of capability zeep object to dict."""
    try:
        from zeep.helpers import serialize_object
        return dict(serialize_object(raw_caps))
    except Exception:
        return {}
