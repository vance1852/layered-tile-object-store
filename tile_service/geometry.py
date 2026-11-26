"""Tile coordinate and raster projection helpers.

The service uses the repository convention: an integer tile is ``z/x/y`` with
the origin in the upper-left.  All geometry calculations that may cross zoom
levels use :class:`fractions.Fraction` so claim boundaries are never affected
by floating-point rounding.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Sequence

RASTER_SIZE = 4
PIXEL_COUNT = RASTER_SIZE * RASTER_SIZE

Tile = tuple[int, int, int]
Bounds = tuple[int, int, int, int]
Geometry = tuple[int, int, int, int, int]  # zoom, x0, y0, x1, y1


def tile_key(z: int, x: int, y: int) -> str:
    return f"{int(z)}/{int(x)}/{int(y)}"


def parse_tile(value) -> Tile:
    """Parse a tile from ``[z, x, y]``, a mapping, or ``"z/x/y"``."""
    if isinstance(value, str):
        parts = value.strip().strip("/").split("/")
        if len(parts) != 3:
            raise ValueError(f"invalid tile coordinate: {value!r}")
        z, x, y = (int(p) for p in parts)
    elif isinstance(value, dict):
        z, x, y = int(value["z"]), int(value["x"]), int(value["y"])
    else:
        if not isinstance(value, Sequence) or len(value) != 3:
            raise ValueError(f"invalid tile coordinate: {value!r}")
        z, x, y = (int(v) for v in value)
    valid_tile(z, x, y)
    return z, x, y


def valid_tile(z: int, x: int, y: int) -> bool:
    if z < 0:
        raise ValueError("zoom must be non-negative")
    limit = 1 << z
    if not (0 <= x < limit and 0 <= y < limit):
        raise ValueError(f"tile {z}/{x}/{y} is outside the zoom level")
    return True


def parse_bounds(value, *, zoom: int | None = None) -> Bounds:
    if not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError("bounds must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (int(v) for v in value)
    if not (x0 < x1 and y0 < y1):
        raise ValueError("bounds must be a non-empty half-open rectangle")
    if zoom is not None:
        limit = 1 << int(zoom)
        if not (0 <= x0 < x1 <= limit and 0 <= y0 < y1 <= limit):
            raise ValueError(f"bounds {value!r} are outside zoom {zoom}")
    return x0, y0, x1, y1


def tile_geometry(tile: Tile) -> Geometry:
    z, x, y = tile
    return z, x, y, x + 1, y + 1


def ancestor_at(tile: Tile, ancestor_z: int) -> Tile:
    z, x, y = tile
    if ancestor_z < 0 or ancestor_z > z:
        raise ValueError("ancestor zoom must be between 0 and tile zoom")
    shift = z - ancestor_z
    return ancestor_z, x >> shift, y >> shift


def children(tile: Tile) -> list[Tile]:
    z, x, y = tile
    nz = z + 1
    return [(nz, 2 * x + dx, 2 * y + dy) for dy in (0, 1) for dx in (0, 1)]


def tiles_inside_geometry(geom: Geometry, level: int) -> list[Tile]:
    """Enumerate integer tiles at ``level`` intersecting a half-open geometry."""
    gz, x0, y0, x1, y1 = geom
    if level < 0:
        raise ValueError("level must be non-negative")
    scale = Fraction(1 << level, 1 << gz)
    lx0 = _floor_fraction(x0 * scale)
    ly0 = _floor_fraction(y0 * scale)
    lx1 = _ceil_fraction(x1 * scale)
    ly1 = _ceil_fraction(y1 * scale)
    out: list[Tile] = []
    limit = 1 << level
    for ty in range(max(0, ly0), min(limit, ly1)):
        for tx in range(max(0, lx0), min(limit, lx1)):
            out.append((level, tx, ty))
    return out


def _floor_fraction(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def pixel_mask(target: Tile, geom: Geometry | None) -> list[bool]:
    """Return which of the target's 16 pixel centres lie in ``geom``."""
    if geom is None:
        return [True] * PIXEL_COUNT
    z, tx, ty = target
    gz, x0, y0, x1, y1 = geom
    scale = Fraction(1 << z, 1 << gz)
    gx0, gx1 = x0 * scale, x1 * scale
    gy0, gy1 = y0 * scale, y1 * scale
    mask: list[bool] = []
    for row in range(RASTER_SIZE):
        cy = ty + Fraction(2 * row + 1, 2 * RASTER_SIZE)
        for col in range(RASTER_SIZE):
            cx = tx + Fraction(2 * col + 1, 2 * RASTER_SIZE)
            mask.append(gx0 <= cx < gx1 and gy0 <= cy < gy1)
    return mask


