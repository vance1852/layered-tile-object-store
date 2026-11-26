"""Compatibility import name for the tile storage service."""

from tile_service import AttrDict, TileService, TileServiceError

TileStorage = TileService
TileStore = TileService
VersionedTileStorage = TileService
create_service = TileService

__all__ = [
    "TileService",
    "TileStorage",
    "TileStore",
    "VersionedTileStorage",
    "create_service",
    "TileServiceError",
    "AttrDict",
]
