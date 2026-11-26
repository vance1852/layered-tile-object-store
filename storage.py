"""Short compatibility import name."""

from tile_service import TileService
from tile_service.core import AttrDict

Storage = TileService
TileStorage = TileService

__all__ = ["TileService", "Storage", "TileStorage", "AttrDict"]
