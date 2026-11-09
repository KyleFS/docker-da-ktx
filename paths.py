"""Paths of exports relative to the input directory, as they are written out.

Exports made on Windows can arrive with backslashes in place of directory
separators, e.g. as a single file named "Map\\Foundry Big Image\\Map_00.png",
since Linux takes a backslash as part of the name. Here they're taken as
separators, so that file comes out as Map/Foundry_Big_Image/Map_00.ktx2.

Each name along the way is made safe to use unquoted in a shell or URL:

    accents        dropped               Château   -> Chateau
    spaces         _                     Big Image -> Big_Image
    quotes and     dropped               Map (Old) -> Map_Old
      brackets
    &              and                   Cats & Co -> Cats_and_Co
    anything else  -                     Ruins, 2  -> Ruins-2

Runs of _ and - shrink to their first, and they're trimmed from either end of
the stem along with ".". A name left with no stem becomes a hash of the original.
"""

import hashlib
import re
import unicodedata
from pathlib import Path, PurePosixPath

DROPPED = re.compile(r"""['"`‘’“”()\[\]{}]""")
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
SEPARATOR_RUN = re.compile(r"([_-])[_-]+")


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\s+", "_", DROPPED.sub("", text)).replace("&", "and")
    return SEPARATOR_RUN.sub(r"\1", UNSAFE.sub("-", text))


def safe_name(name: str) -> str:
    # Split first, as cleaning can leave nothing but the suffix (e.g. ".png").
    path = PurePosixPath(name)
    stem = clean(path.stem).strip("_-.") or hashlib.sha256(name.encode()).hexdigest()[:8]
    return stem + clean(path.suffix)


def relative(path: Path, root: Path) -> PurePosixPath:
    """path relative to root, with backslashes as separators and every name made safe.

    Empty, "." and ".." parts are dropped, so joined to a directory the result
    always stays within it.
    """
    parts = path.relative_to(root).as_posix().replace("\\", "/").split("/")
    return PurePosixPath(*(safe_name(p) for p in parts if p not in ("", ".", "..")))
