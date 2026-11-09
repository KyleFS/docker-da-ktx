"""Convert Dungeon Alchemist images to .ktx2 with `toktx` from KTX-Software.

Configuration is via environment variables:

    SOURCE_EXTENSIONS   image types to convert, e.g. "png,jpg,jpeg"
    TOKTX_ARGS          options passed to toktx
"""

import logging
import os
import shlex
import subprocess
from pathlib import Path

EXTENSIONS = {
    "." + e.strip().lower().lstrip(".")
    for e in os.environ.get("SOURCE_EXTENSIONS", "png,jpg,jpeg").split(",")
    if e.strip()
}
TOKTX_ARGS = shlex.split(
    os.environ.get(
        "TOKTX_ARGS",
        "--encode uastc --uastc_quality 2 --uastc_rdo_l 1.0 --zcmp 19 --genmipmap --target_type RGB",
    )
)

log = logging.getLogger(__name__)


def is_image(path: Path) -> bool:
    return path.suffix.lower() in EXTENSIONS


def convert(src: Path, tmp: Path) -> bool:
    log.info("Converting %s", src)
    result = subprocess.run(["toktx", *TOKTX_ARGS, str(tmp), str(src)], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("toktx failed (exit %s) for %s\n%s", result.returncode, src, result.stderr.strip())
        return False
    return True
