"""Standard-library HTTP adapter for :class:`tile_service.TileService`."""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__ in {None, ""}:  # support python tile_service/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tile_service.core import TileService
    from tile_service.errors import TileServiceError
    from tile_service.replay import load_fixture_service, replay_record
else:
    from .core import TileService
    from .errors import TileServiceError
    from .replay import load_fixture_service, replay_record


class TileHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: TileService):
        super().__init__(address, TileRequestHandler)
        self.service = service


def build_server(host: str = "127.0.0.1", port: int = 8080, *, data_dir=None, fixture=None) -> TileHTTPServer:
    if fixture:
        service, record = load_fixture_service(fixture, data_dir)
        # A fixture directory is an executable timeline: bring the server up
        # to its final state so /pickups/p9 and version explanations exist.
        replay_record(service, record)
    else:
        service = TileService(data_dir)
    return TileHTTPServer((host, int(port)), service)


class TileRequestHandler(BaseHTTPRequestHandler):
    server: TileHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet by default
        return

    # ------------------------------------------------------------- utilities
    @property
    def service(self) -> TileService:
        return self.server.service

    def _query(self) -> dict[str, str]:
        parsed = urlsplit(self.path)
        raw = parse_qs(parsed.query, keep_blank_values=True)
        return {k: v[-1] for k, v in raw.items()}

    def _path(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,HEAD,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,If-None-Match,Range")

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command not in {"HEAD", "OPTIONS"} and data:
            self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 32 << 20:
            raise TileServiceError("request body too large", status=413)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        body = self._read_body()
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TileServiceError(f"invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise TileServiceError("JSON body must be an object")
        return value

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, TileServiceError):
            status = getattr(exc, "status", 400)
            self._send_json(status, exc.to_dict())
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            name = "missing_field" if isinstance(exc, KeyError) else "invalid_request"
            message = f"{name}: {exc}" if str(exc) else name
            self._send_json(400, {"error": "invalid_request", "code": name, "message": message})
        else:
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def _tile_from_query_or_body(self, query: dict, body: dict | None = None):
        body = body or {}
        if "tile" in body:
            return body["tile"]
        if all(k in query for k in ("z", "x", "y")):
            return [query["z"], query["x"], query["y"]]
        if all(k in body for k in ("z", "x", "y")):
            return [body["z"], body["x"], body["y"]]
        raise TileServiceError("tile coordinate is required")

    # ----------------------------------------------------------------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = self._path()
            query = self._query()
            if path == "/health":
                self._send_json(200, {"ok": True, "service": "tile-service"})
            elif path == "/":
                self._send_json(200, {"service": "tile-service", "endpoints": ENDPOINTS})
            elif path in {"/policy", "/claims"}:
                self._send_json(200, {"claims": self.service.list_claims(active_only=query.get("all") != "1")})
            elif path == "/versions":
                self._send_json(200, {"head": self.service.state["head_version"], "versions": self.service.list_versions()})
            elif match := re.fullmatch(r"/versions/([^/]+)", path):
                self._send_json(200, self.service.get_version(match.group(1)))
            elif path in {"/resolve", "/explain"}:
                tile = self._tile_from_query_or_body(query)
                self._send_json(200, _json_default(self.service.resolve(tile, query.get("version"))))
            elif path in {"/tile", "/tiles", "/get_tile", "/v1/tile"}:
                tile = self._tile_from_query_or_body(query)
                self._send_tile(tile, query)
            elif match := re.fullmatch(r"/(?:tiles|tile)/(\d+)/(-?\d+)/(-?\d+)(?:\.bin)?", path):
                tile = tuple(map(int, match.groups()))
                self._send_tile(tile, query)
            elif path == "/pickups":
                self._send_json(200, {"pickups": [self.service.pickup_manifest(pid) for pid in self.service.state["pickups"]]})
            elif match := re.fullmatch(r"/pickups/([^/]+)/package\.zip", path):
                self._package_zip(match.group(1))
            elif match := re.fullmatch(r"/pickups/([^/]+)/items/(\d+)/(-?\d+)/(-?\d+)(?:\.bin)?", path):
                pid, z, x, y = match.groups()
                data = self.service.pickup_item_bytes(pid, (int(z), int(x), int(y)))
                self._send_bytes(200, data, "application/octet-stream")
            elif match := re.fullmatch(r"/pickups/([^/]+)", path):
                self._send_json(200, self.service.pickup_manifest(match.group(1)))
            elif path == "/usage":
                self._send_json(200, _json_default(self.service.reference_report()))
            elif match := re.fullmatch(r"/usage/([0-9a-fA-F]{64})", path):
                self._send_json(200, _json_default(self.service.why_retained(match.group(1).lower())))
            elif path == "/gc/plan":
                self._send_json(200, self.service.gc_plan())
            else:
                self._send_json(404, {"error": "route_not_found", "path": path})
        except Exception as exc:
            self._handle_error(exc)

    def _send_tile(self, tile, query: dict):
        response = self.service.tile_response(
            tile,
            version=query.get("version"),
            allow_alternative=query.get("alternative", "1") not in {"0", "false"},
            strict=query.get("strict") in {"1", "true"},
        )
        if query.get("format") == "json" or query.get("debug") in {"1", "true"}:
            self._send_json(
                response.http_status if response.http_status != 204 else 200,
                _json_default({"response_status": response.http_status, **response.resolution}),
            )
        else:
            self._send_bytes(response.http_status, response.data, response.content_type, response.headers)

    def _package_zip(self, pickup_id: str):
        download = self.service.begin_download(pickup_id, ttl_seconds=300)
        try:
            data = self.service.build_package_zip(pickup_id)
            self._send_bytes(200, data, "application/zip", {
                "Content-Disposition": f'attachment; filename="{pickup_id}-offline.zip"',
                "X-Download-Id": download["id"],
            })
        finally:
            self.service.finish_download(download["id"])

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        try:
            path = self._path()
            query = self._query()
            # Read exactly once.  Re-reading an empty parsed JSON object would
            # block on the already-consumed keep-alive request body.
            raw = self._read_body()
            if self._is_json():
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise TileServiceError(f"invalid JSON: {exc}") from exc
                if not isinstance(body, dict):
                    raise TileServiceError("JSON body must be an object")
            else:
                body = {}

            if path == "/blobs":
                if body:
                    import base64
                    data = base64.b64decode(body.get("data_b64", "")) if body.get("data_b64") else str(body.get("data", "")).encode()
                    meta = self.service.add_blob(data, name=body.get("name"), expected_sha256=body.get("sha256"))
                else:
                    name = query.get("name")
                    meta = self.service.add_blob(raw or b"", name=name, expected_sha256=query.get("sha256"))
                self._send_json(201, meta)
            elif match := re.fullmatch(r"/blobs/([^/]+)", path):
                meta = self.service.add_blob(raw, name=match.group(1), expected_sha256=query.get("sha256"))
                self._send_json(201, meta)
            elif path in {"/claims", "/policy/claims"}:
                claim = self.service.register_claim(
                    body["layer"], body["actor"], int(body["z"]), body["bounds"],
                    int(body.get("priority", 0)), claim_id=body.get("id"),
                    planned_bytes=int(body.get("planned_bytes", 0)),
                )
                self._send_json(201, claim)
            elif path in {"/claims/revoke", "/claims/revoke/"}:
                out = self.service.revoke_claim(body["layer"], body.get("actor"))
                self._send_json(200, out)
            elif match := re.fullmatch(r"/layers/([^/]+)/updates", path):
                result = self.service.submit_update(
                    match.group(1), body.get("actor"),
                    body.get("base", body.get("base_version", 0)),
                    body.get("tiles", []), revision=body.get("revision"),
                    area=body.get("area"), removed=body.get("removed"), message=body.get("message"),
                )
                self._send_json(201, _json_default(result))
            elif path == "/updates":
                result = self.service.submit_update(
                    body,
                )
                self._send_json(201, _json_default(result))
            elif path == "/pickups":
                manifest = self.service.create_pickup(
                    body.get("id") or body.get("pickup_id"),
                    extent=body.get("extent") or body.get("region"),
                    tiles=body.get("tiles"),
                    levels=body.get("levels"),
                    version=body.get("version") or body.get("logical_version"),
                    until=body.get("until"),
                )
                self._send_json(201, manifest)
            elif match := re.fullmatch(r"/pickups/([^/]+)/fetch", path):
                tile = self._tile_from_query_or_body(query, body)
                try:
                    out = self.service.fetch_pickup_tile(match.group(1), tile)
                    self._send_json(200, _json_default({"ok": True, "item": out["item"]}))
                except TileServiceError as exc:
                    self._send_json(exc.status, exc.to_dict())
            elif match := re.fullmatch(r"/pickups/([^/]+)/resume", path):
                self._send_json(200, _json_default(self.service.resume_pickup(match.group(1))))
            elif match := re.fullmatch(r"/pickups/([^/]+)/downloads", path):
                dl = self.service.begin_download(match.group(1), ttl_seconds=body.get("ttl_seconds", 300))
                self._send_json(201, dl)
            elif match := re.fullmatch(r"/downloads/([^/]+)/heartbeat", path):
                self._send_json(200, self.service.heartbeat_download(match.group(1), float(body.get("ttl_seconds", 300))))
            elif match := re.fullmatch(r"/downloads/([^/]+)/finish", path):
                self.service.finish_download(match.group(1))
                self._send_json(200, {"ok": True})
            elif path == "/gc":
                self._send_json(200, self.service.gc(dry_run=bool(body.get("dry_run", query.get("dry_run") in {"1", "true"}))))
            elif path in {"/revisions/reclaim", "/admin/reclaim"}:
                out = self.service.reclaim_layer_revision(body["layer"], int(body["revision"]))
                self._send_json(200, _json_default(out))
            elif path == "/replay-fixture":
                service, record = load_fixture_service(body["fixture"])
                report = replay_record(service, record)
                self._send_json(200, _json_default(report))
            else:
                self._send_json(404, {"error": "route_not_found", "path": path})
        except Exception as exc:
            self._handle_error(exc)

    def do_PUT(self):
        # PUT /blobs/name is REST-friendly; reuse POST logic by method shim.
        self.do_POST()

    def do_DELETE(self):
        try:
            path = self._path()
            if match := re.fullmatch(r"/claims/([^/]+)/([^/]+)", path):
                layer, actor = match.groups()
                self._send_json(200, self.service.revoke_claim(layer, actor))
            elif match := re.fullmatch(r"/pickups/([^/]+)", path):
                self._send_json(200, self.service.delete_pickup(match.group(1)))
            else:
                self._send_json(404, {"error": "route_not_found", "path": path})
        except Exception as exc:
            self._handle_error(exc)

    def _is_json(self) -> bool:
        return "application/json" in self.headers.get("Content-Type", "").lower()


ENDPOINTS = [
    "GET /health",
    "GET /versions[/{id}]",
    "GET /claims",
    "POST /claims",
    "DELETE /claims/{layer}/{actor}",
    "POST /layers/{layer}/updates",
    "POST /blobs | PUT /blobs/{name}",
    "GET /tiles/{z}/{x}/{y}?version=&format=json",
    "GET /resolve?z=&x=&y=",
    "GET /explain?z=&x=&y=",
    "POST /pickups",
    "GET /pickups/{id}",
    "POST /pickups/{id}/fetch",
    "POST /pickups/{id}/resume",
    "GET /pickups/{id}/package.zip",
    "GET /usage[/{sha256}]",
    "POST /gc",
    "POST /revisions/reclaim",
]


def _json_default(value):
    if isinstance(value, dict):
        return {str(k): _json_default(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_default(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("latin1")
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def serve_forever(host: str = "127.0.0.1", port: int = 8080, **kwargs):
    httpd = build_server(host, port, **kwargs)
    print(f"tile-service http://{host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="版本化稀疏瓦片存储 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=None)
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args()
    serve_forever(args.host, args.port, data_dir=args.data, fixture=args.fixture)
