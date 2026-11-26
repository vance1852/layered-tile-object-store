"""Behavior tests for claim composition, versions, packages, and retention."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from tile_service import TileService
from tile_service.errors import CapacityError, ReferencedObjectError, TileUnavailableError
from tile_service.replay import load_fixture_service, replay_record
from tile_service.server import TileHTTPServer

ROOT = Path(__file__).parent
PARTIAL = ROOT / "fixtures" / "partial-claim-and-pinned-package"
SCALES = ROOT / "fixtures" / "scale-coverage-with-no-local-tile"


class FixtureBehaviorTest(unittest.TestCase):
    def test_scale_projection_explicit_blank_and_partial_parent(self):
        service, record = load_fixture_service(SCALES)
        replay_record(service, record)

        child = service.tile_response((2, 0, 1))
        self.assertEqual(child.http_status, 203)
        self.assertEqual(child.data, b"WWXX" * 4)
        self.assertEqual(child.resolution["sources"][0]["layer"], "atlas")
        self.assertTrue(child.headers["X-Tile-Alternative"] == "true")

        blank = service.tile_response((2, 1, 1))
        self.assertEqual(blank.http_status, 204)
        self.assertEqual(blank.resolution["state"], "empty")
        self.assertIn("explicit_empty_within_declared_scope", blank.resolution["no_fallback_reasons"])

        parent = service.tile_response((1, 0, 0))
        self.assertEqual(parent.http_status, 200)
        # Bottom-right 2x2 parent quadrant is the high-priority explicit empty.
        self.assertEqual(parent.data, b"ABCDABCDWX  WX  ")
        self.assertEqual(parent.resolution["empty_pixels"], [10, 11, 14, 15])

    def test_partial_claims_pins_and_retention(self):
        service, record = load_fixture_service(PARTIAL)
        replay = replay_record(service, record)
        by_action = [(e["action"], e) for e in replay["results"]]

        # The outside.bin tile was physically submitted but is outside field's
        # claim and therefore can never be selected.
        outside_sha = "539b277a38895ea166a583db93c259e72fbb4e2715756afcf77065e391e3fbc1"
        pinned_outside = service.resolve((2, 1, 1), version=3)
        self.assertNotIn(outside_sha, pinned_outside["source_hashes"])
        self.assertEqual(pinned_outside["sources"][0]["layer"], "atlas")
        self.assertEqual(pinned_outside["raster"], b"yyzz" * 4)

        # Expected is not explicit empty.  Strict readers get 503 while the
        # marked atlas projection remains available as an alternative.
        expected = service.tile_response((2, 0, 1), version=3)
        self.assertEqual(expected.http_status, 203)
        self.assertFalse(expected.resolution["required_available"])
        self.assertEqual(expected.data, b"wwxx" * 4)
        strict = service.tile_response((2, 0, 1), version=3, strict=True)
        self.assertEqual(strict.http_status, 503)

        # Current head has field claim revoked, so the same coordinate is now
        # served by atlas rather than the field patch.
        after_revoke = service.tile_response((2, 0, 0))
        self.assertEqual(after_revoke.http_status, 203)
        self.assertEqual(after_revoke.resolution["sources"][0]["layer"], "atlas")
        self.assertEqual(after_revoke.data, b"aabb" * 4)

        # p9 remains pinned to logical version 3 and fetched the original road
        # after current version 4 replaced it with explicit empty.
        p9 = service.pickup_manifest("p9")
        self.assertEqual(p9["version"], 3)
        road_item = next(i for i in p9["items"] if i["key"] == "2/1/0")
        self.assertEqual(road_item["status"], "content")
        self.assertEqual(road_item["rendered_sha256"],
                         "c3aaa479db71d909d2e6f62ca5a9d217a667fa0f319ade48735cf8c61ff94bdf")
        expected_item = next(i for i in p9["items"] if i["key"] == "2/0/1")
        self.assertEqual(expected_item["status"], "alternative")
        self.assertTrue(expected_item["required_unavailable"])
        self.assertTrue(p9["interrupted"])
        self.assertFalse(p9["copy_complete"])

        # The pickup hold must not include the out-of-claim object.
        self.assertNotIn(outside_sha, p9["holds"])
        road_sha = road_item["rendered_sha256"]
        why = service.why_retained(road_sha)
        self.assertTrue(any(r.get("type") == "pickup" and r.get("id") == "p9" for r in why["refs"]))
        with self.assertRaises(ReferencedObjectError):
            service.reclaim_layer_revision("incident", 1)

        usage = service.reference_report()
        hold_rows = [r for r in usage["withdrawn_or_superseded_pickup_holds"] if r["pickup"] == "p9"]
        self.assertTrue(hold_rows)

        # Zip is itself a portable manifest with the three required distinctions.
        blob = service.build_package_zip("p9")
        with zipfile.ZipFile(BytesIO(blob)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            names = set(zf.namelist())
        self.assertIn("tiles/2_1_0.bin", names)
        self.assertIn("objects/" + road_sha + ".bin", names)
        statuses = {i["key"]: i["status"] for i in manifest["items"]}
        self.assertEqual(statuses["2/1/0"], "content")
        self.assertEqual(statuses["2/0/1"], "alternative")
        self.assertIn("1/0/0", statuses)  # still pending/interrupted

    def test_replay_event_count(self):
        service, record = load_fixture_service(PARTIAL)
        report = replay_record(service, record)
        self.assertEqual(len(report["results"]), len(record["events"]))
        self.assertTrue(all(e.get("ok", True) is not False or e.get("status") == "unavailable"
                            for e in report["results"] if e["action"] not in {"fetch"}))


class WorkflowTest(unittest.TestCase):
    def test_upload_two_scales_claim_clip_and_resume(self):
        service = TileService(supported_levels=[1, 2])
        service.register_claim("atlas", "basin", 1, [0, 0, 2, 2], priority=1)
        base = b"ABCDABCDWXYZWXYZ"
        base_sha = service.add_blob(base, name="base.bin").sha256
        service.submit_update("atlas", "basin", 0, [
            {"z": 1, "x": 0, "y": 0, "kind": "content", "sha256": base_sha},
            {"z": 1, "x": 1, "y": 0, "kind": "content", "data": b"EFGHEFGHIJKLIJKL"},
        ], revision=1)

        exact = service.tile_response((1, 0, 0))
        self.assertEqual(exact.http_status, 200)
        self.assertEqual(exact.data, base)
        child = service.tile_response((2, 0, 1))
        self.assertEqual(child.http_status, 203)
        self.assertEqual(child.data, b"WWXX" * 4)  # lower-left quadrant W,X projected

        # High-priority claim only owns child 2/1/1. Its parent tile must not
        # paint sibling children outside that authority.
        service.register_claim("patch", "east", 2, [1, 1, 2, 2], priority=9)
        patch_sha = service.add_blob(b"9" * 16, name="patch.bin").sha256
        service.submit_update("patch", "east", 0, [
            {"z": 1, "x": 0, "y": 0, "kind": "content", "sha256": patch_sha},
        ])
        sibling = service.resolve((2, 0, 1))
        self.assertEqual(sibling["sources"][0]["layer"], "atlas")
        owned = service.resolve((2, 1, 1))
        self.assertEqual(owned["sources"][0]["layer"], "patch")

        pickup = service.create_pickup("pick-1", extent=[1, 0, 0], levels=[1, 2])
        self.assertEqual(pickup["total"], 5)
        service.fetch_pickup_tile("pick-1", (2, 0, 0))
        # Simulate interruption: other items remain pending and resume works.
        report = service.resume_pickup("pick-1")
        manifest = service.pickup_manifest("pick-1")
        self.assertEqual(manifest["pending_count"], 0)
        self.assertTrue(manifest["usable_offline"])
        self.assertIn("fetched", report)

    def test_optimistic_base_revision_conflict(self):
        service = TileService()
        service.register_claim("layer", "actor", 0, [0, 0, 1, 1])
        data_sha = service.add_blob(b"x" * 16).sha256
        service.submit_update("layer", "actor", 0, [[0, 0, 0, "content", data_sha]], revision=1)
        with self.assertRaises(Exception):
            service.submit_update("layer", "actor", 0, [[0, 0, 0, "empty", None]])
        service.submit_update("layer", "actor", 1, [[0, 0, 0, "empty", None]], revision=2)
        self.assertEqual(service.tile_response((0, 0, 0)).http_status, 204)

    def test_dedup_capacity_and_reference_aware_gc(self):
        service = TileService(capacity_bytes=48, supported_levels=[])
        first = service.add_blob(b"a" * 16)
        duplicate = service.add_blob(b"a" * 16)
        self.assertEqual(first["sha256"], duplicate["sha256"])
        self.assertEqual(service.used_bytes, 16)
        service.register_claim("base", "basin", 0, [0, 0, 1, 1], priority=1)
        service.submit_update("base", "basin", 0, [[0, 0, 0, "content", first["sha256"]]])

        orphan = service.add_blob(b"b" * 16)
        self.assertEqual(service.used_bytes, 32)
        with self.assertRaises(CapacityError) as ctx:
            service.add_blob(b"c" * 32)
        self.assertEqual(ctx.exception.details["admission_basis"], "actual_used_bytes_only")
        self.assertGreater(ctx.exception.details["projected_reclaimable_bytes"], 0)

        # An active download lease blocks collection of the otherwise orphan.
        dl = service.begin_download(sha256=orphan["sha256"], ttl_seconds=None)
        plan = service.gc(dry_run=True)
        self.assertNotIn(orphan["sha256"], [x["sha256"] for x in plan["reclaimable"]])
        service.finish_download(dl["id"])
        removed = service.gc()
        self.assertEqual(removed["freed_bytes"], 16)
        service.add_blob(b"c" * 32)  # actual free space is now sufficient
        self.assertEqual(service.used_bytes, 48)

    def test_supported_scale_blocks_gc_until_claim_gone(self):
        service = TileService(capacity_bytes=128, supported_levels=[1, 2])
        service.register_claim("atlas", "basin", 1, [0, 0, 1, 1], priority=1)
        sha = service.add_blob(b"A" * 16).sha256
        service.submit_update("atlas", "basin", 0, [[1, 0, 0, "content", sha]])
        service.revoke_claim("atlas", "basin")
        # Revision still exists and is head, so direct revision reclaim reports
        # the current head blocker.  A forced metadata deletion path is not
        # provided; operators retire the head through a new revision/version.
        with self.assertRaises(ReferencedObjectError):
            service.reclaim_layer_revision("atlas", 1)

    def test_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            service = TileService(td, supported_levels=[1, 2])
            service.register_claim("atlas", "basin", 1, [0, 0, 1, 1])
            sha = service.add_blob(b"P" * 16).sha256
            service.submit_update("atlas", "basin", 0, [[1, 0, 0, "content", sha]])
            service.create_pickup("p", extent=[1, 0, 0], levels=[1, 2])
            service.fetch_pickup_tile("p", [1, 0, 0])
            service.close()

            reloaded = TileService(td, supported_levels=[1, 2])
            self.assertEqual(reloaded.state["head_version"], 2)  # claim + update
            self.assertEqual(reloaded.pickup_manifest("p")["copied_count"], 1)
            self.assertEqual(reloaded.get_blob(sha), b"P" * 16)


class HttpServiceTest(unittest.TestCase):
    def test_http_routes(self):
        with tempfile.TemporaryDirectory() as td:
            httpd = TileHTTPServer(("127.0.0.1", 0), TileService(td))
            worker = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                port = httpd.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                resp.read()

                conn.request("POST", "/blobs?name=x.bin", body=b"X" * 16,
                             headers={"Content-Type": "application/octet-stream"})
                resp = conn.getresponse()
                blob = json.loads(resp.read())
                self.assertEqual(resp.status, 201)

                payload = {
                    "layer": "l", "actor": "a", "base": 0, "revision": 1,
                    "z": 0, "bounds": [0, 0, 1, 1], "priority": 1,
                }
                conn.request("POST", "/claims", body=json.dumps(payload),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 201)
                resp.read()

                update = {"actor": "a", "base": 0, "revision": 1,
                          "tiles": [{"z": 0, "x": 0, "y": 0, "kind": "content", "sha256": blob["sha256"]}]}
                conn.request("POST", "/layers/l/updates", body=json.dumps(update),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 201)
                resp.read()

                conn.request("GET", "/tiles/0/0/0")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), b"X" * 16)

                conn.request("GET", "/explain?z=0&x=0&y=0")
                resp = conn.getresponse()
                explained = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertEqual(explained["state"], "content")

                conn.request("GET", "/usage")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                resp.read()
                conn.close()
            finally:
                httpd.shutdown()
                worker.join(timeout=3)
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
