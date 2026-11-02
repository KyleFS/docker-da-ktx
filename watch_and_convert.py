#!/usr/bin/env python3
"""Watch a directory of Dungeon Alchemist batch exports and convert images to KTX2.

watchdog reports file changes under INPUT_DIR. Once a source image has gone
STABILITY_SECONDS without a new event (i.e. it has finished being written),
it is converted to a .ktx2 at the same relative path under OUTPUT_DIR with
`toktx` from KTX-Software. Source files are never changed or removed. A .ktx2
only appears once complete and is stamped with its source's mtime, and a source
is converted unless its .ktx2 carries a matching one. Files already present at
startup go through the same check.

Each image has a matching .json beside it. Those of the base images are copied
to the same relative path under OUTPUT_DIR, whereas those of the time-of-day
and weather variants (matched by VARIANT_PATTERN) are skipped:

    Map_00.json                 base, one per level
    Map_Evening_00.json         variant
    Map_Snow_Evening_00.json    variant

A copy is only replaced by a newer source, so later edits to it survive restarts.

Configuration is via environment variables (set these in Docker).
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/mnt/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/mnt/output"))
EXTENSIONS = {
    "." + e.strip().lower().lstrip(".")
    for e in os.environ.get("SOURCE_EXTENSIONS", "png,jpg,jpeg").split(",")
    if e.strip()
}
# Tested against the filename stem.
VARIANT_PATTERN = re.compile(
    os.environ.get("VARIANT_PATTERN", r"_(Morning|Noon|Evening|Night|Snow)(_\d+)?$"),
    re.IGNORECASE,
)
TOKTX_ARGS = shlex.split(
    os.environ.get(
        "TOKTX_ARGS",
        "--encode uastc --uastc_quality 2 --uastc_rdo_l 1.0 --zcmp 19 --genmipmap --target_type RGB",
    )
)
STABILITY_SECONDS = float(os.environ.get("STABILITY_SECONDS", "2"))
# Native filesystem events don't always cross Docker bind mounts; polling always works.
USE_POLLING = os.environ.get("USE_POLLING", "false").lower() in ("1", "true", "yes")
# The two mounts can store timestamps at different precision (down to 2s on FAT).
MTIME_TOLERANCE_NS = 2_000_000_000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("da_ktx_watch")

pending: dict[Path, float] = {}  # src -> time of its most recent event
lock = threading.Lock()


def is_base_json(path: Path) -> bool:
    return path.suffix.lower() == ".json" and not VARIANT_PATTERN.search(path.stem)


def mark(path: Path) -> None:
    if path.suffix.lower() in EXTENSIONS or is_base_json(path):
        with lock:
            pending[path] = time.monotonic()


class Handler(FileSystemEventHandler):
    def on_any_event(self, event):
        if event.is_directory or event.event_type not in ("created", "modified", "moved", "closed"):
            return
        mark(Path(event.dest_path or event.src_path))  # dest_path is only set for moves


def convert(src: Path, tmp: Path) -> bool:
    log.info("Converting %s", src)
    result = subprocess.run(["toktx", *TOKTX_ARGS, str(tmp), str(src)], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("toktx failed (exit %s) for %s\n%s", result.returncode, src, result.stderr.strip())
        return False
    return True


def copy(src: Path, tmp: Path) -> bool:
    log.info("Copying %s", src)
    try:
        shutil.copyfile(src, tmp)
    except OSError as e:
        log.error("Could not copy %s: %s", src, e)
        return False
    return True


def process(src: Path) -> None:
    is_image = src.suffix.lower() in EXTENSIONS
    dest = OUTPUT_DIR / src.relative_to(INPUT_DIR)
    if is_image:
        dest = dest.with_suffix(".ktx2")
    try:
        src_mtime = src.stat().st_mtime_ns
        if dest.exists():
            age = src_mtime - dest.stat().st_mtime_ns
            if (abs(age) if is_image else age) < MTIME_TOLERANCE_NS:
                return  # already up to date
    except OSError:
        return  # source vanished
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Could not create %s: %s", dest.parent, e)
        return
    # Written beside dest and renamed once complete, so work interrupted partway
    # never leaves a partial file at dest. Keeps the .ktx2 suffix for toktx.
    tmp = dest.with_name(f".{dest.name}")
    if not (convert if is_image else copy)(src, tmp):
        tmp.unlink(missing_ok=True)
        return
    try:
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
    log.info("Watching %s for %s and base .json, writing to %s (polling=%s)", INPUT_DIR, sorted(EXTENSIONS), OUTPUT_DIR, USE_POLLING)

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
