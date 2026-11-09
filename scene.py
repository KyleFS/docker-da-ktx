"""Combine Dungeon Alchemist .json exports into multi-level Foundry scenes.

Each image has a matching .json beside it. Those of the time-of-day and weather
variants (matched by VARIANT_PATTERN) are skipped, while those of the base
images, one per floor, are combined into a single multi-level Foundry scene:

    Map_00.json                 base, floor 0 \\
    Map_01.json                 base, floor 1  > Map.json
    Map_Evening_00.json         variant
    Map_Snow_Evening_00.json    variant

Dungeon Alchemist writes these in Foundry's pre-v10 format. The scene is written
in the current one: each floor becomes a level, LEVEL_HEIGHT high and stacked
from the lowest floor up (0-10, 11-20, 21-30... by default), whose background is
the floor's image, and its walls and lights are placed on that level.

Some fields of each scene can be overridden, each only when its variable is set:

    background   FOUNDRY_ASSET_DIR, where Foundry sees the output directory
                 (e.g. "assets/maps"), joined with each floor's .ktx2 path in it.
                 Otherwise it is the "img" of the floor's .json.
    background   BACKGROUND_COLOR, e.g. "#000000", the same on every level
      color
    grid color   GRID_COLOR, e.g. "#000000"
    grid alpha   GRID_ALPHA, e.g. 0.2

Configuration is via environment variables.
"""

import hashlib
import json
import logging
import os
import re
import string
from pathlib import Path
from urllib.parse import quote

# Joined as a string rather than a path so a URL keeps its "//".
FOUNDRY_ASSET_DIR = os.environ.get("FOUNDRY_ASSET_DIR", "").strip().rstrip("/")
# Fields of each level's "background" set to the same value in every scene.
BACKGROUND_OVERRIDES = {}
if os.environ.get("BACKGROUND_COLOR", "").strip():
    BACKGROUND_OVERRIDES["color"] = os.environ["BACKGROUND_COLOR"].strip()
# Fields of "grid" set to the same value in every scene.
GRID_OVERRIDES = {}
if os.environ.get("GRID_COLOR", "").strip():
    GRID_OVERRIDES["color"] = os.environ["GRID_COLOR"].strip()
if os.environ.get("GRID_ALPHA", "").strip():
    GRID_OVERRIDES["alpha"] = float(os.environ["GRID_ALPHA"])
LEVEL_HEIGHT = int(os.environ.get("LEVEL_HEIGHT", "10"))
# Tested against the filename stem.
VARIANT_PATTERN = re.compile(
    os.environ.get("VARIANT_PATTERN", r"_(Morning|Noon|Evening|Night|Snow)(_\d+)?$"),
    re.IGNORECASE,
)
# The floor number ending a base .json's stem. A map with one floor may have none.
FLOOR_PATTERN = re.compile(r"_\d+$")
ID_CHARS = string.ascii_letters + string.digits
# Wall restrictions as Foundry numbered them before v9 (0 was, and is, none).
LEGACY_RESTRICTIONS = {1: 20, 2: 10}
# Light fields Foundry moved into "config" in v9, by their old and new names.
LEGACY_LIGHT_FIELDS = {
    "dim": "dim",
    "bright": "bright",
    "angle": "angle",
    "tintColor": "color",
    "tintAlpha": "alpha",
    "lightAnimation": "animation",
    "darkness": "darkness",
}

log = logging.getLogger(__name__)


def is_base_json(path: Path) -> bool:
    return path.suffix.lower() == ".json" and not VARIANT_PATTERN.search(path.stem)


def scene_of(floor: Path) -> Path:
    """The path of the scene a base .json is a floor of, which no file has."""
    return floor.with_name(FLOOR_PATTERN.sub("", floor.stem) + ".json")


def floors_of(scene: Path) -> list[Path]:
    """The base .json of each of the scene's floors, lowest first."""
    pattern = re.compile(re.escape(scene.stem) + r"(?:_(\d+))?\.json", re.IGNORECASE)
    try:
        matches = [(m, p) for p in scene.parent.iterdir() if (m := pattern.fullmatch(p.name)) and is_base_json(p)]
    except OSError:
        return []
    return [p for m, p in sorted(matches, key=lambda mp: (int(mp[0].group(1) or 0), mp[1].name))]


