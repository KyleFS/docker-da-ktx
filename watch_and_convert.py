#!/usr/bin/env python3
"""Watch a directory of Dungeon Alchemist batch exports and convert images to KTX2.

watchdog reports file changes under WATCH_DIR. Once a source image has gone
STABILITY_SECONDS without a new event (i.e. it has finished being written),
it is converted to a .ktx2 sibling with `toktx` from KTX-Software. Files
already present at startup are converted too, unless their .ktx2 is up to date.

Configuration is via environment variables (set these in Docker).
"""

import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/data"))
EXTENSIONS = {
    "." + e.strip().lower().lstrip(".")
    for e in os.environ.get("SOURCE_EXTENSIONS", "png,jpg,jpeg,tga,bmp").split(",")
    if e.strip()
}
TOKTX_ARGS = shlex.split(os.environ.get("TOKTX_ARGS", "--t2 --genmipmap --encode uastc --zcmp 19"))
STABILITY_SECONDS = float(os.environ.get("STABILITY_SECONDS", "2"))
# Native filesystem events don't always cross Docker bind mounts; polling always works.
USE_POLLING = os.environ.get("USE_POLLING", "false").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("da_ktx_watch")

pending: dict[Path, float] = {}  # src -> time of its most recent event
lock = threading.Lock()


def mark(path: Path) -> None:
    if path.suffix.lower() in EXTENSIONS:
        with lock:
            pending[path] = time.monotonic()


class Handler(FileSystemEventHandler):
    def on_any_event(self, event):
        if event.is_directory or event.event_type not in ("created", "modified", "moved", "closed"):
            return
        mark(Path(event.dest_path or event.src_path))  # dest_path is only set for moves


def convert(src: Path) -> None:
    dest = src.with_suffix(".ktx2")
    try:
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            return  # already up to date
    except OSError:
        return  # source vanished
    log.info("Converting %s", src)
    result = subprocess.run(["toktx", *TOKTX_ARGS, str(dest), str(src)], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("toktx failed (exit %s) for %s\n%s", result.returncode, src, result.stderr.strip())
        dest.unlink(missing_ok=True)


def main() -> None:
    if not WATCH_DIR.is_dir():
        raise SystemExit(f"WATCH_DIR does not exist or is not a directory: {WATCH_DIR}")

    observer = PollingObserver() if USE_POLLING else Observer()
    observer.schedule(Handler(), str(WATCH_DIR), recursive=True)
    observer.start()
    log.info("Watching %s for %s (polling=%s)", WATCH_DIR, sorted(EXTENSIONS), USE_POLLING)

    for path in WATCH_DIR.rglob("*"):
        mark(path)

    try:
        while True:
            time.sleep(1)
            now = time.monotonic()
            with lock:
                ready = [p for p, t in pending.items() if now - t >= STABILITY_SECONDS]
                for p in ready:
                    del pending[p]
            for p in ready:
                convert(p)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