def source_pixel(target: Tile, col: int, row: int, source: Tile) -> tuple[int, int]:
    """Map a target raster pixel to the nearest source raster pixel."""
    z, tx, ty = target
    sz, sx, sy = source
    scale = Fraction(1 << sz, 1 << z)
    gx = (tx + Fraction(2 * col + 1, 2 * RASTER_SIZE)) * scale
    gy = (ty + Fraction(2 * row + 1, 2 * RASTER_SIZE)) * scale
    rx = gx - sx
    ry = gy - sy
    sc = int((rx * RASTER_SIZE).__floor__())
    sr = int((ry * RASTER_SIZE).__floor__())
    # Boundaries and exact centre calculations can otherwise land on 4.
    sc = min(RASTER_SIZE - 1, max(0, sc))
    sr = min(RASTER_SIZE - 1, max(0, sr))
    return sc, sr


def project_raster(data: bytes, source: Tile, target: Tile) -> bytes:
    """Project a 4x4 source tile into a target tile using nearest sampling."""
    if len(data) != PIXEL_COUNT:
        raise ValueError(f"raster must contain {PIXEL_COUNT} bytes")
    out = bytearray(PIXEL_COUNT)
    for row in range(RASTER_SIZE):
        for col in range(RASTER_SIZE):
            sc, sr = source_pixel(target, col, row, source)
            out[row * RASTER_SIZE + col] = data[sr * RASTER_SIZE + sc]
    return bytes(out)


def iter_tiles_in_extent(extent, levels: Iterable[int]) -> list[Tile]:
    """Expand a pickup extent into tiles at the requested levels.

    Supported forms are a seed tile ``[z, x, y]``, a geometry
    ``[z, x0, y0, x1, y1]`` / ``{"z": z, "bounds": [...]}``, or an explicit
    list of tile coordinates.
    """
    geom: Geometry | None = None
    seed: Tile | None = None
    if isinstance(extent, dict):
        z = int(extent["z"])
        geom = (z, *parse_bounds(extent.get("bounds") or extent.get("rect"), zoom=z))
    elif (
        isinstance(extent, (list, tuple))
        and len(extent) == 5
        and all(isinstance(v, int) for v in extent)
    ):
        z, x0, y0, x1, y1 = (int(v) for v in extent)
        geom = (z, *parse_bounds([x0, y0, x1, y1], zoom=z))
    elif isinstance(extent, (list, tuple)) and len(extent) == 3 and all(isinstance(v, int) for v in extent):
        seed = parse_tile(extent)
    elif isinstance(extent, str):
        seed = parse_tile(extent)
    else:
        # An explicit coordinate list may be supplied directly.
        coords = [parse_tile(v) for v in extent]
        result = []
        wanted = set(int(v) for v in levels)
        for coord in coords:
            if coord[0] in wanted:
                result.append(coord)
        return list(dict.fromkeys(result))

    result: list[Tile] = []
    for level in sorted(set(int(v) for v in levels)):
        if geom is not None:
            result.extend(tiles_inside_geometry(geom, level))
        elif seed is None:  # pragma: no cover - defensive
            raise ValueError("invalid extent")
        elif level == seed[0]:
            result.append(seed)
        elif level < seed[0]:
            result.append(ancestor_at(seed, level))
        else:
            result.extend(tiles_inside_geometry(tile_geometry(seed), level))

    seen: set[Tile] = set()
    ordered: list[Tile] = []
    for tile in result:
        if tile not in seen:
            seen.add(tile)
            ordered.append(tile)
    return ordered