def make_id(*parts) -> str:
    """A Foundry document ID derived from parts, so a rebuilt scene keeps its IDs."""
    digest = hashlib.sha256("/".join(map(str, parts)).encode()).digest()
    return "".join(ID_CHARS[b % len(ID_CHARS)] for b in digest[:16])


def modern_wall(wall: dict) -> dict:
    wall = dict(wall)
    sense = wall.pop("sense", None)  # split into sight and light in v10
    if sense is not None:
        wall.setdefault("sight", sense)
        wall.setdefault("light", sense)
    for field in ("move", "sight", "light", "sound"):
        if field in wall:
            wall[field] = LEGACY_RESTRICTIONS.get(wall[field], wall[field])
    return wall


def modern_light(light: dict) -> dict:
    light = dict(light)
    config = dict(light.get("config") or {})
    for old, new in LEGACY_LIGHT_FIELDS.items():
        if old in light:
            config.setdefault(new, light.pop(old))
    light["config"] = config
    return light


def build_scene(scene: Path, floors: list[tuple[Path, dict]], root: Path) -> dict:
    """A Foundry scene with a level for each (path, Dungeon Alchemist export) of floors.

    Paths are under root, which Foundry sees as FOUNDRY_ASSET_DIR once converted.
    """
    scene_name = scene.relative_to(root).as_posix()
    levels, walls, lights = [], [], []
    for n, (src, data) in enumerate(floors):
        level_id = make_id(scene_name, "level", n)
        bottom = n * LEVEL_HEIGHT + (1 if n else 0)
        if FOUNDRY_ASSET_DIR:
            background = f"{FOUNDRY_ASSET_DIR}/{quote(src.relative_to(root).with_suffix('.ktx2').as_posix())}"
        else:
            background = data.get("img")
        levels.append({
            "_id": level_id,
            "name": f"Level {n + 1}",
            "elevation": {"bottom": bottom, "top": (n + 1) * LEVEL_HEIGHT},
            "background": {"src": background, **BACKGROUND_OVERRIDES},
            "foreground": {"src": data.get("foreground")},
            "sort": n,
        })
        for i, wall in enumerate(data.get("walls") or []):
            walls.append({**modern_wall(wall), "_id": make_id(scene_name, "wall", n, i), "levels": [level_id]})
        for i, light in enumerate(data.get("lights") or []):
            lights.append({
                **modern_light(light),
                "_id": make_id(scene_name, "light", n, i),
                "elevation": bottom,
                "levels": [level_id],
            })

    # Floors of one map share their dimensions and grid, so the lowest speaks for all.
    first = floors[0][1]
    return {
        "name": FLOOR_PATTERN.sub("", str(first.get("name") or scene.stem)),
        "width": first.get("width"),
        "height": first.get("height"),
        "padding": first.get("padding", 0),
        "shiftX": first.get("shiftX", 0),
        "shiftY": first.get("shiftY", 0),
        "grid": {
            "type": first.get("gridType", 1),
            "size": first.get("grid"),
            "distance": first.get("gridDistance", 5),
            "units": first.get("gridUnits", "ft"),
            "color": first.get("gridColor", "#000000"),
            "alpha": first.get("gridAlpha", 0.2),
            **GRID_OVERRIDES,
        },
        "environment": {
            "darknessLevel": first.get("darkness", 0),
            "globalLight": {"enabled": bool(first.get("globalLight", False))},
        },
        "levels": levels,
        "initialLevel": levels[0]["_id"],
        "walls": walls,
        "lights": lights,
    }


def write_scene(scene: Path, sources: list[Path], root: Path, tmp: Path) -> bool:
    log.info("Building %s from %s", scene, ", ".join(p.name for p in sources))
    try:
        floors = []
        for src in sources:
            data = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"top level of {src.name} is not an object")
            floors.append((src, data))
        tmp.write_text(json.dumps(build_scene(scene, floors, root), indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log.error("Could not build %s: %s", scene, e)
        return False
    return True
