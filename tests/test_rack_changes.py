from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from compute_fabric.api import JsonApplication
from compute_fabric.clock import FrozenClock
from compute_fabric.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from compute_fabric.rackchange import (
    DeviceAsset,
    FacilitySnapshot,
    annotate_conflicts,
    build_impact,
    change_totals,
)
from compute_fabric.service import SupplyService


def snapshot_payload(**overrides) -> dict:
    payload = {
        "site_id": "dc-north",
        "name": "北部数据中心",
        "power_limit_kw": "100",
        "cooling_limit_kw": "120",
        "weight_limit_kg": "5000",
        "baseline_power_kw": "40",
        "baseline_cooling_kw": "50",
        "baseline_weight_kg": "2000",
        "racks": [
            {
                "rack_id": "r1",
                "u_size": 42,
                "weight_limit_kg": "1200",
                "power_limit_kw": "20",
                "max_heat_class": "ENHANCED_AIR",
                "baseline_u": [
                    {
                        "u_start": 1,
                        "u_size": 2,
                        "label": "old-switch",
                        "weight_kg": "30",
                        "peak_power_kw": "0.5",
                        "heat_class": "AIR",
                    }
                ],
            }
        ],
        "port_pools": [
            {"port_type": "100g", "total": 48, "baseline_used": 10},
            {"port_type": "25g", "total": 96, "baseline_used": 40},
        ],
    }
    payload.update(overrides)
    return payload


POWER_CURVE = ["2.0"] * 8 + ["4.0"] * 8 + ["3.0"] * 8


def device_payload(asset_id: str, *, u_start: int = 3, peak: str = "4", **overrides) -> dict:
    payload = {
        "asset_id": asset_id,
        "model": "H100-node",
        "rack_id": "r1",
        "u_start": u_start,
        "u_size": 4,
        "power_curve": [str(Decimal(peak) * Decimal(value) / Decimal("4")) for value in POWER_CURVE],
        "cooling_load_kw": "4.5",
        "weight_kg": "80",
        "heat_class": "ENHANCED_AIR",
        "ports": {"100g": 2, "25g": 1},
    }
    payload.update(overrides)
    return payload


def change_payload(change_id: str = "chg-1", **overrides) -> dict:
    payload = {
        "change_id": change_id,
        "title": "GPU 机柜上架",
        "site_id": "dc-north",
        "device_list_version": "bom-v1",
        "devices": [device_payload("gpu-1")],
        "window_starts_at": "2026-10-01T01:00:00Z",
        "window_ends_at": "2026-10-01T05:00:00Z",
        "execution_steps": [
            {"code": "rack_prep", "title": "机柜准备"},
            {"code": "mount", "title": "设备上架"},
            {"code": "cable", "title": "接线与验证"},
        ],
        "rollback_plan": {
            "trigger_notes": "任一步骤失败立即回退",
            "steps": [{"code": "unmount", "title": "下架设备", "instruction": "拆除并恢复 U 位"}],
        },
    }
    payload.update(overrides)
    return payload


class RackChangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("eng", "dispatcher"),
            ("eng2", "dispatcher"),
            ("plan", "planner"),
            ("risk", "risk"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_facility_snapshot("plan", snapshot_payload())

    def tearDown(self) -> None:
        self.connection.close()

    def test_snapshot_versions_and_open_change_blocks_rebuild(self) -> None:
        first = self.service.facility_snapshot("dc-north")
        self.assertEqual(first["revision"], 1)
        self.service.submit_rack_change("eng", change_payload())
        with self.assertRaises(InvalidState):
            self.service.register_facility_snapshot("plan", snapshot_payload())
        self.service.decide_rack_change("risk", "chg-1", "rejected", "驳回")
        second = self.service.register_facility_snapshot("plan", snapshot_payload(name="北部数据中心二期"))
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.service.facility_snapshot("dc-north", 1)["state"], "superseded")

    def test_impact_shows_each_constraint_remaining_and_basis(self) -> None:
        result = self.service.submit_rack_change("eng", change_payload())
        impact = result["impact"]
        self.assertTrue(impact["feasible"])
        self.assertEqual(impact["site_constraints"]["power_kw"]["remaining"], "60.000")
        self.assertEqual(impact["site_constraints"]["power_kw"]["requested"], "4.000")
        self.assertEqual(impact["site_constraints"]["cooling_kw"]["remaining"], "70.000")
        self.assertEqual(impact["port_constraints"]["100g"]["remaining"], 38)
        self.assertEqual(impact["port_constraints"]["25g"]["remaining"], 56)
        rack = impact["rack_constraints"]["r1"]
        self.assertEqual(rack["rack_power_kw"]["remaining"], "19.500")
        self.assertEqual(rack["rack_weight_kg"]["remaining"], "1170.000")
        self.assertEqual(rack["occupied_u"][0]["asset_id"], "old-switch")
        self.assertIn("余量=总量-基线已用", impact["basis"])

    def test_overlapping_baseline_u_rejected_at_submit(self) -> None:
        payload = change_payload()
        payload["devices"][0].update(u_start=2, u_size=4)
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)

    def test_unknown_rack_and_heat_class_rejected(self) -> None:
        payload = change_payload()
        payload["devices"][0]["rack_id"] = "nope"
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)
        payload = change_payload()
        payload["devices"][0]["heat_class"] = "LIQUID"
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)

    def test_power_curve_must_cover_24_hours(self) -> None:
        payload = change_payload()
        payload["devices"][0]["power_curve"] = ["1"] * 23
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)

    def test_role_separation_for_write_and_approval(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_facility_snapshot("eng", snapshot_payload(site_id="other"))
        self.service.submit_rack_change("eng", change_payload())
        with self.assertRaises(Forbidden):
            self.service.decide_rack_change("eng", "chg-1", "approved", "self")
        with self.assertRaises(Forbidden):
            self.service.submit_rack_change("risk", change_payload("chg-x"))

    def test_approval_requires_window_reservation_and_rollback_together(self) -> None:
        # 缺少回退步骤
        payload = change_payload()
        payload["rollback_plan"]["steps"] = []
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)
        # 只有一个施工步骤
        payload = change_payload()
        payload["execution_steps"] = [{"code": "only", "title": "only"}]
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)
        # 窗口倒置
        payload = change_payload()
        payload["window_ends_at"] = "2026-10-01T00:00:00Z"
        with self.assertRaises(ValidationFailed):
            self.service.submit_rack_change("eng", payload)

    def test_approved_change_locks_reservations_and_blocks_competitor(self) -> None:
        tight = snapshot_payload(power_limit_kw="50", baseline_power_kw="45")
        self.service.register_facility_snapshot("plan", tight)
        # r1 的机柜受电也收紧
        self.service.submit_rack_change("eng", change_payload("chg-a"))
        competitor = change_payload(
            "chg-b",
            devices=[device_payload("gpu-2", u_start=10)],
            window_starts_at="2026-10-01T03:00:00Z",
            window_ends_at="2026-10-01T07:00:00Z",
        )
        self.service.submit_rack_change("eng2", competitor)
        before = self.service.rack_change("chg-b")["impact"]
        self.assertEqual(
            [(c["change_id"], c["hard_reservation"]) for c in before["conflicting_changes"]],
            [("chg-a", False)],
        )
        self.service.decide_rack_change("risk", "chg-a", "approved", "窗口预留回退齐备")
        locked = self.service.rack_change("chg-a")
        self.assertEqual(locked["state"], "reserved")
        dimensions = {(row["dimension"], row["scope_key"]) for row in locked["locks"]}
        self.assertIn(("power_kw", "dc-north"), dimensions)
        self.assertIn(("port_count", "100g"), dimensions)
        self.assertIn(("rack_power_kw", "r1"), dimensions)
        after = self.service.rack_change("chg-b")["impact"]
        self.assertFalse(after["feasible"])
        peer = next(c for c in after["conflicting_changes"] if c["change_id"] == "chg-a")
        self.assertTrue(peer["hard_reservation"])
        self.assertIn("window_overlap", peer["reasons"])
        self.assertIn("power_kw", peer["contested_constraints"])
        with self.assertRaises(InvalidState):
            self.service.decide_rack_change("risk", "chg-b", "approved", "强行批准")
        capacity = self.service.site_capacity("dc-north")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["locked_by_approved"], "4.000")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["remaining"], "1.000")

    def test_completion_consumes_capacity_and_requires_every_step(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok")
        with self.assertRaises(InvalidState):
            self.service.record_execution_step("eng", "chg-1", 1, True)
        self.service.start_rack_execution("eng", "chg-1")
        self.service.record_execution_step("eng", "chg-1", 0, True)
        partial = self.service.rack_change("chg-1")
        self.assertEqual(partial["state"], "in_progress")
        self.assertEqual(partial["receipts"]["execution"][0]["state"], "done")
        with self.assertRaises(InvalidState):
            self.service.record_execution_step("eng", "chg-1", 0, True)
        self.service.record_execution_step("eng", "chg-1", 1, True)
        completed = self.service.record_execution_step("eng", "chg-1", 2, True)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["locks"], [])
        capacity = self.service.site_capacity("dc-north")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["consumed_by_completed"], "4.000")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["remaining"], "56.000")

    def test_failed_step_never_partial_success_and_requires_explicit_disposition(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok")
        self.service.start_rack_execution("eng", "chg-1")
        self.service.record_execution_step("eng", "chg-1", 0, True)
        failed = self.service.record_execution_step("eng", "chg-1", 1, False, "滑轨卡死")
        self.assertEqual(failed["state"], "failed")
        self.assertNotEqual(failed["state"], "completed")
        with self.assertRaises(InvalidState):
            self.service.record_execution_step("eng", "chg-1", 2, True)
        with self.assertRaises(InvalidState):
            self.service.start_rack_execution("eng", "chg-1")
        # 失败期间余量锁仍在
        capacity = self.service.site_capacity("dc-north")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["locked_by_approved"], "4.000")
        with self.assertRaises(ValidationFailed):
            self.service.manual_takeover("risk", "chg-1", "  ")

    def test_rollback_runs_steps_and_releases_locks(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok")
        self.service.start_rack_execution("eng", "chg-1")
        self.service.record_execution_step("eng", "chg-1", 0, True)
        self.service.record_execution_step("eng", "chg-1", 1, False, "失败")
        self.service.begin_rollback("eng", "chg-1")
        with self.assertRaises(InvalidState):
            self.service.begin_rollback("eng", "chg-1")
        rolled = self.service.record_rollback_step("eng", "chg-1", 0, True, "已下架恢复")
        self.assertEqual(rolled["state"], "rolled_back")
        self.assertEqual(rolled["receipts"]["rollback"][0]["state"], "done")
        capacity = self.service.site_capacity("dc-north")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["locked_by_approved"], "0.000")

    def test_failed_rollback_can_go_manual_takeover_with_locks_kept(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok")
        self.service.start_rack_execution("eng", "chg-1")
        self.service.record_execution_step("eng", "chg-1", 0, False, "失败")
        self.service.begin_rollback("eng", "chg-1")
        self.service.record_rollback_step("eng", "chg-1", 0, False, "螺栓锈蚀无法下架")
        takeover = self.service.manual_takeover("risk", "chg-1", "现场班组接管并保留预留")
        self.assertEqual(takeover["state"], "manual_takeover")
        capacity = self.service.site_capacity("dc-north")
        self.assertEqual(capacity["site_constraints"]["power_kw"]["locked_by_approved"], "4.000")
        with self.assertRaises(Forbidden):
            self.service.manual_takeover("eng", "chg-1", "无权接管")

    def test_revision_chain_preserves_history(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "returned", "请缩减端口需求")
        revised = change_payload(device_list_version="bom-v2")
        revised["devices"][0]["ports"] = {"100g": 1}
        self.service.revise_rack_change("eng", "chg-1", revised, "减少一个 100g 端口")
        detail = self.service.rack_change("chg-1")
        self.assertEqual(detail["current_revision"], 2)
        self.assertEqual(
            [(row["revision"], row["state"], row["supersedes_revision"]) for row in detail["revision_chain"]],
            [(1, "superseded", None), (2, "pending_approval", 1)],
        )
        decisions = detail["approvals"]
        self.assertEqual(decisions[0]["decision"], "returned")
        self.assertIn("审批人不得为申请人", decisions[0]["basis"]["rule"])
        with self.assertRaises(Forbidden):
            self.service.revise_rack_change("eng2", "chg-1", revised)
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok", expected_revision=2)
        with self.assertRaises(InvalidState):
            self.service.decide_rack_change("risk", "chg-1", "rejected", "过期修订", expected_revision=1)

    def test_returned_change_keeps_snapshot_locked_until_revised_against_new_one(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "returned", "请调整")
        # 退回但未结案的变更仍然锁定快照版本，避免审批依据漂移
        with self.assertRaises(InvalidState):
            self.service.register_facility_snapshot("plan", snapshot_payload())
        # 审批人将退回变更驳回结案后，快照可演进；申请人修订时自动引用最新快照
        self.service.decide_rack_change("risk", "chg-1", "rejected", "本期不实施")
        self.service.register_facility_snapshot(
            "plan", snapshot_payload(power_limit_kw="200", name="扩容后")
        )
        self.service.revise_rack_change(
            "eng", "chg-1", change_payload(title="GPU 机柜上架二期")
        )
        detail = self.service.rack_change("chg-1")
        self.assertEqual(detail["current_revision"], 2)
        self.assertEqual(detail["snapshot_revision"], 2)
        self.assertEqual(detail["impact"]["site_constraints"]["power_kw"]["remaining"], "160.000")

    def test_cancel_and_list(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        with self.assertRaises(Forbidden):
            self.service.cancel_rack_change("eng2", "chg-1")
        cancelled = self.service.cancel_rack_change("eng", "chg-1")
        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaises(InvalidState):
            self.service.cancel_rack_change("eng", "chg-1")
        listing = self.service.list_rack_changes("dc-north")
        self.assertEqual([row["change_id"] for row in listing["changes"]], ["chg-1"])

    def test_audit_chain_records_change_lifecycle(self) -> None:
        self.service.submit_rack_change("eng", change_payload())
        self.service.decide_rack_change("risk", "chg-1", "approved", "ok")
        self.service.start_rack_execution("eng", "chg-1")
        self.service.record_execution_step("eng", "chg-1", 0, True)
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        events = [
            row[0]
            for row in self.connection.execute(
                "SELECT event_type FROM supply_audit_events WHERE entity_type='rack_change' ORDER BY event_id"
            ).fetchall()
        ]
        self.assertEqual(
            events,
            [
                "rackchange.submitted",
                "rackchange.approved",
                "rackchange.execution_started",
                "rackchange.step_done",
            ],
        )


class PureImpactTests(unittest.TestCase):
    def test_change_totals_uses_peak_of_curve(self) -> None:
        device = DeviceAsset.from_dict(device_payload("gpu-a"))
        totals = change_totals([device])
        self.assertEqual(totals["power_kw"], "4.000")
        self.assertEqual(totals["ports"], {"100g": 2, "25g": 1})

    def test_build_impact_with_no_request_reports_remaining(self) -> None:
        snapshot = FacilitySnapshot.from_dict(snapshot_payload())

        class Peers:
            def __iter__(self):
                return iter(())

        impact = build_impact(snapshot, [], Peers())
        self.assertEqual(impact["site_constraints"]["power_kw"]["remaining"], "60.000")
        self.assertTrue(impact["feasible"])
        self.assertEqual(impact["rack_constraints"], {})


class RackChangeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection, FrozenClock(datetime(2026, 9, 26, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        self.service.create_user("eng", "eng", "dispatcher")
        self.service.create_user("plan", "plan", "planner")
        self.service.create_user("risk", "risk", "risk")

    def test_api_full_lifecycle(self) -> None:
        response = self.app.handle(
            "POST", "/facility/snapshots", {"X-Actor-Id": "plan"},
            json.dumps(snapshot_payload()).encode(),
        )
        self.assertEqual(response.status, 201)
        response = self.app.handle(
            "POST", "/rack-changes", {"X-Actor-Id": "eng"},
            json.dumps(change_payload()).encode(),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["state"], "pending_approval")
        response = self.app.handle(
            "POST",
            "/rack-changes/chg-1/decision",
            {"X-Actor-Id": "risk"},
            json.dumps({"decision": "approved", "comment": "同意"}).encode(),
        )
        self.assertEqual(response.status, 200)
        response = self.app.handle("GET", "/facility/dc-north/capacity", {"X-Actor-Id": "risk"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["site_constraints"]["power_kw"]["remaining"], "56.000")
        response = self.app.handle("POST", "/rack-changes/chg-1/start", {"X-Actor-Id": "eng"}, b"{}")
        self.assertEqual(response.status, 200)
        response = self.app.handle(
            "POST",
            "/rack-changes/chg-1/steps",
            {"X-Actor-Id": "eng"},
            json.dumps({"step_index": 0, "success": True, "note": "ok"}).encode(),
        )
        self.assertEqual(response.status, 200)
        response = self.app.handle(
            "POST",
            "/rack-changes/chg-1/steps",
            {"X-Actor-Id": "eng"},
            json.dumps({"step_index": 1, "success": False, "note": "fail"}).encode(),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "failed")
        response = self.app.handle("GET", "/rack-changes/chg-1", {"X-Actor-Id": "eng"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["receipts"]["execution"][1]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
