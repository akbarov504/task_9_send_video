import threading
from video_uploader import upload_loop

threading.Thread(target=upload_loop, daemon=True).start()
