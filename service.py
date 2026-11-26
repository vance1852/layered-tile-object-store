"""Compatibility module exposing the service and HTTP server factory."""

from tile_service import TileService
from tile_service.server import TileHTTPServer, build_server

TileService = TileService
Service = TileService
create_server = build_server

__all__ = ["TileService", "Service", "TileHTTPServer", "build_server", "create_server"]
