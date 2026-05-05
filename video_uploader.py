import os
import time
import logging
import requests
import subprocess

from core.config import (
    API_BASE_STREAM,
    UPLOAD_BATCH_SIZE,
    UPLOAD_CYCLE_INTERVAL,
    MIN_VIDEO_AGE_SECONDS,
    RETRY_INTERVAL_SECONDS,
    TASK_7_VERTUAL_PATH,
)
from utils.token_manager import get_valid_token
from core.db import (
    get_unuploaded_videos,
    mark_uploaded,
    increment_retry,
    init_db,
)

from datetime import datetime

logger = logging.getLogger(__name__)

UPLOAD_URL_ENDPOINT   = f"{API_BASE_STREAM}/google-cloud-storage/upload-url"
VIDEO_NOTIFY_ENDPOINT = f"{API_BASE_STREAM}/video/upload/v2"

def _auth_headers() -> dict:
    token = get_valid_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _generate_filename(type: str, camera_type: str, start_time, global_id, ext: str = "mp4") -> str:
    start_time_one = start_time[:10]
    return f"safety/{type}/{camera_type.lower()}/50/{start_time_one}/{global_id}{start_time}.{ext}"

def _check_internet(timeout: int = 5) -> bool:
    import socket
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except (socket.error, OSError):
        return False

def _calc_duration(start_time: str, end_time: str) -> int:
    from datetime import datetime
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        delta = datetime.strptime(end_time, fmt) - datetime.strptime(start_time, fmt)
        return max(1, int(delta.total_seconds()))
    except Exception:
        return 10

