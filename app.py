"""Conventional application entry point.

Run ``python app.py --fixture fixtures/partial-claim-and-pinned-package`` or
import :func:`create_server` for tests.
"""

from tile_service.server import TileHTTPServer, build_server, serve_forever

app = None  # Initialized by create_app(); HTTP is stdlib's ThreadingHTTPServer.


def create_app(data_dir=None, fixture=None):
    global app
    app = build_server("127.0.0.1", 0, data_dir=data_dir, fixture=fixture)
    return app


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="瓦片存储 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None)
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args(argv)
    serve_forever(args.host, args.port, data_dir=args.data, fixture=args.fixture)


if __name__ == "__main__":
    main()
