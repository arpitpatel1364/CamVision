"""
onvif_client.py
Communicates with NVR / DVR / XVR / IP CAM via ONVIF.
Covers Profile S (live) and Profile G (recording + replay).
No video is stored — only URIs and metadata are fetched.
"""

import logging
import datetime
import requests
import xml.etree.ElementTree as ET
from datetime import datetime as dt_obj, timezone, timedelta
from urllib.parse import urlparse, urlunparse, quote
from requests.auth import HTTPDigestAuth
from onvif import ONVIFCamera

log = logging.getLogger("onvif_client")


class ONVIFClient:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host     = host
        self.port     = port
        self.username = username
        self.password = password
        self._cam       = None
        self._media     = None
        self._recording = None
        self._replay    = None
        self._search    = None

    def _connect(self):
        if not self._cam:
            self._cam = ONVIFCamera(self.host, self.port, self.username, self.password)
            # Apply time offset to prevent "Unauthorized" errors due to clock drift
            try:
                self._sync_time()
            except Exception as e:
                log.warning("Time sync failed (may cause auth errors): %s", e)
        return self._cam

    def _sync_time(self):
        """
        Fetches the camera's system time and calculates the offset.
        This is critical for WS-Security authentication.
        """
        if not self._cam:
            return
        try:
            devmgmt = self._cam.create_devicemgmt_service()
            system_date = devmgmt.GetSystemDateAndTime()
            
            # 1. Try UTCDateTime first
            utc = getattr(system_date, "UTCDateTime", None)
            if utc and getattr(utc, "Date", None) and getattr(utc, "Time", None):
                cam_utc = dt_obj(
                    utc.Date.Year, utc.Date.Month, utc.Date.Day,
                    utc.Time.Hour, utc.Time.Minute, utc.Time.Second,
                    tzinfo=timezone.utc
                )
            else:
                # 2. Fallback to LocalDateTime (less ideal, but better than crashing)
                local = getattr(system_date, "LocalDateTime", None)
                if local and getattr(local, "Date", None) and getattr(local, "Time", None):
                    # We assume the camera's local time is what it expects in the header
                    # even if it's not actually UTC. WS-Security is picky about the mismatch.
                    cam_utc = dt_obj(
                        local.Date.Year, local.Date.Month, local.Date.Day,
                        local.Time.Hour, local.Time.Minute, local.Time.Second,
                        tzinfo=timezone.utc
                    )
                else:
                    log.warning("Neither UTCDateTime nor LocalDateTime found on camera. Authentication may fail.")
                    return

            # Calculate drift
            now_utc = dt_obj.now(timezone.utc)
            diff = cam_utc - now_utc
            
            if abs(diff.total_seconds()) > 5:
                log.info("Time drift detected: %ds. Adjusting authentication headers.", diff.total_seconds())
                # For onvif-zeep, the offset should be applied to the camera's internal clock
                # or we just rely on the fact that some versions allow monkeypatching.
                # A more standard way in zeep is to adjust the timestamp in the security header, 
                # but onvif-zeep simplifies this.
                try:
                    self._cam.to_utc_timestamp = lambda: dt_obj.now(timezone.utc) + diff
                except Exception:
                    pass
        except Exception as e:
            log.warning("Failed to sync time with camera: %s", e)

    def _media_svc(self):
        if not self._media:
            self._media = self._connect().create_media_service()
        return self._media

    def _recording_svc(self):
        if not self._recording:
            self._recording = self._connect().create_recording_service()
        return self._recording

    def _replay_svc(self):
        if not self._replay:
            self._replay = self._connect().create_replay_service()
        return self._replay

    def _search_svc(self):
        if not self._search:
            self._search = self._connect().create_search_service()
        return self._search

    # ── Device info ───────────────────────────────────────────────────────

    def get_device_info(self) -> dict:
        dev = self._connect().create_devicemgmt_service()
        info = dev.GetDeviceInformation()
        return {
            "manufacturer": str(getattr(info, "Manufacturer", "")),
            "model":        str(getattr(info, "Model", "")),
            "firmware":     str(getattr(info, "FirmwareVersion", "")),
            "serial":       str(getattr(info, "SerialNumber", "")),
        }

    # ── Profiles / Channels ───────────────────────────────────────────────

    def get_profiles(self) -> list:
        """Fetch all video profiles metadata with resolution info."""
        try:
            profiles = self._media_svc().GetProfiles()
            result = []
            for p in profiles:
                token = str(getattr(p, "token", ""))
                name  = str(getattr(p, "Name", token))
                res   = ""
                
                # Try to find resolution in VideoEncoderConfiguration
                vec = getattr(p, "VideoEncoderConfiguration", None)
                if vec:
                    r = getattr(vec, "Resolution", None)
                    if r:
                        w = getattr(r, "Width", "")
                        h = getattr(r, "Height", "")
                        if w and h:
                            res = f"{w}x{h}"
                
                # Fallback for Media2 style resolution
                if not res:
                    cfg = getattr(p, "Configurations", None)
                    if cfg and hasattr(cfg, "VideoEncoder"):
                        ve = cfg.VideoEncoder
                        if hasattr(ve, "Resolution"):
                            w = getattr(ve.Resolution, "Width", "")
                            h = getattr(ve.Resolution, "Height", "")
                            if w and h:
                                res = f"{w}x{h}"
                
                result.append({"token": token, "name": name, "resolution": res})
            return result
        except Exception as e:
            log.warning("get_profiles failed: %s", e)
            return []

    # ── Live stream ───────────────────────────────────────────────────────

    def get_live_uri(self, profile_token: str | None = None) -> str:
        """
        Fetch the RTSP Live stream URI (Profile S).
        If profile_token is None, the first available profile is used.
        """
        media = self._media_svc()
        if not profile_token:
            profiles = self.get_profiles()
            if not profiles:
                raise Exception("No profiles found on device")
            profile_token = profiles[0]["token"]

        req = media.create_type("GetStreamUri")
        req.ProfileToken = profile_token
        req.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        result = media.GetStreamUri(req)
        return self.inject_credentials(str(result.Uri))

    # ── Recordings (Profile G) ────────────────────────────────────────────

    def get_recording_summary(self) -> dict:
        try:
            summary = self._recording_svc().GetRecordingSummary()
            return {
                "data_from":         _dt(getattr(summary, "DataFrom", None)),
                "data_until":        _dt(getattr(summary, "DataUntil", None)),
                "number_recordings": int(getattr(summary, "NumberRecordings", 0)),
            }
        except Exception as e:
            log.warning("GetRecordingSummary: %s", e)
            return {}

    def list_recordings(self) -> list:
        """Enumerate all recording tokens and their time ranges."""
        try:
            items = self._recording_svc().GetRecordings()
            result = []
            if not items or not hasattr(items, "RecordingItem"):
                return []
            for item in (items.RecordingItem or []):
                token = str(getattr(item, "RecordingToken", ""))
                try:
                    info = self._recording_svc().GetRecordingInformation({"RecordingToken": token})
                    ri = info.RecordingInformation
                    result.append({
                        "token":    token,
                        "earliest": _dt(getattr(ri, "EarliestRecording", None)),
                        "latest":   _dt(getattr(ri, "LatestRecording", None)),
                    })
                except Exception:
                    result.append({"token": token, "earliest": None, "latest": None})
            return result
        except Exception as e:
            log.warning("GetRecordings failed (ONVIF): %s. Tip: Ensure Profile G is supported.", e)
            return []

    # ── Hikvision ISAPI (Internal Storage / SD Card) ──────────────────────

    def list_hikvision_recordings(self, channel: int = 1) -> list:
        """
        Fetches recordings directly from Hikvision SD card via ISAPI.
        Used for TIER_B devices that lack ONVIF Profile G search.
        """
        url = f"http://{self.host}:{self.port}/ISAPI/ContentMgmt/search"
        # Track 101/102 etc.
        search_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <CMSearchDescription>
            <searchID>{quote(self.host)}</searchID>
            <trackList>
                <trackID>{channel * 100 + 1}</trackID>
            </trackList>
            <timeSpanList>
                <timeSpan>
                    <startTime>2000-01-01T00:00:00Z</startTime>
                    <endTime>{dt_obj.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>
                </timeSpan>
            </timeSpanList>
            <maxResults>50</maxResults>
        </CMSearchDescription>"""

        try:
            res = requests.post(
                url, 
                data=search_xml, 
                auth=HTTPDigestAuth(self.username, self.password),
                timeout=5
            )
            if res.status_code != 200:
                return []

            root = ET.fromstring(res.text)
            namespace = {'ns': 'http://www.isapi.org/ver20/XMLSchema'}
            recordings = []
            
            for match in root.findall('.//ns:searchMatchItem', namespace):
                start = match.find('.//ns:startTime', namespace)
                end   = match.find('.//ns:endTime', namespace)
                if start is not None and end is not None:
                    recordings.append({
                        "token": f"SD_CARD_{channel}",
                        "earliest": start.text,
                        "latest":   end.text,
                        "source":   "ISAPI"
                    })
            return recordings
        except Exception as e:
            log.error("Hikvision ISAPI search failed: %s", e)
            return []

    def find_recording_for_time(self, start_time: dt_obj) -> str | None:
        """
        Uses the Search Service to find which RecordingToken contains the given start_time.
        This is the 'correct' way to handle Profile G playback.
        """
        try:
            search = self._search_svc()
            # 1. Create a search filter
            search_filter = {
                "SearchScope": {
                    "IncludedSources": [],
                    "RecordingInformationFilter": f"Time >= {start_time.isoformat()}"
                },
                "MaxMatches": 1,
                "KeepAliveTime": "PT10S"
            }
            res = search.FindRecordings(search_filter)
            search_token = res.SearchToken
            
            # 2. Get results
            results = search.GetRecordingSearchResults({"SearchToken": search_token, "MaxResults": 1})
            if results and hasattr(results, "ResultList") and results.ResultList and len(getattr(results.ResultList, "RecordingSearchResult", [])) > 0:
                res_item = results.ResultList.RecordingSearchResult[0]
                if hasattr(res_item, "RecordingToken"):
                    return str(res_item.RecordingToken)
        except Exception as e:
            log.debug("ONVIF Search failed (falling back to enumeration): %s", e)
        
        # Fallback: Enumerate manually if Search service fails
        recs = self.list_recordings()
        for r in recs:
            if r["earliest"] and r["latest"]:
                try:
                    # Normalize both to UTC for safe comparison
                    e = dt_obj.fromisoformat(r["earliest"]).replace(tzinfo=timezone.utc)
                    l = dt_obj.fromisoformat(r["latest"]).replace(tzinfo=timezone.utc)
                    s = start_time.astimezone(timezone.utc)
                    if e <= s <= l:
                        return r["token"]
                except Exception:
                    continue
        return recs[0]["token"] if recs else None

    # ── Replay URI (Profile G) ────────────────────────────────────────────

    def get_replay_uri(self, recording_token: str) -> str:
        """
        Fetch the RTSP Replay stream URI (Profile G).
        Requires the NVR/Camera to support Profile G.
        """
        svc = self._replay_svc()
        req = svc.create_type("GetReplayUri")
        req.RecordingToken = recording_token
        req.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        result = svc.GetReplayUri(req)
        return self.inject_credentials(str(result.Uri))

    # ── Utility ───────────────────────────────────────────────────────────

    def inject_credentials(self, uri: str) -> str:
        """Embeds user:pass into the RTSP URI using URL encoding for special characters."""
        if not uri or not isinstance(uri, str):
            return ""
        try:
            parsed = urlparse(uri)
            host   = parsed.hostname or self.host
            port   = parsed.port or 554
            user   = quote(self.username)
            pwd    = quote(self.password)
            return str(urlunparse(parsed._replace(
                netloc=f"{user}:{pwd}@{host}:{port}"
            )))
        except Exception:
            return uri


def _dt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt_obj):
        return value.isoformat()
    return str(value)
