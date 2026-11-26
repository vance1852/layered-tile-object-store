"""Compatibility import name for the tile storage service."""

from tile_service import AttrDict, TileService, TileServiceError

TileStore = TileService
VersionedTileStore = TileService
create_store = TileService

__all__ = ["TileService", "TileStore", "VersionedTileStore", "create_store", "TileServiceError", "AttrDict"]
