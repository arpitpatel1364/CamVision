# CamVision - Technical Documentation

This project is a high-performance **ONVIF Media Server** designed to bridge IP cameras/NVRs and web browsers. It provides a central API to manage cameras, stream live video, and fetch recorded footage as time-bounded MP4 chunks without storing any video on the hub itself.

---

##  System Architecture

The project follows a **Tier-based capability model** to handle the vast fragmentation in the IP camera industry:

| Tier | Name | Capabilities | Replay Method |
| :--- | :--- | :--- | :--- |
| **Tier A** | `PROFILE_G` | Full ONVIF Profile G support | Standard ONVIF Replay Service |
| **Tier B** | `RTSP_FALLBACK` | Basic ONVIF + Manufacturer RTSP | Vendor-specific RTSP URI patterns (e.g., Hikvision, Dahua) |
| **Tier C** | `LIVE_ONLY` | Basic ONVIF Profile S | Live streaming only (no playback) |

---

##  Chunk Fetch Methodology

The "Chunk Fetch" method is the core mechanism for retrieving recorded footage. Instead of downloading a full video file, the system pulls a precisely timed segment (default 30s) directly from the camera's storage.

1.  **Capability Check**: The system checks if the device is **Tier A** (ONVIF Profile G) or **Tier B** (Manufacturer RTSP).
2.  **URI Construction**: 
    -   **Tier A**: Fetches a Replay URI via the ONVIF Replay Service.
    -   **Tier B**: Builds a vendor-specific RTSP URI (e.g., Hikvision, Dahua) that embeds the start and end times in the query parameters.
3.  **Transmuxing**: An asynchronous `ffmpeg` process is spawned. It uses `-ss` for fast/precise seeking and `-t` to limit the duration.
4.  **Streaming**: The video is transmuxed into **fragmented MP4** (`frag_keyframe+empty_moov`) and yielded byte-by-byte to the browser. This allows the video to start playing immediately in a standard `<video>` tag without a full download.

---

##  File & Function Reference

### 1. [main.py](main.py)
The entry point and API Orchestrator. Uses **FastAPI** for high-performance async I/O.

#### Functions:
- [_load()](main.py), [_save(data)](main.py): Internal helpers for managing the `cameras.json` flat-file database.
- [_get_cam(cam_id)](main.py): Fetches a camera from the registry or raises a 404 error.
- [_make_client(cam)](main.py): Instantiates an [ONVIFClient](onvif_client.py) using stored credentials.
- [_parse_dt(s)](main.py): Utility to convert ISO 8601 strings into timezone-aware Python `datetime` objects.
- [list_cameras()](main.py): API endpoint (`GET /cameras`) to list all registered devices.
- [add_camera(body)](main.py): API endpoint (`POST /cameras`) that probes and registers a new camera.
- [remove_camera(cam_id)](main.py): API endpoint (`DELETE /cameras/{id}`).
- [camera_info(cam_id)](main.py): API endpoint (`GET /cameras/{id}/info`) returning metadata without credentials.
- [get_channels(cam_id)](main.py): API endpoint (`GET /cameras/{id}/channels`).
- [live_uri(cam_id, profile_token)](main.py): API endpoint (`GET /cameras/{id}/live-uri`).
- [list_recordings(cam_id)](onvif_client.py): API endpoint (`GET /cameras/{id}/recordings`).
- [get_chunk(...)](main.py): API endpoint (`GET /cameras/{id}/chunk`) - The main streaming generator.

#### Rationale:
- **FastAPI / Uvicorn**: Chosen for its native support for `StreamingResponse` and `asyncio`, which is critical when piping multiple [ffmpeg](chunk_streamer.py) streams simultaneously.
- **In-memory Registry (`CAMERAS` + `cameras.json`)**: Simple persistence for lightweight deployments. Avoids the overhead of a database (PostgreSQL/MongoDB) while keeping everything portable.

#### Alternatives:
- Use **Redis** for the camera registry if scaling to a distributed environment with multiple hub instances.
- Integrate **WebRTC** for lower-latency live streaming (though significantly more complex than RTSP).

---

### 2. [onvif_client.py](onvif_client.py)
A robust wrapper around the `python-onvif-zeep` library.

