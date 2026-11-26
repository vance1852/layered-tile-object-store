"""Versioned, claim-aware sparse tile storage.

The central object is :class:`TileService`.  It is persistence-aware but does
not require HTTP; the HTTP adapter is intentionally thin.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Iterable

from .errors import (
    CapacityError,
    ClaimDeniedError,
    ConflictError,
    NotFoundError,
    PickupStateError,
    ReferencedObjectError,
    TileUnavailableError,
    ValidationError,
)
from .geometry import (
    PIXEL_COUNT,
    ancestor_at,
    iter_tiles_in_extent,
    parse_bounds,
    parse_tile,
    pixel_mask,
    project_raster,
    source_pixel,
    tile_geometry,
    tile_key,
    tiles_inside_geometry,
)

# Re-exported for callers that prefer one import.
__all__ = ["TileService", "AttrDict", "tile_key"]


class AttrDict(dict):
    """Dictionary with attribute access, useful to both scripts and tests."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - convenience path
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes_default(value):
    if isinstance(value, bytes):
        return value.decode("latin1")
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _geom_value(geom: tuple[int, int, int, int, int] | None):
    return list(geom) if geom is not None else None


def _parse_geom(value) -> tuple[int, int, int, int, int] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        z = int(value["z"])
        bounds = parse_bounds(value.get("bounds", value.get("rect")), zoom=z)
        return (z, *bounds)
    if isinstance(value, (list, tuple)) and len(value) == 5:
        z, x0, y0, x1, y1 = (int(v) for v in value)
        parse_bounds([x0, y0, x1, y1], zoom=z)
        return z, x0, y0, x1, y1
    raise ValidationError("area must be {z,bounds} or [z,x0,y0,x1,y1]")


