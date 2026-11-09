"""Paths of exports relative to the input directory, as they are written out.

Exports made on Windows can arrive with backslashes in place of directory
separators, e.g. as a single file named "Map\\Foundry Big Image\\Map_00.png",
since Linux takes a backslash as part of the name. Here they're taken as
separators, so that file comes out as Map/Foundry Big Image/Map_00.ktx2.
"""

from pathlib import Path, PurePosixPath


def relative(path: Path, root: Path) -> PurePosixPath:
    """path relative to root, with backslashes as separators.

    Empty, "." and ".." parts are dropped, so joined to a directory the result
    always stays within it.
    """
    parts = path.relative_to(root).as_posix().replace("\\", "/").split("/")
    return PurePosixPath(*(p for p in parts if p not in ("", ".", "..")))