def _extract_first_frame(video_path: str, output_path: str) -> bool:
    """
    Extract first frame from video as .webp:
      1. ffmpeg extracts first frame as temp .jpg
      2. Pillow converts .jpg → .webp
      3. Temp .jpg deleted
    """
    from PIL import Image
    jpg_path = output_path.replace(".webp", "_tmp.jpg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vframes", "1", "-q:v", "2", jpg_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if result.returncode != 0 or not os.path.exists(jpg_path):
            err = result.stderr.decode(errors="ignore").strip().splitlines()
            logger.warning(f"[UPLOADER] ffmpeg frame extraction failed: {err[-1] if err else 'unknown'}")
            return False

        with Image.open(jpg_path) as img:
            img.save(output_path, "WEBP", quality=80)

        logger.info(f"[UPLOADER] Screenshot saved: {output_path}")
        return True

    except Exception as e:
        logger.warning(f"[UPLOADER] Screenshot extraction failed: {e}")
        return False
    finally:
        if os.path.exists(jpg_path):
            try:
                os.remove(jpg_path)
            except OSError:
                pass

def _get_signed_upload_url(file_name: str, headers: dict) -> str:
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

def _upload_to_gcs(signed_url: str, file_path: str, content_type: str) -> None:
    file_size = os.path.getsize(file_path)
    logger.info(f"[UPLOADER] Uploading to GCS: {file_path} ({file_size} bytes)")
    with open(file_path, "rb") as f:
        response = requests.put(
            signed_url,
            data=f,
            headers={"Content-Type": content_type},
            timeout=300,
        )
    response.raise_for_status()
    logger.info(f"[UPLOADER] GCS upload done. HTTP {response.status_code}")

def _notify_backend(
    file_name: str,
    file_path: str,
    thumbnail_key: str,
    start_time: str,
    end_time: str,
    global_video_id: str,
    camera_type: str,
    headers: dict,
) -> None:
    filesize = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    duration = _calc_duration(start_time, end_time)

    payload = {
        "videoKey":      file_name,
        "thumbnailKey":  thumbnail_key,
        "startTime":     start_time + "Z",
        "endTime":       end_time + "Z",
        "globalVideoId": global_video_id,
        "format":        "P1080",
        "cameraType":    camera_type + "SIDE",
        "filesize":      filesize,
        "duration":      duration,
        "extension":     "mp4",
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

def upload_video(video_row: tuple) -> bool:
    """
    Full pipeline for one video:
      1. Extract first frame as .webp screenshot (ffmpeg)
      2. Upload video to GCS
      3. Upload screenshot to GCS
      4. Notify backend with videoKey + thumbnailKey
      5. Mark uploaded in DB
      6. Delete local video + screenshot files
    """
    video_id, file_path, camera_type, start_time, end_time, global_video_id = video_row
    file_path = os.path.join(TASK_7_VERTUAL_PATH, file_path)

    if not os.path.exists(file_path):
        logger.warning(f"[UPLOADER] File not found, skipping: {file_path}")
        increment_retry(video_id)
        return False

    screenshot_path = file_path.rsplit(".", 1)[0] + "_thumb.webp"

    try:
        headers       = _auth_headers()
        video_key     = _generate_filename("video", camera_type+"SIDE", start_time, global_video_id, "mp4")
        thumbnail_key = None

        has_thumbnail = _extract_first_frame(file_path, screenshot_path)

        video_signed_url = _get_signed_upload_url(video_key, headers)
        _upload_to_gcs(video_signed_url, file_path, content_type="video/mp4")

        if has_thumbnail and os.path.exists(screenshot_path):
            thumbnail_key = _generate_filename("screenshot", camera_type+"SIDE", start_time, global_video_id, "webp")
            thumb_signed_url = _get_signed_upload_url(thumbnail_key, headers)
            _upload_to_gcs(thumb_signed_url, screenshot_path, content_type="image/webp")
        else:
            logger.warning(f"[UPLOADER] No thumbnail for video_id={video_id}, skipping thumbnail upload.")

        _notify_backend(
            file_name       = video_key,
            file_path       = file_path,
            thumbnail_key   = thumbnail_key,
            start_time      = start_time,
            end_time        = end_time,
            global_video_id = global_video_id,
            camera_type     = camera_type,
            headers         = headers,
        )

        mark_uploaded(video_id)

        for path in [file_path, screenshot_path]:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"[UPLOADER] Deleted local file: {path}")
            except OSError as e:
                logger.warning(f"[UPLOADER] Could not delete {path}: {e}")

        logger.info(f"[UPLOADER] video_id={video_id} done. video={video_key} thumbnail={thumbnail_key}")
        return True

    except requests.exceptions.RequestException as e:
        logger.error(f"[UPLOADER] Network error (video_id={video_id}): {e}")
        increment_retry(video_id)
        return False
    except Exception as e:
        logger.error(f"[UPLOADER] Error (video_id={video_id}): {e}")
        increment_retry(video_id)
        return False
    finally:
        if os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
            except OSError:
                pass

def run_upload_cycle() -> None:
    if not _check_internet():
        logger.warning("[UPLOADER] No internet — skipping cycle.")
        return

    videos = get_unuploaded_videos(
        limit           = UPLOAD_BATCH_SIZE,
        min_age_seconds = MIN_VIDEO_AGE_SECONDS,
        retry_interval  = RETRY_INTERVAL_SECONDS,
    )

    if not videos:
        from core.db import DB_PATH
        import sqlite3
        from datetime import datetime, timedelta, timezone
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM videos WHERE uploaded=0")
            total = c.fetchone()[0]
            c.execute("SELECT id, start_time, last_try FROM videos WHERE uploaded=0 LIMIT 3")
            samples = c.fetchall()
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=MIN_VIDEO_AGE_SECONDS)).isoformat()
        retry_thr = (datetime.now(timezone.utc) - timedelta(seconds=RETRY_INTERVAL_SECONDS)).isoformat()
        logger.debug(f"[UPLOADER] No pending videos. Total unuploaded={total}, threshold={threshold}, retry_threshold={retry_thr}, samples={samples}")
        return

    logger.info(f"[UPLOADER] Cycle started: {len(videos)} video(s)")
    for video_row in videos:
        upload_video(video_row)

def upload_loop() -> None:
    """
    Calls run_upload_cycle() every UPLOAD_CYCLE_INTERVAL seconds.

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
