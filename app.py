import threading
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from video_uploader import upload_loop

t = threading.Thread(target=upload_loop, daemon=True)
t.start()

t.join()
