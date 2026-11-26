"""Replay the supplied timeline/event fixtures against :class:`TileService`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import TileService
from .errors import TileServiceError, TileUnavailableError


def load_record(fixture_dir: str | Path) -> dict:
    return json.loads((Path(fixture_dir) / "events.json").read_text(encoding="utf-8"))


def load_fixture_service(
    fixture_dir: str | Path,
    data_dir: str | Path | None = None,
    *,
    supported_levels=(1, 2),
) -> tuple[TileService, dict]:
    fixture_dir = Path(fixture_dir)
    record = load_record(fixture_dir)
    service = TileService(
        data_dir,
        capacity_bytes=record.get("policy", {}).get("capacity_bytes"),
        supported_levels=supported_levels,
    )
    service.install_policy(record.get("policy", {}), reset=True)
    objects_dir = fixture_dir / "objects"
    for name, meta in record.get("objects", {}).items():
        data = (objects_dir / name).read_bytes()
        service.add_blob(data, name=name, expected_sha256=meta.get("sha256"))
    return service, record


def replay_record(service: TileService, record: dict, *, strict: bool = False) -> dict[str, Any]:
    results = []
    for event in sorted(record.get("events", []), key=lambda e: (e.get("at", 0),)):
        at = event.get("at")
        action = event.get("action")
        entry: dict[str, Any] = {"at": at, "action": action}
        try:
            if action == "layer":
                out = service.submit_update(
                    event["layer"],
                    event["actor"],
                    int(event.get("base", 0)),
                    event.get("tiles", []),
                    revision=event.get("revision"),
                    area=event.get("area"),
                    removed=event.get("removed"),
                    now=at,
                )
                entry["ok"] = True
                entry["version"] = out["version"]["id"]
                entry["revision"] = out["revision"]["revision"]
                entry["accepted_tiles"] = out["accepted_tiles"]

            elif action == "pickup":
                manifest = service.create_pickup(
                    event["id"],
                    extent=event.get("extent"),
                    levels=event.get("levels"),
                    until=event.get("until"),
                    now=at,
                )
                entry["ok"] = True
                entry["pickup"] = _summary_manifest(manifest)

            elif action == "fetch":
                try:
                    out = service.fetch_pickup_tile(event["id"], event["tile"], now=at)
                    entry["ok"] = True
                    entry["item"] = _public_fetch_item(out["item"])
                    entry["bytes"] = out["data"].decode("latin1") if out.get("data") else ""
                except TileUnavailableError as exc:
                    entry["ok"] = False
                    entry["status"] = "unavailable"
                    entry["details"] = exc.details
                    if strict:
                        raise

            elif action == "read":
                response = service.tile_response(event["tile"])
                entry["http_status"] = response.http_status
                entry["state"] = response.resolution["state"]
                entry["required_available"] = response.resolution["required_available"]
                entry["data"] = response.data.decode("latin1") if response.data else ""
                entry["headers"] = response.headers
                entry["sources"] = response.resolution["sources"]
                entry["empty_pixels"] = response.resolution["empty_pixels"]
                entry["unavailable_pixels"] = response.resolution["unavailable_pixels"]
                entry["explanation"] = response.resolution["explanation"]
                entry["no_fallback_reasons"] = response.resolution["no_fallback_reasons"]

            elif action == "revoke_claim":
                out = service.revoke_claim(event["layer"], event.get("actor"), now=at)
                entry["ok"] = True
                entry["version"] = out["version"]["id"]
                entry["claim"] = out["claim"]

            elif action in {"query_usage", "usage"}:
                report = service.reference_report(now=at if isinstance(at, (int, float)) else None)
                entry["ok"] = True
                entry["usage"] = _jsonable(report)

            elif action == "gc":
                entry["gc"] = service.gc(dry_run=bool(event.get("dry_run", False)), now=at)

            elif action == "upload_blob":
                data = event.get("data", "").encode("latin1") if isinstance(event.get("data"), str) else b""
                meta = service.add_blob(data, name=event.get("name"), now=at)
                entry["blob"] = meta

            elif action in {"register_claim", "claim", "enable_layer"}:
                claim = service.register_claim(
                    event["layer"], event["actor"], int(event["z"]), event["bounds"],
                    int(event.get("priority", 0)), claim_id=event.get("id"),
                    planned_bytes=int(event.get("planned_bytes", 0)), now=at,
                )
                entry["claim"] = claim

            elif action == "resume":
                out = service.resume_pickup(event["id"], now=at)
                entry["resume"] = _jsonable(out)

            elif action in {"explain", "resolve"}:
                res = service.resolve(event["tile"], version=event.get("version"))
                entry["resolution"] = _jsonable(res)

            elif action == "delete_pickup":
                entry["deleted"] = service.delete_pickup(event["id"])

            elif action in {"reclaim_layer", "reclaim_layer_revision"}:
                entry["result"] = _jsonable(service.reclaim_layer_revision(
                    event["layer"], int(event["revision"]), now=at
                ))

            else:
                entry["ok"] = False
                entry["error"] = f"unknown action {action!r}"
                if strict:
                    raise ValueError(entry["error"])
        except TileServiceError as exc:
            entry["ok"] = False
            entry["error"] = exc.code
            entry["message"] = str(exc)
            entry["details"] = exc.details
            if strict:
                raise
        results.append(entry)

    final_usage = service.reference_report()
    pickups = {pid: service.pickup_manifest(pid) for pid in service.state["pickups"]}
    return {"results": results, "usage": _jsonable(final_usage), "pickups": _jsonable(pickups)}


def replay_fixture(fixture_dir: str | Path, data_dir: str | Path | None = None) -> dict:
    service, record = load_fixture_service(fixture_dir, data_dir)
    replay = replay_record(service, record)
    replay["fixture"] = str(fixture_dir)
    return replay


def _summary_manifest(manifest: dict) -> dict:
    return {
        key: manifest[key]
        for key in (
            "id", "version", "total", "copied_count", "pending_count",
            "unavailable_count", "copy_complete", "usable_offline",
            "full_fidelity_complete", "interrupted", "holds",
        )
        if key in manifest
    }


def _public_fetch_item(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in ("tile", "key", "status", "attempts", "file", "rendered_sha256", "required_unavailable")
    }


def _jsonable(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if k != "decisions"}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("latin1")
    return value