#### Functions:
- [_connect()](onvif_client.py), [_media_svc()](onvif_client.py), [_recording_svc()](onvif_client.py), [_replay_svc()](onvif_client.py): Lazy-initialization helpers for ONVIF SOAP services.
- [get_device_info()](onvif_client.py): Fetches Manufacturer, Model, and Firmware via `GetDeviceInformation`.
- [get_profiles()](onvif_client.py): Extracts video profiles (tokens, names, resolutions).
- [get_live_uri(profile_token)](onvif_client.py): Fetches the Profile S live stream RTSP URI.
- [get_recording_summary()](onvif_client.py): Gets the total recording range (min/max time) from the device.
- [list_recordings()](onvif_client.py): Enumerates recording tokens and their metadata.
- [get_replay_uri(recording_token)](onvif_client.py): Fetches the Profile G replay RTSP URI.
- [inject_credentials(uri)](onvif_client.py): Rewrites an RTSP URI to include `user:pass` so tools like [ffmpeg](chunk_streamer.py) can authenticate.
- [_dt(value)](onvif_client.py): Internal helper to normalize datetime objects for JSON serialization.

#### Rationale:
- **Lazy Connection**: Services (Media, Recording, Replay) are only instantiated when needed to save memory and avoid expensive SOAP handshake overhead on every request.
- **Credential Injection**: Automatically embeds `user:pass@` into URIs because many cameras return "naked" URIs that the hub must authorize for [ffmpeg](chunk_streamer.py).

#### Alternatives:
- Write a raw XML SOAP client to replace `onvif-zeep`, reducing dependencies and potentially improving speed (though increasing code complexity).

---

### 3. [device_detector.py](device_detector.py)
The "brain" of the hub. It resolves the "Tier" and "Brand" of a device.

#### Functions:
- [detect(host, port, user, pass)](device_detector.py): The main entry point for device discovery. Probes services and assigns a Capability Tier.
- [_fingerprint(manufacturer, model)](device_detector.py): Pattern-matches device strings to determine the brand key and device type (NVR vs IP CAM).
- [_parse_profiles(raw_profiles)](device_detector.py): Cleans up complex SOAP profile objects into simple dictionaries.
- [_find_url(raw_caps, service_name)](device_detector.py): Recursively searches the device's capability tree for service endpoints (Media, Recording, etc.).
- [_caps_to_dict(raw_caps)](device_detector.py): Converts Zeep/Zesoap objects into standard Python dictionaries.

#### Rationale:
- **Probing > Manual Config**: Most users don't know if their camera supports "Profile G". Auto-detection makes the system "just work".
- **Fingerprinting**: Crucial because many devices (especially DVRs/XVRs) say they support ONVIF but lack the standard Recording service, necessitating the `Tier B` fallback.

#### Alternatives:
- Use **Nmap/OUI lookups** to identify brands by MAC address hardware vendors if ONVIF information is stripped by the manufacturer.

---

### 4. [chunk_streamer.py](chunk_streamer.py)
The video processing engine. Powered by **ffmpeg**.

#### Functions:
- [_build_cmd(...)](chunk_streamer.py): Generates the exact [ffmpeg](chunk_streamer.py) command-line arguments for the requested chunk.
- [stream_chunk(...)](chunk_streamer.py): Async generator that manages the [ffmpeg](chunk_streamer.py) subprocess lifecycle and yields video bytes.
- [pick_seek_mode(tier, start_time)](chunk_streamer.py): Decides whether to use `input` seek (fast) or `output` seek (accurate) based on the device tier.
- [iso_to_ffmpeg_offset(start, reference)](chunk_streamer.py): Calculates the "Seconds from start" offset required for Profile G replay sessions.

#### Rationale:
- **Fragmented MP4 (`-movflags frag_keyframe+empty_moov`)**: This is the "magic" that allows `video/mp4` to be streamed over HTTP without the browser needing to wait for the entire file to download. It makes the video playable immediately.
- **No Disk Writing**: Writing video to SSD/HDD would cause high wear and latency. Piping through RAM is significantly faster and more secure.

#### Alternatives:
- Use **GStreamer** instead of [ffmpeg](chunk_streamer.py). GStreamer is more modular but has a steeper learning curve and fewer pre-built "recipes" for camera playback.

---

### 5. [rtsp_fallback.py](rtsp_fallback.py)
A library of reverse-engineered RTSP playback URI patterns.

