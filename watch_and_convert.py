#!/usr/bin/env python3
"""Watch a directory of Dungeon Alchemist batch exports and convert them for Foundry.

watchdog reports file changes under INPUT_DIR. Once a source has gone
STABILITY_SECONDS without a new event (i.e. it has finished being written), it
is converted into OUTPUT_DIR at the same relative path, with backslashes taken
as directory separators (see paths.py). Source files are never
changed or removed. An output only appears once complete and is stamped with its
source's mtime, and a source is converted unless its output carries a matching
one. Files already present at startup go through the same check.

    images     converted to .ktx2, see ktx.py
    .json      each map's floors combined into a Foundry scene, see scene.py

A scene is only replaced when one of its floors' .json is newer, so later edits
to it survive restarts.

Everything created under OUTPUT_DIR, directories included, gets mode OUTPUT_MODE.

Configuration is via environment variables (set these in Docker).
"""

import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

import ktx
import paths
import scene

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/mnt/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/mnt/output"))
STABILITY_SECONDS = float(os.environ.get("STABILITY_SECONDS", "2"))
# Native filesystem events don't always cross Docker bind mounts; polling always works.
USE_POLLING = os.environ.get("USE_POLLING", "false").lower() in ("1", "true", "yes")
# The two mounts can store timestamps at different precision (down to 2s on FAT).
MTIME_TOLERANCE_NS = 2_000_000_000
# Set with chmod, as a mode passed at creation would be masked by the umask.
OUTPUT_MODE = 0o774

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("da_ktx_watch")

# An image, or a scene's path under INPUT_DIR (which no file has) -> time of its
# most recent event. A scene's floors share it, so it waits for all of them.
pending: dict[Path, float] = {}
lock = threading.Lock()


def mark(path: Path) -> None:
    if ktx.is_image(path):
        key = path
    elif scene.is_base_json(path):
        key = scene.scene_of(path)
    else:
        return
    with lock:
        pending[key] = time.monotonic()


class Handler(FileSystemEventHandler):
    def on_any_event(self, event):
        if event.is_directory or event.event_type not in ("created", "modified", "moved", "closed"):
            return
        mark(Path(event.dest_path or event.src_path))  # dest_path is only set for moves


def make_dirs(path: Path) -> None:
    missing = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        directory.chmod(OUTPUT_MODE)


def process(key: Path) -> None:
    is_image = ktx.is_image(key)
    dest = OUTPUT_DIR / paths.relative(key, INPUT_DIR)
    if is_image:
        dest = dest.with_suffix(".ktx2")
        sources = [key]
    else:
        sources = scene.floors_of(key)
    try:
        src_mtime = max(p.stat().st_mtime_ns for p in sources)
        if dest.exists():
            age = src_mtime - dest.stat().st_mtime_ns
            if (abs(age) if is_image else age) < MTIME_TOLERANCE_NS:
                return  # already up to date
    except (OSError, ValueError):
        return  # source vanished
    try:
        make_dirs(dest.parent)
    except OSError as e:
        log.error("Could not create %s: %s", dest.parent, e)
        return
    # Written beside dest and renamed once complete, so work interrupted partway
    # never leaves a partial file at dest. Keeps the .ktx2 suffix for toktx.
    tmp = dest.with_name(f".{dest.name}")
    if not (ktx.convert(key, tmp) if is_image else scene.write_scene(key, sources, INPUT_DIR, tmp)):
        tmp.unlink(missing_ok=True)
        return
    try:
        tmp.chmod(OUTPUT_MODE)
        tmp.replace(dest)
        os.utime(dest, ns=(src_mtime, src_mtime))
    except OSError as e:
        log.error("Could not finish %s: %s", dest, e)


def main() -> None:
    for name, directory in (("INPUT_DIR", INPUT_DIR), ("OUTPUT_DIR", OUTPUT_DIR)):
        if not directory.is_dir():
            raise SystemExit(f"{name} does not exist or is not a directory: {directory}")

    observer = PollingObserver() if USE_POLLING else Observer()
    observer.schedule(Handler(), str(INPUT_DIR), recursive=True)
    observer.start()
    log.info("Watching %s for %s and base .json, writing to %s (polling=%s)", INPUT_DIR, sorted(ktx.EXTENSIONS), OUTPUT_DIR, USE_POLLING)
    log.info(
        "Levels %s high, backgrounds under %s, background overrides: %s, grid overrides: %s",
        scene.LEVEL_HEIGHT, scene.FOUNDRY_ASSET_DIR or "the .json's img",
        scene.BACKGROUND_OVERRIDES or "none", scene.GRID_OVERRIDES or "none",
    )

    for path in INPUT_DIR.rglob("*"):
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
                process(p)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
