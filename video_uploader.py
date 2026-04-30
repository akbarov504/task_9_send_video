import os
import uuid
import time
import logging
import requests

from config import (
    API_BASE_STREAM,
    UPLOAD_BATCH_SIZE,
    UPLOAD_CYCLE_INTERVAL,
    MIN_VIDEO_AGE_SECONDS,
    RETRY_INTERVAL_SECONDS,
)
from token_manager import get_valid_token
from db import (
    get_unuploaded_videos,
    mark_uploaded,
    increment_retry,
    init_db,
)

logger = logging.getLogger(__name__)

UPLOAD_URL_ENDPOINT   = f"{API_BASE_STREAM}/google-cloud-storage/upload-url"
VIDEO_NOTIFY_ENDPOINT = f"{API_BASE_STREAM}/video/upload/v2"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _auth_headers() -> dict:
    """Return Bearer-token headers. Raises if no valid token available."""
    token = get_valid_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _generate_filename() -> str:
    """Return a globally-unique .mp4 filename."""
    return f"{uuid.uuid4()}.mp4"


def _check_internet(timeout: int = 5) -> bool:
    """Quick connectivity check via socket — works without HTTP server."""
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except (socket.error, OSError):
        return False


# ─────────────────────────────────────────────
# Upload steps
# ─────────────────────────────────────────────

def _get_signed_upload_url(file_name: str, headers: dict) -> str:
    """
    POST /api/google-cloud-storage/upload-url
    Body: { "fileName": "<uuid>.mp4" }
    Returns signed GCS URL (valid for 15 minutes).
    """
    response = requests.post(
        UPLOAD_URL_ENDPOINT,
        json={"fileName": file_name},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    url = data.get("url")
    if not url:
        raise ValueError(f"No 'url' in response: {data}")
    logger.info(f"[UPLOADER] Signed URL received for {file_name}")
    return url


def _upload_to_gcs(signed_url: str, file_path: str) -> None:
    """
    PUT video bytes directly to signed GCS URL.
    Must be called immediately after _get_signed_upload_url (URL expires in 15 min).
    No auth headers needed — signature is embedded in the URL.
    """
    file_size = os.path.getsize(file_path)
    logger.info(f"[UPLOADER] Uploading to GCS: {file_path} ({file_size} bytes)")

    with open(file_path, "rb") as f:
        response = requests.put(
            signed_url,
            data=f,
            headers={"Content-Type": "video/mp4"},
            timeout=300,
        )
    response.raise_for_status()
    logger.info(f"[UPLOADER] GCS upload done. HTTP {response.status_code}")


def _notify_backend(
    file_name: str,
    start_time: str,
    end_time: str,
    global_video_id: str,
    camera_type: str,
    headers: dict,
) -> None:
    """
    POST /api/video/upload/v2
    Tells backend the video is ready in GCS.
    """
    payload = {
        "fileName":      file_name,
        "startTime":     start_time,
        "endTime":       end_time,
        "globalVideoId": global_video_id,
        "format":        "P1080",
        "cameraType":    camera_type,
    }
    logger.info(f"[UPLOADER] Notifying backend: {payload}")
    response = requests.post(
        VIDEO_NOTIFY_ENDPOINT,
        json=payload,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    logger.info(f"[UPLOADER] Backend notified. HTTP {response.status_code}")


# ─────────────────────────────────────────────
# Single video pipeline
# ─────────────────────────────────────────────

def upload_video(video_row: tuple) -> bool:
    """
    Full pipeline for one video:
      1. Get signed URL  (POST)
      2. Upload to GCS   (PUT)  ← must happen within 15 min of step 1
      3. Notify backend  (POST)
      4. Mark uploaded in DB

    Returns True on success, False on any failure (retry is handled by caller).
    Row format: (id, file_path, camera_type, start_time, end_time, globalVideoId)
    """
    video_id, file_path, camera_type, start_time, end_time, global_video_id = video_row

    if not os.path.exists(file_path):
        logger.warning(f"[UPLOADER] File not found, skipping: {file_path}")
        increment_retry(video_id)
        return False

    try:
        headers   = _auth_headers()
        file_name = _generate_filename()

        # Step 1 — get signed URL
        signed_url = _get_signed_upload_url(file_name, headers)

        # Step 2 — upload to GCS immediately (URL valid 15 min)
        _upload_to_gcs(signed_url, file_path)

        # Step 3 — tell backend it's ready
        _notify_backend(
            file_name       = file_name,
            start_time      = start_time,
            end_time        = end_time,
            global_video_id = global_video_id,
            camera_type     = camera_type,
            headers         = headers,
        )

        # Step 4 — mark done in DB
        mark_uploaded(video_id)

        # Step 5 — delete local file (already safe in GCS, free up disk space)
        try:
            os.remove(file_path)
            logger.info(f"[UPLOADER] ✓ video_id={video_id} uploaded as {file_name} — local file deleted.")
        except OSError as e:
            # Not critical — file might already be gone
            logger.warning(f"[UPLOADER] Could not delete local file {file_path}: {e}")

        return True

    except requests.exceptions.RequestException as e:
        logger.error(f"[UPLOADER] Network error (video_id={video_id}): {e}")
        increment_retry(video_id)
        return False
    except Exception as e:
        logger.error(f"[UPLOADER] Error (video_id={video_id}): {e}")
        increment_retry(video_id)
        return False


# ─────────────────────────────────────────────
# Upload cycle
# ─────────────────────────────────────────────

def run_upload_cycle() -> None:
    """
    Pull up to UPLOAD_BATCH_SIZE pending videos from DB and upload them.
    Skips the whole cycle silently if there is no internet.
    """
    if not _check_internet():
        logger.warning("[UPLOADER] No internet — skipping cycle.")
        return

    videos = get_unuploaded_videos(
        limit           = UPLOAD_BATCH_SIZE,
        min_age_seconds = MIN_VIDEO_AGE_SECONDS,
        retry_interval  = RETRY_INTERVAL_SECONDS,
    )

    if not videos:
        logger.debug("[UPLOADER] No pending videos.")
        return

    logger.info(f"[UPLOADER] Cycle started: {len(videos)} video(s)")
    for video_row in videos:
        upload_video(video_row)


# ─────────────────────────────────────────────
# Background loop  (run in a daemon thread)
# ─────────────────────────────────────────────

def upload_loop() -> None:
    """
    Calls run_upload_cycle() every UPLOAD_CYCLE_INTERVAL seconds.
    Run this in a background thread:

        import threading
        from video_uploader import upload_loop

        t = threading.Thread(target=upload_loop, daemon=True)
        t.start()
    """
    logger.info("[UPLOADER] Upload loop started.")
    init_db()
    while True:
        try:
            run_upload_cycle()
        except Exception as e:
            logger.error(f"[UPLOADER] Unexpected loop error: {e}")
        time.sleep(UPLOAD_CYCLE_INTERVAL)