#### Functions:
- [build_playback_uri(brand, ...)](rtsp_fallback.py): Dispatcher that selects the correct URI builder based on the detected brand.
- [_hikvision()](rtsp_fallback.py), [_dahua()](rtsp_fallback.py), [_tvt()](rtsp_fallback.py), [_uniview()](rtsp_fallback.py), [_hanwha()](rtsp_fallback.py), [_reolink()](rtsp_fallback.py): Brand-specific URI constructors.
- [_hiktime()](rtsp_fallback.py), [_dahuatime()](rtsp_fallback.py): Formatters for building the timestamp strings required by various manufacturer RTSP implementations.
- [channel_from_profile_name(name)](rtsp_fallback.py): A regex helper that extracts the 1-based channel number from arbitrary ONVIF profile names (e.g., "Channel 01" -> 1).
- [_generic()](rtsp_fallback.py): A fallback builder that attempts Hikvision-style paths (the most common OEM base).

#### Rationale:
- Essential for supporting older or cheaper hardware (DVRs/XVRs) that have proprietary playback even if they ignore the ONVIF Recording standard.

#### Alternatives:
- Implement the **Manufacturer's SDK** (C++ / Python wrappers) instead of RTSP. This would be more reliable but would require separate drivers for every brand. RTSP is a "universal" fallback.

---

### 6. [index.html](index.html) (The Web UI)
A single-page application (SPA) built with vanilla JavaScript and CSS.

#### Features:
- **Device Management**: UI for adding/removing cameras with real-time detection feedback.
- **Tabbed Interface**:
  - **Playback**: A custom video player for fetching and stepping through 30-120s chunks.
  - **Live URI**: Generates RTSP URIs for external players like VLC.
  - **Recordings**: Lists available Profile G recording slots.
  - **Info**: Displays full ONVIF metadata.

#### Rationale:
- **Vanilla JS**: No frameworks (React/Vue) were used to ensure the project remains extremely lightweight and has zero build-time dependencies.
- **Fragmented MP4 Support**: Relies on the browser's native `<video>` tag ability to play fragmented MP4 streams provided by the backend.

---

### 7. [Dockerfile](Dockerfile) & [docker-compose.yml](docker-compose.yml)
Containerization configuration.

#### Rationale:
- **[ffmpeg](chunk_streamer.py) dependency**: Installing [ffmpeg](chunk_streamer.py) correctly can be difficult across different OS distros. Docker encapsulates this dependency, ensuring the hub works regardless of the host OS.
- **`network_mode: host`**: (Often used in camera apps) Allows the container to discover cameras on the local network more easily, appearing as if it's running directly on the host.

#### Alternatives:
- Use **Kubernetes** with a sidecar pattern if managing hundreds of hubs across different physical sites.

---

## Operational Guide

### Why is this part here?
- **[Dockerfile](Dockerfile) / [docker-compose.yml](docker-compose.yml)**: Designed for "one-click" deployment. [ffmpeg](chunk_streamer.py) can be tricky to install with the right codecs; Docker ensures a consistent environment.
- **[requirements.txt](requirements.txt)**: Pins versions of `fastapi`, `uvicorn`, and `onvif-zeep` to prevent breaking changes in upstream libraries.

### What can we do instead?
1. **Front-end Playback**: Currently, [index.html](index.html) (the UI) uses simple `<video>` tags. For smoother seeking, we could implement **HLS** or **DASH** on the server, though this would introduce 5-10 seconds of latency.
2. **Security**: The current hub uses a simple `cameras.json`. In a production environment, we should move to **OAuth2 / JWT** for API security and encrypt the camera passwords at rest using a vault.

---

## Troubleshooting & Testing

### Direct Playback Test (OpenCV)
If you are having trouble seeing video in the web UI, you can test the RTSP stream directly using **OpenCV**. This bypasses the hub's transcoding and helps isolate network/credential issues.

```python
import cv2

# Replace with the URI generated by /live-uri or /chunk (for Profile G)
rtsp_url = "rtsp://user:pass@192.168.1.XXX:554/live"

cap = cv2.VideoCapture(rtsp_url)
while True:
    ret, frame = cap.read()
    if not ret: 
        print("Failed to fetch frame")
        break
    cv2.imshow('ONVIF Video Test', frame)
    if cv2.waitKey(1) & 0xFF == ord('q'): 
        break

cap.release()
cv2.destroyAllWindows()
```

### Common Issues
1. **Profile G Support**: Not all "ONVIF" cameras support Profile G (Recordings). Use the `/info` endpoint to check the `tier`. If it says `LIVE_ONLY`, playback won't work.
2. **Replay URI Token**: Some NVRs require a specific `RecordingToken`. The hub tries to auto-detect this, but you can find it manually using the `/recordings` endpoint.
3. **Special Characters**: If your camera password contains `@` or `:`, the hub now automatically URL-encodes these for the RTSP URI (e.g., `@` becomes `%40`).
