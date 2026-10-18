#!/usr/bin/env python3
"""Watch a directory of Dungeon Alchemist batch exports and convert images to KTX2.

Dungeon Alchemist writes a batch export as a tree of PNG/JPEG files (one
folder per map, several layers per map). This watches that tree, waits for
each file to finish being written, and converts it to KTX2 via the `toktx`
CLI from KTX-Software. Conversion runs in a small worker pool so one slow
`toktx` invocation doesn't block newly detected files.

Configuration is entirely via environment variables (set these in Docker).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from watchfiles import Change, DefaultFilter, watch

log = logging.getLogger("da_ktx_watch")

DEFAULT_EXTENSIONS = ".png,.jpg,.jpeg,.tga,.bmp"
DEFAULT_TOKTX_ARGS = "--t2 --genmipmap --encode uastc --zcmp 19"


def _bool_env(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() not in ("0", "false", "")


@dataclass
class Config:
    watch_dir: Path
    extensions: tuple[str, ...]
    recursive: bool
    toktx_bin: str
    toktx_args: list[str]
    stability_seconds: float
    poll_interval: float
    workers: int
    force: bool
    process_existing: bool


def load_config() -> Config:
    env = os.environ

    watch_dir_raw = env.get("WATCH_DIR")
    if not watch_dir_raw:
        sys.exit("WATCH_DIR environment variable is required")

    watch_dir = Path(watch_dir_raw).expanduser().resolve()
    if not watch_dir.is_dir():
        sys.exit(f"WATCH_DIR does not exist or is not a directory: {watch_dir}")

    extensions = tuple(
        e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
        for e in env.get("SOURCE_EXTENSIONS", DEFAULT_EXTENSIONS).split(",")
        if e.strip()
    )

    logging.basicConfig(
        level=getattr(logging, env.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    return Config(
        watch_dir=watch_dir,
        extensions=extensions,
        recursive=_bool_env("RECURSIVE", True),
        toktx_bin=env.get("TOKTX_BIN", "toktx"),
        toktx_args=shlex.split(env.get("TOKTX_ARGS", DEFAULT_TOKTX_ARGS)),
        stability_seconds=float(env.get("STABILITY_SECONDS", "2.0")),
        poll_interval=float(env.get("POLL_INTERVAL", "0.5")),
        workers=int(env.get("WORKERS", "2")),
        force=_bool_env("FORCE_RECONVERT", False),
        process_existing=_bool_env("PROCESS_EXISTING", True),
    )


def dest_path_for(src: Path) -> Path:
    return src.with_suffix(".ktx2")


def needs_conversion(src: Path, dest: Path, force: bool) -> bool:
    if force:
        return True
    if not dest.exists():
        return True
    try:
        return src.stat().st_mtime > dest.stat().st_mtime
    except OSError:
        return True


class ConversionQueue:
    """Tracks files that changed recently and submits them for conversion
    once their size has stopped changing for `stability_seconds`."""

    def __init__(self, config: Config, executor: ThreadPoolExecutor):
        self.config = config
        self.executor = executor
        self._lock = threading.Lock()
        self._pending: dict[Path, tuple[int, float]] = {}  # path -> (size, last_change_time)
        self._in_flight: set[Path] = set()
        self._stop = threading.Event()

    def touch(self, path: Path) -> None:
        with self._lock:
            if path in self._in_flight:
                return
            now = time.monotonic()
            try:
                size = path.stat().st_size
            except OSError:
                return
            self._pending[path] = (size, now)

    def _poll_once(self) -> None:
        now = time.monotonic()
        ready: list[Path] = []
        with self._lock:
            for path, (last_size, last_change) in list(self._pending.items()):
                try:
                    size = path.stat().st_size
                except OSError:
                    log.debug("File disappeared before stabilizing: %s", path)
                    del self._pending[path]
                    continue
                if size != last_size:
                    self._pending[path] = (size, now)
                    continue
                if now - last_change >= self.config.stability_seconds:
                    ready.append(path)

            for path in ready:
                del self._pending[path]
                self._in_flight.add(path)

        for path in ready:
            self.executor.submit(self._convert_and_release, path)

    def _convert_and_release(self, path: Path) -> None:
        try:
            convert_one(path, self.config)
        finally:
            with self._lock:
                self._in_flight.discard(path)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.config.poll_interval)

    def stop(self) -> None:
        self._stop.set()


def convert_one(src: Path, config: Config) -> bool:
    if not src.exists():
        log.debug("Skipping vanished file: %s", src)
        return False

    dest = dest_path_for(src)

    if not needs_conversion(src, dest, config.force):
        log.debug("Up to date, skipping: %s", src)
        return True

    cmd = [config.toktx_bin, *config.toktx_args, str(dest), str(src)]
    log.info("Converting %s -> %s", src, dest)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log.error(
            "toktx executable not found (%s). Is KTX-Software installed on PATH?",
            config.toktx_bin,
        )
        return False

    if result.returncode != 0:
        log.error(
            "toktx failed (exit %s) for %s\nstdout: %s\nstderr: %s",
            result.returncode,
            src,
            result.stdout.strip(),
            result.stderr.strip(),
        )
        dest.unlink(missing_ok=True)
        return False

    log.info("Done: %s", dest)
    return True


class ExtensionFilter(DefaultFilter):
    """Extends watchfiles' default ignore rules (dotfiles, VCS dirs, etc.)
    with a check that the changed path has one of our source extensions."""

    def __init__(self, extensions: tuple[str, ...]):
        self.extensions = extensions
        super().__init__()

    def __call__(self, change: Change, path: str) -> bool:
        if not super().__call__(change, path):
            return False
        return Path(path).suffix.lower() in self.extensions


def watch_loop(config: Config, queue: ConversionQueue, stop_event: threading.Event) -> None:
    watch_filter = ExtensionFilter(config.extensions)
    for changes in watch(
        config.watch_dir,
        watch_filter=watch_filter,
        recursive=config.recursive,
        stop_event=stop_event,
        debounce=200,
        step=50,
        raise_interrupt=False,
    ):
        for change_type, raw_path in changes:
            if change_type in (Change.added, Change.modified):
                queue.touch(Path(raw_path))


def scan_existing(config: Config, queue: ConversionQueue) -> None:
    walker = config.watch_dir.rglob("*") if config.recursive else config.watch_dir.glob("*")
    count = 0
    for path in walker:
        if path.is_file() and path.suffix.lower() in config.extensions:
            queue.touch(path)
            count += 1
    if count:
        log.info("Queued %d existing file(s) from %s for conversion check", count, config.watch_dir)


def main() -> int:
    config = load_config()

    log.info("Watching %s (recursive=%s) for %s", config.watch_dir, config.recursive, config.extensions)
    log.info("Writing .ktx2 output alongside source files")

    executor = ThreadPoolExecutor(max_workers=config.workers)
    queue = ConversionQueue(config, executor)

    if config.process_existing:
        scan_existing(config, queue)

    watch_stop = threading.Event()
    watcher = threading.Thread(target=watch_loop, args=(config, queue, watch_stop), daemon=True)
    watcher.start()

    poller = threading.Thread(target=queue.run_forever, daemon=True)
    poller.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        watch_stop.set()
        watcher.join()
        queue.stop()
        executor.shutdown(wait=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
