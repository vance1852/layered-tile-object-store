"""Conventional tile server entry point."""

from tile_service.server import TileHTTPServer, build_server, serve_forever

__all__ = ["TileHTTPServer", "build_server", "serve_forever"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="版本化稀疏瓦片存储 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None)
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args()
    serve_forever(args.host, args.port, data_dir=args.data, fixture=args.fixture)