class TileService:
    """A thread-safe JSON/blob backed tile service.

    With ``data_dir=None`` the service keeps blob bytes in memory.  Tests can
    therefore construct one quickly; a real server supplies a directory.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike | int | None = None,
        capacity_bytes: int | None = None,
        supported_levels: Iterable[int] | None = (0, 1, 2, 3),
        clock=None,
        **options,
    ):
        # Tolerate TileService(capacity_bytes) as well as TileService(data_dir).
        if isinstance(data_dir, int) and capacity_bytes is None:
            capacity_bytes = int(data_dir)
            data_dir = None
        if data_dir is None:
            data_dir = options.get("root") or options.get("path") or options.get("directory") or options.get("storage_dir")
        if capacity_bytes is None:
            capacity_bytes = options.get("capacity") or options.get("quota_bytes")
        if "levels" in options and supported_levels == (0, 1, 2, 3):
            supported_levels = options["levels"]
        self.data_dir = Path(data_dir) if data_dir is not None else None
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "blobs").mkdir(exist_ok=True)
            (self.data_dir / "pickups").mkdir(exist_ok=True)
            self.state_path = self.data_dir / "state.json"
        else:
            self.state_path = None
        self._lock = threading.RLock()
        self._blob_memory: dict[str, bytes] = {}
        self.clock = clock or _epoch_seconds
        self.state = self._load_or_init(capacity_bytes, sorted(set(supported_levels or [])))

    # ------------------------------------------------------------------ state
    def _load_or_init(self, capacity_bytes, supported_levels) -> dict:
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if capacity_bytes is not None:
                state["capacity_bytes"] = int(capacity_bytes)
            if supported_levels:
                state["supported_levels"] = sorted(set(state.get("supported_levels", [])) | set(supported_levels))
            return state
        state = self._fresh_state(capacity_bytes, supported_levels)
        self._save_locked(state)
        return state

    @staticmethod
    def _fresh_state(capacity_bytes, supported_levels) -> dict:
        capacity = int(capacity_bytes) if capacity_bytes is not None else 1 << 40
        return {
            "schema": 1,
            "capacity_bytes": capacity,
            "supported_levels": list(supported_levels),
            "head_version": 0,
            "versions": [
                {
                    "id": 0,
                    "parent": None,
                    "kind": "genesis",
                    "claims": [],
                    "heads": {},
                    "message": "empty logical world",
                }
            ],
            "claims": [],
            "revisions": {},
            "heads": {},
            "blobs": {},
            "pickups": {},
            "downloads": {},
            "logical_clock": 0,
        }

    def _save_locked(self, state: dict | None = None) -> None:
        state = state or self.state
        if not self.state_path:
            return
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def close(self) -> None:  # pragma: no cover - server lifecycle symmetry
        with self._lock:
            self._save_locked()

    # ----------------------------------------------------------------- policy
    def install_policy(self, policy: dict, *, reset: bool = False) -> dict:
        """Install claims/capacity from a fixture ``policy`` object."""
        with self._lock:
            if reset:
                supported = self.state.get("supported_levels", (0, 1, 2, 3))
                if self.data_dir is not None:
                    shutil.rmtree(self.data_dir / "blobs", ignore_errors=True)
                    shutil.rmtree(self.data_dir / "pickups", ignore_errors=True)
                    (self.data_dir / "blobs").mkdir(exist_ok=True)
                    (self.data_dir / "pickups").mkdir(exist_ok=True)
                self._blob_memory = {}
                self.state = self._fresh_state(policy.get("capacity_bytes"), supported)
            if "capacity_bytes" in policy:
                self.state["capacity_bytes"] = int(policy["capacity_bytes"])
            claims = []
            for raw in policy.get("claims", []):
                z = int(raw["z"])
                bounds = parse_bounds(raw["bounds"], zoom=z)
                claim = {
                    "id": raw.get("id") or f"{raw['layer']}/{raw['actor']}",
                    "layer": str(raw["layer"]),
                    "actor": str(raw["actor"]),
                    "z": z,
                    "bounds": list(bounds),
                    "priority": int(raw.get("priority", 0)),
                    "active": True,
                    "installed": True,
                }
                claims.append(claim)
            self.state["claims"] = claims
            self.state["versions"][0]["claims"] = copy.deepcopy(claims)
            self.state["versions"][0]["heads"] = {}
            self._save_locked()
            return AttrDict({"claims": claims, "capacity_bytes": self.state["capacity_bytes"]})

    # Domain-language policy aliases.
    load_policy = install_policy
    apply_policy = install_policy

    def list_claims(self, active_only: bool = True) -> list[dict]:
        with self._lock:
            claims = copy.deepcopy(self.state["claims"])
        if active_only:
            claims = [c for c in claims if c.get("active", True)]
        return claims

    def _find_claim_locked(self, layer: str, actor: str | None = None) -> dict:
        matches = [
            c for c in self.state["claims"]
            if c["layer"] == layer and c.get("active", True) and (actor is None or c["actor"] == actor)
        ]
        if not matches:
            raise ClaimDeniedError(f"no active claim for layer {layer!r}" + (f" actor {actor!r}" if actor else ""))
        if actor is None and len(matches) > 1:
            raise ConflictError("multiple claims match; specify actor")
        return matches[0]

    def register_claim(
        self,
        layer: str,
        actor: str,
        z: int,
        bounds,
        priority: int = 0,
        *,
        claim_id: str | None = None,
        planned_bytes: int = 0,
        now: float | None = None,
    ) -> dict:
        with self._lock:
            # Enabling must pass real capacity admission first.  Projected GC
            # savings are deliberately not added to free space.
            self.check_capacity(int(planned_bytes))
            b = parse_bounds(bounds, zoom=int(z))
            cid = claim_id or f"{layer}/{actor}"
            if any(c["id"] == cid and c.get("active", True) for c in self.state["claims"]):
                raise ConflictError(f"active claim {cid!r} already exists")
            claim = {
                "id": cid,
                "layer": layer,
                "actor": actor,
                "z": int(z),
                "bounds": list(b),
                "priority": int(priority),
                "active": True,
                "created_at": self._now(now),
            }
            self.state["claims"].append(claim)
            self._append_version_locked("claim_registered", layer=layer, claim_id=cid)
            return copy.deepcopy(claim)

    def enable_layer(self, *args, planned_bytes: int = 0, **kwargs) -> dict:
        """Alias for claim registration with explicit capacity semantics."""
        return self.register_claim(*args, planned_bytes=planned_bytes, **kwargs)

    def revoke_claim(self, layer: str, actor: str | None = None, *, now: float | None = None) -> dict:
        with self._lock:
            claim = self._find_claim_locked(layer, actor)
            claim["active"] = False
            claim["revoked_at"] = self._now(now)
            version = self._append_version_locked("claim_revoked", layer=layer, actor=claim["actor"], claim_id=claim["id"])
            return AttrDict({"claim": copy.deepcopy(claim), "version": version})

    withdraw_layer = revoke_claim

    # ----------------------------------------------------------------- blobs
    def _now(self, now: float | None) -> float:
        return float(now if now is not None else self.clock())

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return sum(int(b["bytes"]) for b in self.state["blobs"].values())

    @property
    def free_bytes(self) -> int:
        return max(0, int(self.state["capacity_bytes"]) - self.used_bytes)

    def check_capacity(self, extra_bytes: int) -> None:
        extra = int(extra_bytes)
        if extra < 0:
            raise ValidationError("extra_bytes must be non-negative")
        used = self.used_bytes
        capacity = int(self.state["capacity_bytes"])
        if used + extra > capacity:
            # Do not count gc_plan()['reclaimable_bytes'] here.  The operator
            # must audit and reclaim first, making the space actually free.
            raise CapacityError(
                f"need {extra} bytes but only {max(0, capacity - used)} are free",
                used_bytes=used,
                capacity_bytes=capacity,
                free_bytes=max(0, capacity - used),
                projected_reclaimable_bytes=self.gc_plan().get("reclaimable_bytes", 0),
                admission_basis="actual_used_bytes_only",
            )

    def add_blob(
        self,
        data: bytes,
        *,
        name: str | None = None,
        expected_sha256: str | None = None,
        now: float | None = None,
    ) -> dict:
        if not isinstance(data, (bytes, bytearray)):
            raise ValidationError("blob data must be bytes")
        data = bytes(data)
        digest = _sha256(data)
        if expected_sha256 and expected_sha256 != digest:
            raise ValidationError("sha256 mismatch", expected=expected_sha256, actual=digest)
        with self._lock:
            if digest in self.state["blobs"]:
                if name and name not in self.state["blobs"][digest].setdefault("names", []):
                    self.state["blobs"][digest]["names"].append(name)
                    self._save_locked()
                return AttrDict(self.state["blobs"][digest])
            self.check_capacity(len(data))
            if self.data_dir is not None:
                prefix = self.data_dir / "blobs" / digest[:2]
                prefix.mkdir(parents=True, exist_ok=True)
                path = prefix / f"{digest}.bin"
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
            else:
                self._blob_memory[digest] = data
            meta = {
                "sha256": digest,
                "bytes": len(data),
                "names": [name] if name else [],
                "stored_at": self._now(now),
            }
            self.state["blobs"][digest] = meta
            self._save_locked()
            return AttrDict(copy.deepcopy(meta))

    def has_blob(self, digest: str) -> bool:
        with self._lock:
            return digest in self.state["blobs"]

    def sha_for_name(self, name: str) -> str:
        with self._lock:
            for sha, meta in self.state["blobs"].items():
                if name in meta.get("names", []):
                    return sha
        raise NotFoundError(f"no uploaded blob named {name!r}")

    def get_blob(self, digest: str) -> bytes:
        with self._lock:
            if digest not in self.state["blobs"]:
                raise NotFoundError(f"blob {digest} is absent")
            if self.data_dir is None:
                return self._blob_memory[digest]
            path = self.data_dir / "blobs" / digest[:2] / f"{digest}.bin"
            return path.read_bytes()

    # -------------------------------------------------------------- revisions
    def _append_version_locked(self, kind: str, **extra) -> dict:
        vid = int(self.state["head_version"]) + 1
        version = {
            "id": vid,
            "parent": self.state["head_version"],
            "kind": kind,
            "claims": copy.deepcopy([c for c in self.state["claims"] if c.get("active", True)]),
            "heads": copy.deepcopy(self.state["heads"]),
            "created_at": self._now(extra.pop("now", None)),
        }
        version.update(extra)
        self.state["versions"].append(version)
        self.state["head_version"] = vid
        self._save_locked()
        return copy.deepcopy(version)

    def get_version(self, version: int | str | None = None) -> dict:
        with self._lock:
            if version is None or version == "head":
                vid = int(self.state["head_version"])
            else:
                vid = int(str(version).lstrip("vV"))
            for doc in self.state["versions"]:
                if doc["id"] == vid:
                    return copy.deepcopy(doc)
        raise NotFoundError(f"logical version {version!r} not found")

    def list_versions(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.state["versions"])

    def _revision_id(self, layer: str, revision: int) -> str:
        return f"{layer}@{int(revision)}"

    def get_revision(self, layer: str, revision: int | None = None) -> dict:
        with self._lock:
            if revision is None:
                revision = int(self.state["heads"].get(layer, 0))
            rid = self._revision_id(layer, int(revision))
            if rid not in self.state["revisions"]:
                raise NotFoundError(f"layer revision {rid} not found")
            return copy.deepcopy(self.state["revisions"][rid])

    def submit_update(
        self,
        layer: str | None = None,
        actor: str | None = None,
        base: int | None = None,
        tiles: Iterable[dict | Sequence] | None = None,
        revision: int | None = None,
        *,
        base_version: int | None = None,
        base_revision: int | None = None,
        area=None,
        removed: Iterable | None = None,
        message: str | None = None,
        now: float | None = None,
        **extra,
    ) -> dict:
        if isinstance(layer, dict):
            event = layer
            layer = event.get("layer")
            actor = event.get("actor", actor)
            base = event.get("base", event.get("base_version", base))
            tiles = event.get("tiles", tiles)
            revision = event.get("revision", revision)
            area = event.get("area", area)
            removed = event.get("removed", removed)
            message = event.get("message", message)
        if base is None:
            base = base_version if base_version is not None else base_revision
        if base is None:
            base = 0
        if tiles is None:
            tiles = extra.get("tile_list") or extra.get("tile_declarations") or []
        if not layer:
            raise ValidationError("layer is required for an update")
        with self._lock:
            claim = self._find_claim_locked(layer, actor)
            current = int(self.state["heads"].get(layer, 0))
            if int(base) != current:
                raise ConflictError(
                    f"base revision {base} is not current revision {current} for layer {layer!r}",
                    layer=layer,
                    base=int(base),
                    current_revision=current,
                )
            new_rev = int(revision) if revision is not None else current + 1
            if new_rev != current + 1:
                raise ConflictError(f"next revision must be {current + 1}", requested=new_rev)
            rid = self._revision_id(layer, new_rev)
            if rid in self.state["revisions"]:
                raise ConflictError(f"revision {rid} already exists")

            if current:
                base_doc = self.state["revisions"][self._revision_id(layer, current)]
                tile_docs = copy.deepcopy(base_doc.get("tiles", {}))
            else:
                tile_docs = {}
            geom = _parse_geom(area)

            normalized = []
            for raw in removed or []:
                coord = parse_tile(raw)
                key = tile_key(*coord)
                tile_docs.pop(key, None)
                normalized.append({"tile": list(coord), "op": "remove"})
            for raw in tiles or []:
                tile_input = self._normalize_tile_input(raw)
                coord = (tile_input["z"], tile_input["x"], tile_input["y"])
                key = tile_key(*coord)
                op = tile_input.get("op", "put")
                if tile_input.get("kind") in {"remove", "removed", "absent", "delete"}:
                    op = "remove"
                if op in {"remove", "absent", "delete"}:
                    tile_docs.pop(key, None)
                    normalized.append({"tile": list(coord), "op": "remove"})
                    continue
                kind = tile_input.get("kind", "content")
                if kind not in {"content", "empty", "expected"}:
                    raise ValidationError("tile kind must be content, empty, or expected", kind=kind)
                sha = tile_input.get("sha256") or tile_input.get("sha")
                ref_name = tile_input.get("name") or tile_input.get("file") or tile_input.get("filename")
                if kind == "content" and not sha and ref_name:
                    sha = self.sha_for_name(ref_name)
                data = tile_input.get("data")
                if data is not None:
                    meta = self.add_blob(data, name=tile_input.get("name"), now=now)
                    sha = meta["sha256"]
                if kind == "content":
                    if not sha:
                        raise ValidationError("content tile requires data or sha256", tile=key)
                    if not self.has_blob(sha):
                        raise NotFoundError(f"content blob {sha} has not been uploaded", tile=key)
                elif sha:
                    # Empty/expected records must not silently carry an object.
                    raise ValidationError(f"{kind} tile cannot carry sha256", tile=key)
                rec = {
                    "z": coord[0],
                    "x": coord[1],
                    "y": coord[2],
                    "kind": kind,
                    "sha256": sha if kind == "content" else None,
                    "op": "put",
                    # The area belongs to the update that introduced this
                    # record.  Inherited cumulative records retain their own
                    # stamp rather than being clipped by a newer patch area.
                    "area": _geom_value(geom),
                }
                claim_geom: tuple[int, int, int, int, int] = (
                    claim["z"], claim["bounds"][0], claim["bounds"][1], claim["bounds"][2], claim["bounds"][3]
                )
                rec["within_claim"] = self._geometry_intersects(tile_geometry(coord), claim_geom)
                if not rec["within_claim"]:
                    rec["service_effect"] = "excluded_outside_claim"
                tile_docs[key] = rec
                normalized.append(copy.deepcopy(rec))

            doc = {
                "id": rid,
                "layer": layer,
                "actor": actor,
                "revision": new_rev,
                "base": int(base),
                "area": _geom_value(geom),
                "tiles": tile_docs,
                "submitted_at": self._now(now),
                "reclaimed": False,
                "message": message,
                "claim_id": claim["id"],
            }
            self.state["revisions"][rid] = doc
            self.state["heads"][layer] = new_rev
            version = self._append_version_locked(
                "layer_update",
                layer=layer,
                actor=actor,
                revision=new_rev,
                base=int(base),
                message=message,
                now=now,
            )
            return AttrDict({"revision": copy.deepcopy(doc), "version": version, "accepted_tiles": normalized})

    def upload_tile(self, layer=None, a=None, b=None, c=None, d=None, **kwargs):
        """One-step blob upload + sparse layer update.

        Supports ``upload_tile(layer, (z,x,y), data, ...)`` and
        ``upload_tile(layer, z, x, y, data, ...)`` plus keyword coordinates.
        """
        actor = kwargs.pop("actor", None)
        kind = kwargs.pop("kind", "content")
        area = kwargs.pop("area", None)
        message = kwargs.pop("message", None)
        now = kwargs.pop("now", None)
        base = kwargs.pop("base", kwargs.pop("base_version", None))
        revision = kwargs.pop("revision", None)
        data = kwargs.pop("data", kwargs.pop("body", kwargs.pop("content", None)))
        coord = kwargs.pop("tile", kwargs.pop("coord", None))

        if d is not None:  # layer, z, x, y, data
            z, x, y, data = a, b, c, d
            coord = (z, x, y)
        elif coord is None and b is not None:  # layer, coord, data
            coord, data = a, b
        elif coord is None and a is not None:
            coord = a
        if coord is None and all(k in kwargs for k in ("z", "x", "y")):
            coord = (kwargs.pop("z"), kwargs.pop("x"), kwargs.pop("y"))
        if data is None and "sha256" not in kwargs and "sha" not in kwargs:
            raise ValidationError("upload_tile requires data or sha256")
        if isinstance(data, str):
            data = data.encode("latin1")
        sha = kwargs.pop("sha256", kwargs.pop("sha", None))
        if data is not None:
            sha = self.add_blob(data, name=kwargs.pop("name", None), expected_sha256=sha, now=now)["sha256"]
        if base is None:
            base = int(self.state["heads"].get(layer, 0))
        tile = {"tile": coord, "kind": kind, "sha256": sha}
        tile.update(kwargs.pop("tile_fields", {}))
        return self.submit_update(
            layer, actor, base, [tile], revision, area=area, message=message, now=now, **kwargs
        )

    upload_overlay = upload_tile
    put_tile = upload_tile

    @staticmethod
    def _geometry_intersects(a, b) -> bool:
        az, ax0, ay0, ax1, ay1 = a
        bz, bx0, by0, bx1, by1 = b
        # Convert b into a's zoom coordinates.  Integer zoom ratios make the
        # ordinary rectangle comparison sufficient for our aligned claims.
        if bz != az:
            from .geometry import Fraction
            scale = Fraction(1 << az, 1 << bz)
            bx0 = bx0 * scale
            bx1 = bx1 * scale
            by0 = by0 * scale
            by1 = by1 * scale
        return max(ax0, bx0) < min(ax1, bx1) and max(ay0, by0) < min(ay1, by1)

    @staticmethod
    def _normalize_tile_input(raw) -> dict:
        if isinstance(raw, dict):
            out = dict(raw)
            if "coord" in out and isinstance(out["coord"], (list, tuple, str)):
                z, x, y = parse_tile(out["coord"])
                out.update(z=z, x=x, y=y)
            elif "tile" in out:
                z, x, y = parse_tile(out["tile"])
                out.update(z=z, x=x, y=y)
            if not all(k in out for k in ("z", "x", "y")):
                raise ValidationError("tile object requires z/x/y or tile")
            parse_tile((out["z"], out["x"], out["y"]))
            return out
        if isinstance(raw, (list, tuple)):
            # Fixture compact form: [z, x, y, kind, file-or-sha/null].
            if len(raw) == 5:
                z, x, y, kind, ref = raw
                out = {"z": z, "x": x, "y": y, "kind": kind}
                if ref is not None:
                    if isinstance(ref, str) and len(ref) == 64:
                        out["sha256"] = ref
                    else:
                        out["name"] = ref
                return out
            if len(raw) == 4:
                z, x, y, kind = raw
                return {"z": z, "x": x, "y": y, "kind": kind}
        raise ValidationError(f"unrecognized tile declaration: {raw!r}")

    # -------------------------------------------------------------- resolution
    def resolve(self, *args, version: int | str | None = None, **kwargs) -> AttrDict:
        if "z" in kwargs and "x" in kwargs and "y" in kwargs:
            tile = (kwargs["z"], kwargs["x"], kwargs["y"])
        elif len(args) == 3:
            tile = args[:3]
        elif len(args) == 4:
            tile = args[:3]
            version = version if version is not None else args[3]
        elif len(args) == 2 and not isinstance(args[0], int):
            tile, version = args
        elif len(args) == 1:
            tile = args[0]
        else:
            raise ValidationError("call resolve([z,x,y]) or resolve(z,x,y)")
        coord = parse_tile(tile)
        z, x, y = coord
        with self._lock:
            vdoc = self.get_version(version)
            claim_docs = copy.deepcopy(vdoc["claims"])
            for claim in claim_docs:
                claim["_revision"] = vdoc["heads"].get(claim["layer"], 0)
            # Highest priority first; deterministic tie-break.
            claim_docs.sort(key=lambda c: (-int(c["priority"]), c["layer"], c["actor"]))
            claim_masks = {c["id"]: pixel_mask(coord, (c["z"], *c["bounds"])) for c in claim_docs}

            pixel_decisions: list[dict] = []
            source_map: dict[str, dict] = {}
            owner_by_pixel: list[str | None] = []

            for p in range(PIXEL_COUNT):
                col, row = p % 4, p // 4
                owner = None
                matches = []
                for claim in claim_docs:
                    if not claim_masks[claim["id"]][p]:
                        continue
                    if owner is None:
                        owner = claim["id"]
                    rev_no = vdoc["heads"].get(claim["layer"])
                    if not rev_no:
                        continue
                    rev = self.state["revisions"].get(self._revision_id(claim["layer"], int(rev_no)))
                    if not rev or rev.get("reclaimed"):
                        continue
                    best = None
                    for best_candidate in rev.get("tiles", {}).values():
                        rec_coord = (best_candidate["z"], best_candidate["x"], best_candidate["y"])
                        if not pixel_mask(coord, tile_geometry(rec_coord))[p]:
                            continue
                        rec_area = tuple(best_candidate["area"]) if best_candidate.get("area") else None
                        if rec_area is not None and not pixel_mask(coord, rec_area)[p]:
                            continue
                        if best_candidate.get("within_claim") is False:
                            continue
                        if best is None or best_candidate["z"] > best["z"]:
                            best = best_candidate
                    if best is not None:
                        matches.append({"claim": claim, "record": copy.deepcopy(best)})

                owner_by_pixel.append(owner)
                decision = self._decide_pixel_locked(coord, p, col, row, owner, matches, source_map)
                pixel_decisions.append(decision)

            result = self._build_resolution_result(coord, vdoc, pixel_decisions, source_map)
            return AttrDict(result)

    def _decide_pixel_locked(self, target, p, col, row, owner_id, matches, source_map) -> dict:
        base = {
            "pixel": p,
            "row": row,
            "col": col,
            "owner_claim_id": owner_id,
            "candidate_layers": [
                {
                    "layer": m["claim"]["layer"],
                    "actor": m["claim"]["actor"],
                    "priority": m["claim"]["priority"],
                    "revision": self._rev_no(m),
                    "kind": m["record"]["kind"],
                    "tile": [m["record"]["z"], m["record"]["x"], m["record"]["y"]],
                    "sha256": m["record"].get("sha256"),
                }
                for m in matches
            ],
        }
        if not matches:
            base.update(status="undeclared", reason="no_layer_declaration_at_pixel")
            return base

        top = matches[0]
        tc, tr = top["claim"], top["record"]
        top_source = self._source_descriptor(target, col, row, top, source_map)
        base["primary"] = top_source

        if tr["kind"] == "empty":
            base.update(
                status="empty",
                reason="explicit_empty_blocks_inheritance",
                selected=top_source,
                fallback=None,
                blocked_candidates=[self._candidate_brief(m) for m in matches[1:]],
                no_fallback_reason="explicit_empty_within_declared_scope",
            )
            return base

        required_status = "content"
        if tr["kind"] == "expected":
            required_status = "unavailable"
            reason = "required_tile_expected_but_not_uploaded"
        elif tr["kind"] == "content" and not self.has_blob(tr["sha256"]):
            required_status = "unavailable"
            reason = "required_content_blob_is_missing"
        else:
            reason = "highest_priority_declaration"

        selected = None
        fallback = None
        blocked_reason = None
        unavailable = []

        if required_status == "content":
            selected = top_source
            selected["relation"] = self._relation(target, tr)
            if selected.get("projected") or (owner_id and tc["id"] != owner_id):
                fallback = selected
        else:
            unavailable.append(self._candidate_brief(top))

        # Find the highest-priority usable content below an unavailable top.
        if selected is None:
            for m in matches[1:]:
                rec = m["record"]
                if rec["kind"] == "empty":
                    blocked_reason = "lower_explicit_empty_blocks_inheritance"
                    break
                if rec["kind"] == "expected":
                    unavailable.append(self._candidate_brief(m))
                    continue
                sha = rec.get("sha256")
                if rec["kind"] == "content" and sha and self.has_blob(sha):
                    fallback = self._source_descriptor(target, col, row, m, source_map)
                    fallback["alternative"] = True
                    fallback["alternative_reason"] = "required_source_unavailable_using_lower_priority_source"
                    selected = fallback
                    break
                unavailable.append(self._candidate_brief(m))

        owner_is_top = owner_id == tc["id"]
        priority_alternative = not owner_is_top
        projected = bool(top_source.get("projected"))
        if required_status == "content":
            if tr["kind"] == "content" and not projected and owner_is_top:
                status = "content"
            else:
                status = "alternative"
                top_source["alternative"] = True
                if priority_alternative:
                    top_source["alternative_reason"] = "lower_priority_layer_marked_as_source"
                elif projected:
                    top_source["alternative_reason"] = "projected_from_other_scale"
        elif selected is not None:
            status = "alternative"
        else:
            status = "unavailable"

        no_fallback_reason = None
        if status == "unavailable":
            no_fallback_reason = blocked_reason or "required_object_unavailable_without_usable_lower_source"

        base.update(
            status=status,
            required_status=required_status,
            reason=reason,
            selected=selected,
            fallback=fallback,
            unavailable=unavailable,
            blocked_reason=blocked_reason,
            no_fallback_reason=no_fallback_reason,
            priority_alternative=priority_alternative,
            projected=projected,
        )
        return base

    @staticmethod
    def _rev_no(match) -> int:
        # The record does not itself duplicate revision; caller's claim maps to
        # a head.  _source_descriptor adds it.  Keep this helper for JSON shape.
        return match["claim"].get("_revision", 0)

    def _candidate_brief(self, match) -> dict:
        c, r = match["claim"], match["record"]
        return {
            "layer": c["layer"],
            "actor": c["actor"],
            "priority": c["priority"],
            "kind": r["kind"],
            "tile": [r["z"], r["x"], r["y"]],
            "sha256": r.get("sha256"),
        }

    def _source_descriptor(self, target, col, row, match, source_map) -> dict:
        c, r = match["claim"], match["record"]
        source_coord = (r["z"], r["x"], r["y"])
        sp = source_pixel(target, col, row, source_coord)
        relation = self._relation(target, r)
        desc = {
            "layer": c["layer"],
            "actor": c["actor"],
            "priority": c["priority"],
            "revision": int(c.get("_revision") or 0),
            "claim_id": c["id"],
            "kind": r["kind"],
            "tile": list(source_coord),
            "sha256": r.get("sha256"),
            "relation": relation,
            "source_pixel": list(sp),
            "projected": source_coord != target,
            "alternative": source_coord != target,
        }
        if r.get("sha256"):
            # The same bytes can legitimately be referenced by multiple layers
            # after content de-duplication.  Key by full attribution so one
            # layer's descriptor cannot clobber another's.
            key = json.dumps(
                [r["sha256"], c["layer"], c["actor"], desc["revision"], list(source_coord), relation],
                sort_keys=True,
                ensure_ascii=False,
            )
            source_map[key] = {
                "sha256": r["sha256"],
                "layer": c["layer"],
                "actor": c["actor"],
                "revision": desc["revision"],
                "tile": list(source_coord),
                "relation": relation,
            }
        return desc

    @staticmethod
    def _relation(target, record) -> str:
        tz = target[0]
        rz = record["z"]
        if (record["z"], record["x"], record["y"]) == target:
            return "exact"
        if rz < tz:
            return "ancestor_projection"
        if rz > tz:
            return "descendant_sample"
        return "same_zoom_clip"

    def _build_resolution_result(self, coord, vdoc, decisions, source_map) -> dict:
        raster = bytearray(b" " * PIXEL_COUNT)
        renderable = True
        required_available = True
        all_empty = True
        all_undeclared = True
        has_alternative = False
        unavailable_regions = []
        empty_regions = []
        undeclared_regions = []
        source_hashes = []

        for d in decisions:
            p = d["pixel"]
            status = d["status"]
            if status != "empty":
                all_empty = False
            if status != "undeclared":
                all_undeclared = False
            if status == "empty":
                empty_regions.append(p)
                raster[p] = ord(" ")
                continue
            if status == "undeclared":
                undeclared_regions.append(p)
                raster[p] = ord("?")
                continue
            if d.get("required_status") == "unavailable":
                required_available = False
                unavailable_regions.append(p)
            selected = d.get("selected")
            if not selected or not selected.get("sha256"):
                renderable = False
                raster[p] = ord("!")
                continue
            sha = selected["sha256"]
            if sha not in source_hashes:
                source_hashes.append(sha)
            try:
                data = self.get_blob(sha)
            except NotFoundError:
                renderable = False
                raster[p] = ord("!")
                continue
            source_coord = tuple(selected["tile"])
            if len(data) == PIXEL_COUNT:
                if source_coord == coord:
                    raster[p] = data[p]
                else:
                    sc, sr = selected["source_pixel"]
                    raster[p] = data[sr * 4 + sc]
            elif source_coord == coord and len(data) > p:
                raster[p] = data[p]
            else:
                renderable = False
                raster[p] = ord("?")
            if status == "alternative":
                has_alternative = True

        # Preserve arbitrary exact object bytes.  Repository fixtures use
        # 16-byte 4x4 rasters, but storage/reference tests may intentionally
        # use short opaque byte strings; do not turn those into '?' canvases.
        exact_descriptors = [d for d in source_map.values() if d.get("relation") == "exact" and tuple(d["tile"]) == coord]
        if (
            len(source_hashes) == 1
            and exact_descriptors
            and not empty_regions
            and not undeclared_regions
            and not unavailable_regions
        ):
            exact_data = self.get_blob(source_hashes[0])
            if len(exact_data) != PIXEL_COUNT:
                raster = bytearray(exact_data)
                renderable = True

        if all_empty:
            state = "empty"
        elif all_undeclared:
            state = "undeclared"
        elif not renderable:
            state = "unavailable"
        elif unavailable_regions:
            # A required/expected object is missing, but a marked lower source
            # still produces best-effort bytes.  This is deliberately not
            # collapsed into a normal "alternative" or "empty" state.
            state = "degraded"
        elif has_alternative:
            state = "alternative"
        elif empty_regions or undeclared_regions:
            state = "mixed"
        else:
            state = "content"

        primary_status = "unavailable" if unavailable_regions else state

        # Attach revision numbers into candidate descriptors after the fact.
        for d in decisions:
            for cand in d.get("candidate_layers", []):
                cand["revision"] = vdoc["heads"].get(cand["layer"], 0)
            if d.get("primary"):
                d["primary"]["revision"] = vdoc["heads"].get(d["primary"]["layer"], 0)
            if d.get("selected"):
                d["selected"]["revision"] = vdoc["heads"].get(d["selected"]["layer"], 0)

        no_fallback_reasons = sorted({
            d["no_fallback_reason"] for d in decisions if d.get("no_fallback_reason")
        })
        return {
            "tile": list(coord),
            "version": vdoc["id"],
            "version_kind": vdoc["kind"],
            "state": state,
            "status": state,
            "primary_status": primary_status,
            "required_status": "unavailable" if unavailable_regions else "available",
            "temporarily_unavailable": bool(unavailable_regions),
            "unavailable": bool(unavailable_regions) and not renderable,
            "required_available": required_available,
            "available": renderable,
            "renderable": renderable,
            "has_alternative": has_alternative,
            "raster": bytes(raster),
            "raster_b64": __import__("base64").b64encode(bytes(raster)).decode("ascii"),
            "source_hashes": source_hashes,
            "sources": list(source_map.values()),
            "empty_pixels": empty_regions,
            "unavailable_pixels": unavailable_regions,
            "undeclared_pixels": undeclared_regions,
            "no_fallback_reasons": no_fallback_reasons,
            "decisions": decisions,
            "explanation": self._human_explanation(coord, state, decisions, no_fallback_reasons),
        }

    @staticmethod
    def _human_explanation(coord, state, decisions, no_fallback_reasons) -> str:
        z, x, y = coord
        if state == "empty":
            return f"{z}/{x}/{y}: 显式空白在声明范围内阻止低优先级来源继承。"
        if state == "undeclared":
            return f"{z}/{x}/{y}: 各图层均未在此格内留下声明。"
        if no_fallback_reasons:
            return f"{z}/{x}/{y}: 高优显式空白阻止回退；其余来源见 decisions。"
        if state == "unavailable":
            return f"{z}/{x}/{y}: 必需对象暂不可用，且没有可离线使用的替代。"
        if state == "degraded":
            return f"{z}/{x}/{y}: 高优先级必需对象暂不可用；当前字节是标明来源的低一级/其他比例替代，不能视为完整高分辨率结果。"
        if state == "alternative":
            return f"{z}/{x}/{y}: 当前字节来自低优先级或其他比例层，来源已标明。"
        return f"{z}/{x}/{y}: 已按图层优先级和有效范围选定来源。"

    def tile_response(self, *args, version=None, allow_alternative: bool = True, strict: bool = False, **kwargs) -> AttrDict:
        if "z" in kwargs and "x" in kwargs and "y" in kwargs:
            tile = (kwargs["z"], kwargs["x"], kwargs["y"])
        elif len(args) >= 3:
            tile = args[:3]
            if len(args) >= 4 and version is None:
                version = args[3]
        elif len(args) == 2 and not isinstance(args[0], int):
            tile, version = args
        elif args:
            tile = args[0]
        else:
            raise ValidationError("tile coordinate is required")
        res = self.resolve(tile, version=version)
        state = res["state"]
        headers = {
            "X-Tile-Version": str(res["version"]),
            "X-Tile-State": state,
            "X-Tile-Required-Available": str(res["required_available"]).lower(),
        }
        selected = None
        for d in res["decisions"]:
            if d.get("selected"):
                selected = d["selected"]
                break
        if selected:
            headers["X-Tile-Source-Layer"] = selected["layer"]
            headers["X-Tile-Source-Revision"] = str(selected["revision"])
            headers["X-Tile-Source-Tile"] = "/".join(map(str, selected["tile"]))
            headers["X-Tile-Source-Sha256"] = selected["sha256"] or ""
            headers["X-Tile-Alternative"] = str(bool(selected.get("alternative"))).lower()

        if state == "empty":
            return AttrDict(http_status=204, data=b"", content_type="application/x-empty", headers=headers, resolution=res)
        if state == "undeclared":
            return AttrDict(http_status=404, data=json.dumps({"error": "no_declaration", "resolution": self._jsonable_res(res)}).encode(), content_type="application/json", headers=headers, resolution=res)
        if strict and not res["required_available"]:
            return AttrDict(http_status=503, data=json.dumps({"error": "tile_unavailable", "resolution": self._jsonable_res(res)}).encode(), content_type="application/json", headers=headers, resolution=res)
        if not res["renderable"] or (state == "unavailable" and not allow_alternative):
            return AttrDict(http_status=503, data=json.dumps({"error": "tile_unavailable", "resolution": self._jsonable_res(res)}).encode(), content_type="application/json", headers=headers, resolution=res)
        data = res["raster"]
        if state in {"alternative"} or not res["required_available"]:
            status = 203 if allow_alternative else 503
            if not allow_alternative:
                return AttrDict(http_status=503, data=json.dumps({"error": "tile_unavailable", "resolution": self._jsonable_res(res)}).encode(), content_type="application/json", headers=headers, resolution=res)
        else:
            status = 200
        return AttrDict(http_status=status, data=data, content_type="application/octet-stream", headers=headers, resolution=res)

    @staticmethod
    def _jsonable_res(res: dict) -> dict:
        out = copy.deepcopy(res)
        if isinstance(out.get("raster"), bytes):
            out["raster"] = out["raster"].decode("latin1")
        return out

    def get_tile(self, *args, version=None, **kwargs) -> AttrDict:
        """Convenience alias returning the tile response."""
        return self.tile_response(*args, version=version, **kwargs)

    # Common domain-language aliases whose targets are already defined.
    query_tile = get_tile
    fetch = get_tile
    explain = resolve
    explain_source = resolve
    upload_blob = add_blob

    # --------------------------------------------------------------- pickups
    def create_pickup(
        self,
        pickup_id: str | dict | list | None = None,
        extent=None,
        levels: Iterable[int] | None = None,
        tiles: Iterable | None = None,
        version: int | str | None = None,
        until=None,
        now: float | None = None,
        **kwargs,
    ) -> dict:
        # Tolerate create_pickup(extent, levels) and create_pickup(id, extent, levels).
        if not isinstance(pickup_id, (str, int, type(None))):
            if extent is not None and levels is None:
                levels = extent
            extent = pickup_id
            pickup_id = None
        pickup_id = pickup_id or kwargs.get("id") or kwargs.get("pickup_id")
        with self._lock:
            vdoc = self.get_version(version)
            pin = vdoc["id"]
            if tiles is not None:
                coords = [parse_tile(t) for t in tiles]
            else:
                if extent is None or levels is None:
                    raise ValidationError("pickup requires tiles or extent plus levels")
                coords = iter_tiles_in_extent(extent, levels)
            pid = str(pickup_id) if pickup_id is not None else f"p-{uuid.uuid4().hex[:12]}"
            if pid in self.state["pickups"]:
                raise ConflictError(f"pickup {pid!r} already exists")
            items = []
            holds = []
            for coord in coords:
                res = self.resolve(coord, version=pin)
                for sha in res["source_hashes"]:
                    if sha not in holds:
                        holds.append(sha)
                if res["state"] == "empty":
                    initial_status = "empty"
                    fetched_at = self._now(now)
                    last_error = None
                elif not res["renderable"]:
                    initial_status = "unavailable"
                    fetched_at = None
                    last_error = "required_object_unavailable"
                else:
                    initial_status = "pending"
                    fetched_at = None
                    last_error = None
                items.append(
                    {
                        "tile": list(coord),
                        "key": tile_key(*coord),
                        "status": initial_status,
                        "attempts": 0,
                        "state_at_pin": res["state"],
                        "required_available_at_pin": res["required_available"],
                        "source_hashes": list(res["source_hashes"]),
                        "resolution": self._jsonable_res(res),
                        "file": None,
                        "rendered_sha256": None,
                        "required_unavailable": not res["required_available"],
                        "temporarily_unavailable": not res["required_available"],
                        "incomplete": not res["required_available"],
                        "created_at": self._now(now),
                        "fetched_at": fetched_at,
                        "last_error": last_error,
                    }
                )
            pickup = {
                "id": pid,
                "version": pin,
                "extent": copy.deepcopy(extent) if extent is not None else None,
                "levels": list(levels) if levels is not None else None,
                "until": until,
                "created_at": self._now(now),
                "status": "open",
                "items": items,
                "holds": holds,
            }
            self.state["pickups"][pid] = pickup
            self._save_locked()
            return self.pickup_manifest(pid)

    def pickup_manifest(self, pickup_id: str) -> AttrDict:
        with self._lock:
            pickup = self._pickup_locked(pickup_id)
            items = copy.deepcopy(pickup["items"])
            total = len(items)
            done = [i for i in items if i["status"] in {"content", "alternative", "empty", "composite"}]
            pending = [i for i in items if i["status"] == "pending"]
            unavailable = [i for i in items if i["status"] == "unavailable" or i.get("required_unavailable")]
            alternatives = [i for i in items if i["status"] == "alternative"]
            empties = [i for i in items if i["status"] == "empty"]
            copied = [i for i in items if i.get("file") or i.get("rendered_sha256")]
            manifest = {
                "id": pickup["id"],
                "version": pickup["version"],
                "extent": pickup.get("extent"),
                "levels": pickup.get("levels"),
                "until": pickup.get("until"),
                "created_at": pickup.get("created_at"),
                "total": total,
                "copied_count": len(copied),
                "finished_count": len(done),
                "pending_count": len(pending),
                "alternative_count": len(alternatives),
                "empty_count": len(empties),
                "unavailable_count": len(unavailable),
                "copy_complete": len(done) == total and not pending,
                "usable_offline": all(
                    i["status"] == "empty" or bool(i.get("file") or i.get("rendered_sha256"))
                    for i in items
                ),
                "full_fidelity_complete": all(
                    i["status"] in {"content", "empty", "composite"} and not i.get("required_unavailable")
                    for i in items
                ),
                "interrupted": bool(pending),
                "holds": list(pickup.get("holds", [])),
                "items": [self._public_item(i) for i in items],
            }
            manifest["complete"] = manifest["copy_complete"]
            return AttrDict(manifest)

    @staticmethod
    def _public_item(item: dict) -> dict:
        out = copy.deepcopy(item)
        # Full resolution is useful to maintainers but bulky in list views;
        # keep it because offline provenance requires it.
        return out

    def _pickup_locked(self, pickup_id: str) -> dict:
        if pickup_id not in self.state["pickups"]:
            raise NotFoundError(f"pickup {pickup_id!r} not found")
        return self.state["pickups"][pickup_id]

    def fetch_pickup_tile(self, pickup_id: str, tile, *, now: float | None = None) -> AttrDict:
        coord = parse_tile(tile)
        key = tile_key(*coord)
        with self._lock:
            pickup = self._pickup_locked(pickup_id)
            item = next((i for i in pickup["items"] if i["key"] == key), None)
            if item is None:
                raise NotFoundError(f"tile {key} is not in pickup {pickup_id!r}")
            if item["status"] in {"content", "alternative", "empty", "composite"} and (
                item["status"] == "empty"
                or (
                    self.data_dir
                    and item.get("file")
                    and (self.data_dir / item["file"]).exists()
                )
                or (self.data_dir is None and item.get("rendered_sha256"))
            ):
                return AttrDict({"pickup_id": pickup_id, "item": self._public_item(item), "idempotent": True})

            item["attempts"] = int(item.get("attempts", 0)) + 1
            # Re-resolve the pinned immutable version.  Pickup holds ensure the
            # physical source objects cannot have been reclaimed normally.
            response = self.tile_response(coord, version=pickup["version"], allow_alternative=True)
            res = response.resolution
            item["state_at_pin"] = res["state"]
            item["required_available_at_pin"] = res["required_available"]
            item["source_hashes"] = list(res["source_hashes"])

            if response.http_status == 204 or res["state"] == "empty":
                item.update(status="empty", fetched_at=self._now(now), last_error=None, file=None)
                self._save_locked()
                return AttrDict({"pickup_id": pickup_id, "item": self._public_item(item), "data": b""})

            if response.http_status >= 400 or not response.data:
                item.update(
                    status="unavailable",
                    temporarily_unavailable=True,
                    incomplete=True,
                    last_error=response.data.decode("utf-8", "replace") if response.data else res["state"],
                )
                self._save_locked()
                raise TileUnavailableError(
                    f"tile {key} is not available for offline copy",
                    pickup_id=pickup_id,
                    tile=list(coord),
                    resolution=self._jsonable_res(res),
                    manifest=self.pickup_manifest(pickup_id),
                )

            data = bytes(response.data)
            rendered_sha = _sha256(data)
            rel_path = None
            if self.data_dir is not None:
                tile_dir = self.data_dir / "pickups" / pickup_id / "tiles"
                tile_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{z}_{x}_{y}.bin" if False else f"{coord[0]}_{coord[1]}_{coord[2]}.bin"
                path = tile_dir / filename
                path.write_bytes(data)
                rel_path = str(path.relative_to(self.data_dir))
            else:
                memory_key = f"pickup:{pickup_id}:{key}"
                self._blob_memory[memory_key] = data

            exact = self._is_exact_single_source(res, coord)
            single_marked_source = len(res.get("sources", [])) == 1 and not res.get("empty_pixels")
            if exact and res["required_available"] and res["state"] == "content":
                status = "content"
            elif single_marked_source and res["state"] in {"alternative", "degraded"}:
                status = "alternative"
            else:
                status = "composite"
            if res["state"] == "empty":
                status = "empty"
            item.update(
                status=status,
                file=rel_path,
                memory_key=None if self.data_dir is not None else f"pickup:{pickup_id}:{key}",
                rendered_sha256=rendered_sha,
                fetched_at=self._now(now),
                last_error=None,
                required_unavailable=not res["required_available"],
                temporarily_unavailable=not res["required_available"],
                incomplete=not res["required_available"],
                empty_pixels=res.get("empty_pixels", []),
            )
            # Ensure all sources used by the rendered item are held.
            for sha in res["source_hashes"]:
                if sha not in pickup["holds"]:
                    pickup["holds"].append(sha)
            self._save_locked()
            return AttrDict({"pickup_id": pickup_id, "item": self._public_item(item), "data": data})

    @staticmethod
    def _is_exact_single_source(res, coord) -> bool:
        sources = res.get("sources", [])
        if len(sources) != 1:
            return False
        return tuple(sources[0]["tile"]) == coord and not res.get("empty_pixels") and not res.get("undeclared_pixels")

    def resume_pickup(self, pickup_id: str, *, now: float | None = None) -> dict:
        with self._lock:
            manifest = self.pickup_manifest(pickup_id)
            targets = [
                i["tile"] for i in manifest["items"]
                if i["status"] in {"pending", "unavailable"}
            ]
        fetched = []
        unavailable = []
        for tile in targets:
            try:
                out = self.fetch_pickup_tile(pickup_id, tile, now=now)
                fetched.append(out["item"]["key"])
            except TileUnavailableError as exc:
                unavailable.append({"tile": tile, "details": exc.details})
        final = self.pickup_manifest(pickup_id)
        return AttrDict({"fetched": fetched, "unavailable": unavailable, "manifest": final})

    def pickup_item_bytes(self, pickup_id: str, tile) -> bytes:
        coord = parse_tile(tile)
        key = tile_key(*coord)
        with self._lock:
            pickup = self._pickup_locked(pickup_id)
            item = next((i for i in pickup["items"] if i["key"] == key), None)
            if not item or not (item.get("file") or item.get("rendered_sha256")):
                raise NotFoundError(f"copied item {key} not found")
            if self.data_dir is None:
                return self._blob_memory[item.get("memory_key") or f"pickup:{pickup_id}:{key}"]
            return (self.data_dir / item["file"]).read_bytes()

    def delete_pickup(self, pickup_id: str, *, delete_files: bool = True) -> dict:
        """Release a package's holds after the offline package is retired."""
        with self._lock:
            pickup = self._pickup_locked(pickup_id)
            if delete_files and self.data_dir is not None:
                pkg_dir = self.data_dir / "pickups" / pickup_id
                if pkg_dir.exists():
                    shutil.rmtree(pkg_dir)
            for key in list(self._blob_memory):
                if key.startswith(f"pickup:{pickup_id}:"):
                    self._blob_memory.pop(key, None)
            del self.state["pickups"][pickup_id]
            self._save_locked()
            return {"deleted": pickup_id}

    def export_package_directory(self, pickup_id: str, destination: str | os.PathLike) -> Path:
        """Write a portable unpacked package directory."""
        manifest = self.pickup_manifest(pickup_id)
        dest = Path(destination)
        tiles_dir = dest / "tiles"
        objects_dir = dest / "objects"
        tiles_dir.mkdir(parents=True, exist_ok=True)
        objects_dir.mkdir(parents=True, exist_ok=True)
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_bytes_default),
            encoding="utf-8",
        )
        used = set()
        for item in manifest["items"]:
            if item["status"] not in {"content", "alternative", "composite"}:
                continue
            key = item["key"].replace("/", "_")
            (tiles_dir / f"{key}.bin").write_bytes(self.pickup_item_bytes(pickup_id, item["tile"]))
            used.update(item.get("source_hashes", []))
        for sha in used:
            try:
                (objects_dir / f"{sha}.bin").write_bytes(self.get_blob(sha))
            except NotFoundError:
                pass
        return dest

    def build_package_zip(self, pickup_id: str) -> bytes:
        manifest = self.pickup_manifest(pickup_id)
        import io
        buf = io.BytesIO()
        used_objects = set()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for item in manifest["items"]:
                if item["status"] not in {"content", "alternative", "composite"}:
                    continue
                key = item["key"].replace("/", "_")
                zf.writestr(f"tiles/{key}.bin", self.pickup_item_bytes(pickup_id, item["tile"]))
                for sha in item.get("source_hashes", []):
                    used_objects.add(sha)
            for sha in sorted(used_objects):
                try:
                    zf.writestr(f"objects/{sha}.bin", self.get_blob(sha))
                except NotFoundError:
                    # Audit should prevent this for normal pickups; manifest
                    # still records the missing source explicitly.
                    pass
            readme = (
                "离线图包\n"
                "manifest.json 中每个 tile 的 status 为 content/alternative/composite/empty/pending/unavailable。\n"
                "alternative 表示字节可用但来源是低优先级或其他比例层；empty 是显式空白；\n"
                "pending/unavailable 表示该范围尚不能完整离线。\n"
            )
            zf.writestr("README.txt", readme)
        return buf.getvalue()

    # ------------------------------------------------------------- downloads
    def begin_download(self, pickup_id: str | None = None, sha256: str | None = None, *, ttl_seconds: float | None = 300, now: float | None = None) -> dict:
        with self._lock:
            did = f"dl-{uuid.uuid4().hex[:12]}"
            refs = []
            if pickup_id:
                pickup = self._pickup_locked(pickup_id)
                refs.extend({"type": "pickup", "id": pickup_id, "sha256": sha} for sha in pickup.get("holds", []))
            if sha256:
                refs.append({"type": "blob", "sha256": sha256})
            current = self._now(now)
            dl = {
                "id": did,
                "pickup_id": pickup_id,
                "sha256": sha256,
                "refs": refs,
                "started_at": current,
                "expires_at": None if ttl_seconds is None else current + float(ttl_seconds),
                "active": True,
            }
            self.state["downloads"][did] = dl
            self._save_locked()
            return copy.deepcopy(dl)

    def heartbeat_download(self, download_id: str, ttl_seconds: float = 300, *, now: float | None = None) -> dict:
        with self._lock:
            dl = self.state["downloads"].get(download_id)
            if not dl:
                raise NotFoundError(f"download {download_id} not found")
            current = self._now(now)
            dl["expires_at"] = current + float(ttl_seconds)
            dl["active"] = True
            self._save_locked()
            return copy.deepcopy(dl)

    def finish_download(self, download_id: str) -> None:
        with self._lock:
            if download_id in self.state["downloads"]:
                self.state["downloads"][download_id]["active"] = False
                del self.state["downloads"][download_id]
                self._save_locked()

    def _active_downloads_locked(self, now: float | None = None):
        current = self._now(now)
        for dl in list(self.state["downloads"].values()):
            if not dl.get("active", True):
                continue
            expires = dl.get("expires_at")
            if expires is not None and float(expires) < current:
                continue
            yield dl

    # ---------------------------------------------------------------- usage/gc
    def scale_references(self, level: int, version: int | str | None = "head") -> dict[str, list[list[int]]]:
        """Return blob hashes selected while browsing a supported level.

        Small claim/level combinations are resolved exactly.  Enumerating every
        deep descendant of a large claim would be quadratic/exponential work,
        so large ranges use a conservative manifest-level over-approximation:
        it can retain an object slightly longer, but never releases one that a
        supported scale could still project.
        """
        refs: dict[str, set[tuple]] = {}
        level = int(level)
        with self._lock:
            vdoc = self.get_version(version)
            estimated = 0
            for claim in vdoc["claims"]:
                width = max(1, int(claim["bounds"][2]) - int(claim["bounds"][0]))
                height = max(1, int(claim["bounds"][3]) - int(claim["bounds"][1]))
                zoom_delta = max(0, level - int(claim["z"]))
                estimated += width * height * (4 ** zoom_delta)

            if estimated <= 4096:
                targets: set[Tile] = set()
                for claim in vdoc["claims"]:
                    geom = (claim["z"], *claim["bounds"])
                    targets.update(tiles_inside_geometry(geom, level))
                for tile in sorted(targets):
                    res = self.resolve(tile, version=vdoc["id"])
                    for source in res.get("sources", []):
                        sha = source.get("sha256")
                        if sha:
                            refs.setdefault(sha, set()).add(tile)
            else:
                refs = self._conservative_scale_references_locked(level, vdoc)
        return {sha: [list(t) for t in sorted(tiles)] for sha, tiles in refs.items()}

    def _conservative_scale_references_locked(self, level: int, vdoc: dict) -> dict[str, set[tuple]]:
        refs: dict[str, set[tuple]] = {}
        active_layers = {c["layer"] for c in vdoc["claims"]}
        for layer in active_layers:
            rev_no = vdoc["heads"].get(layer)
            if not rev_no:
                continue
            rev = self.state["revisions"].get(self._revision_id(layer, int(rev_no)))
            if not rev or rev.get("reclaimed"):
                continue
            for rec in rev.get("tiles", {}).values():
                sha = rec.get("sha256")
                if rec.get("kind") != "content" or not sha:
                    continue
                z, x, y = rec["z"], rec["x"], rec["y"]
                if z <= level:
                    shift = level - z
                    representative = (level, x << shift, y << shift)
                else:
                    representative = ancestor_at((z, x, y), level)
                refs.setdefault(sha, set()).add(representative)
        return refs

    def reference_report(self, *, now: float | None = None, include_scale: bool = True) -> dict:
        with self._lock:
            blob_refs: dict[str, list[dict]] = {sha: [] for sha in self.state["blobs"]}

            def add(sha: str | None, ref: dict):
                if not sha:
                    return
                blob_refs.setdefault(sha, []).append(copy.deepcopy(ref))

            for rid, rev in self.state["revisions"].items():
                if rev.get("reclaimed"):
                    continue
                current = self.state["heads"].get(rev["layer"]) == rev["revision"]
                for rec in rev.get("tiles", {}).values():
                    add(rec.get("sha256"), {
                        "type": "layer_revision",
                        "id": rid,
                        "layer": rev["layer"],
                        "actor": rev.get("actor"),
                        "revision": rev["revision"],
                        "current_head": current,
                        "tile": [rec["z"], rec["x"], rec["y"]],
                    })

            for pid, pickup in self.state["pickups"].items():
                for sha in pickup.get("holds", []):
                    add(sha, {"type": "pickup", "id": pid, "version": pickup["version"], "relation": "pinned_hold"})
                for item in pickup.get("items", []):
                    for sha in item.get("source_hashes", []):
                        add(sha, {
                            "type": "pickup",
                            "id": pid,
                            "version": pickup["version"],
                            "relation": "item_source",
                            "tile": item["tile"],
                            "status": item["status"],
                        })

            for dl in self._active_downloads_locked(now):
                for ref in dl.get("refs", []):
                    if ref.get("sha256"):
                        add(ref["sha256"], {**ref, "type": "active_download", "id": dl["id"]})

            scale_summary = {}
            if include_scale:
                for level in self.state.get("supported_levels", []):
                    try:
                        used = self.scale_references(level)
                    except Exception:
                        # A malformed/unsupported level must not prevent the
                        # rest of the safety audit.  The level is reported.
                        scale_summary[str(level)] = {"error": "resolution_failed"}
                        continue
                    scale_summary[str(level)] = {sha: len(tiles) for sha, tiles in used.items()}
                    for sha, tiles in used.items():
                        add(sha, {
                            "type": "supported_scale_level",
                            "level": level,
                            "tile_count": len(tiles),
                            "example_tile": tiles[0] if tiles else None,
                        })

            blobs = {}
            retained_layer_holds = []
            for sha, meta in self.state["blobs"].items():
                refs = blob_refs.get(sha, [])
                # De-duplicate identical refs.
                unique = []
                seen = set()
                for ref in refs:
                    key = json.dumps(ref, sort_keys=True, ensure_ascii=False)
                    if key not in seen:
                        seen.add(key)
                        unique.append(ref)
                blockers = [r for r in unique if r["type"] in {"pickup", "active_download", "supported_scale_level"}]
                blobs[sha] = {
                    **copy.deepcopy(meta),
                    "refs": unique,
                    "reference_count": len(unique),
                    "reclaimable": not unique,
                    "retention_blockers": blockers,
                }
                for ref in unique:
                    if ref["type"] == "pickup" and any(
                        r["type"] == "layer_revision" and r.get("current_head") is False
                        for r in unique
                    ):
                        retained_layer_holds.append({"sha256": sha, "pickup": ref["id"], "refs": unique})
                        break

            reclaimable = sum(b["bytes"] for b in blobs.values() if b["reclaimable"])
            withdrawn_holds = self._withdrawn_pickup_holds_locked()
            return AttrDict({
                "capacity_bytes": self.state["capacity_bytes"],
                "used_bytes": self.used_bytes,
                "free_bytes": self.free_bytes,
                "reclaimable_bytes": reclaimable,
                "supported_levels": self.state.get("supported_levels", []),
                "scale_summary": scale_summary,
                "blobs": blobs,
                "withdrawn_or_superseded_pickup_holds": withdrawn_holds,
                "pickups": [self.pickup_manifest(pid) for pid in self.state["pickups"]],
                "downloads": copy.deepcopy(self.state["downloads"]),
            })

    def _withdrawn_pickup_holds_locked(self):
        active_claims = {(c["layer"], c["actor"]) for c in self.state["claims"] if c.get("active", True)}
        out = []
        for pid, pickup in self.state["pickups"].items():
            for item in pickup.get("items", []):
                for sha in item.get("source_hashes", []):
                    related_revs = []
                    for rid, rev in self.state["revisions"].items():
                        if rev.get("reclaimed"):
                            continue
                        if any(rec.get("sha256") == sha for rec in rev.get("tiles", {}).values()):
                            current = self.state["heads"].get(rev["layer"]) == rev["revision"]
                            claim_active = (rev["layer"], rev.get("actor")) in active_claims
                            if (not current) or (not claim_active):
                                related_revs.append({
                                    "layer": rev["layer"],
                                    "actor": rev.get("actor"),
                                    "revision": rev["revision"],
                                    "current_head": current,
                                    "claim_active": claim_active,
                                })
                    if related_revs:
                        out.append({
                            "pickup": pid,
                            "pinned_version": pickup["version"],
                            "tile": item["tile"],
                            "item_status": item["status"],
                            "sha256": sha,
                            "retained_revisions": related_revs,
                            "reason": "pickup_pins_source_until_package_is_no_longer_referenced",
                        })
        # de-dup
        dedup = {}
        for row in out:
            key = json.dumps(row, sort_keys=True)
            dedup[key] = row
        return list(dedup.values())

    def why_retained(self, sha: str) -> dict:
        report = self.reference_report()
        if sha not in report["blobs"]:
            raise NotFoundError(f"blob {sha} not found")
        return AttrDict(report["blobs"][sha])

    def gc_plan(self, *, now: float | None = None) -> dict:
        report = self.reference_report(now=now)
        items = [
            {"sha256": sha, "bytes": blob["bytes"], "names": blob.get("names", [])}
            for sha, blob in report["blobs"].items()
            if blob["reclaimable"]
        ]
        return AttrDict({"dry_run": True, "reclaimable": items, "reclaimable_bytes": sum(i["bytes"] for i in items)})

    def gc(self, *, dry_run: bool = False, now: float | None = None) -> dict:
        with self._lock:
            plan = self.gc_plan(now=now)
            if dry_run:
                return plan
            removed = []
            for item in plan["reclaimable"]:
                sha = item["sha256"]
                if self.data_dir is not None:
                    path = self.data_dir / "blobs" / sha[:2] / f"{sha}.bin"
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    self._blob_memory.pop(sha, None)
                self.state["blobs"].pop(sha, None)
                removed.append(item)
            self._save_locked()
            return AttrDict({"dry_run": False, "removed": removed, "freed_bytes": sum(i["bytes"] for i in removed)})

    def reclaim_layer_revision(self, layer: str, revision: int, *, now: float | None = None) -> dict:
        """Reclaim one old layer revision after proving no consumer needs it."""
        with self._lock:
            rid = self._revision_id(layer, int(revision))
            rev = self.state["revisions"].get(rid)
            if not rev:
                raise NotFoundError(f"revision {rid} not found")
            if rev.get("reclaimed"):
                return AttrDict({"reclaimed": True, "id": rid, "already": True})

            report = self.reference_report(now=now)
            blockers = []
            if self.state["heads"].get(layer) == int(revision):
                blockers.append({"type": "current_logical_head", "layer": layer, "revision": revision})
            for rec in rev.get("tiles", {}).values():
                sha = rec.get("sha256")
                if not sha:
                    continue
                for ref in report["blobs"].get(sha, {}).get("refs", []):
                    if ref["type"] in {"pickup", "active_download", "supported_scale_level"}:
                        blockers.append({"sha256": sha, "reference": ref})
            if blockers:
                raise ReferencedObjectError(
                    f"cannot reclaim {rid}: active packages, downloads, or supported levels reference it",
                    revision=rid,
                    blockers=blockers,
                    usage=report["withdrawn_or_superseded_pickup_holds"],
                )

            # No retention consumer.  Tombstone the manifest, then let global GC
            # remove blobs not shared with another retained revision.
            rev["reclaimed"] = True
            rev["reclaimed_at"] = self._now(now)
            rev["reclaimed_tile_count"] = len(rev.get("tiles", {}))
            rev["tiles"] = {}
            self._append_version_locked("revision_reclaimed", layer=layer, revision=int(revision), now=now)
            gc_result = self.gc(now=now)
            return AttrDict({"reclaimed": True, "id": rid, "gc": gc_result})

    # Common aliases whose targets are defined later in the class.
    delete_layer_revision = reclaim_layer_revision
    remove_layer_revision = reclaim_layer_revision
    create_offline_package = create_pickup
    create_package = create_pickup
    claim_tiles = create_pickup
    start_pickup = create_pickup
    claim_package = create_pickup
    export_offline_package = export_package_directory
    offline_package = pickup_manifest
    get_package = pickup_manifest
    resume_offline_package = resume_pickup
    resume_package = resume_pickup
    continue_pickup = resume_pickup
    fetch_package_tile = fetch_pickup_tile
    commit_update = submit_update
    put_update = submit_update
    apply_layer_update = submit_update
    query_map = tile_response
    get_map_tile = tile_response
    usage_report = reference_report
    retention_report = reference_report
    retention = reference_report
    audit = reference_report
    why_retained_object = why_retained
    reclaim = gc
    why_source = resolve
    explain_tile = resolve
    why_not_fallback = resolve


def _epoch_seconds() -> float:
    import time
    return time.time()
