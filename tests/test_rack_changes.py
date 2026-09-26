from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from compute_fabric.api import JsonApplication
from compute_fabric.clock import FrozenClock
from compute_fabric.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from compute_fabric.rack_change import THERMAL_COOLING_FACTOR, RackChangeDraft, required_demands
from compute_fabric.service import SupplyService


def draft(change_id: str = "chg-1", **overrides) -> dict:
    payload = {
        "change_id": change_id,
        "facility_id": "dc1",
        "title": "GPU 机柜上架",
        "bom_version": "bom-2026.09",
        "devices": [
            {"device_id": "srv-01", "model": "G8-X", "weight_kg": "35.5", "rated_power_kw": "3.2", "quantity": 2}
        ],
        "rack_locations": [{"rack_id": "r-a01", "u_start": 1, "u_size": 2}],
        "power_curve": [{"at": "01:00", "kw": "5"}, {"at": "13:00", "kw": "8"}],
        "thermal_class": "AIR_L2",
        "port_demands": [{"kind": "100g", "count": 4}, {"kind": "10g", "count": 2}],
        "window_starts_at": "2026-10-01T01:00:00Z",
        "window_ends_at": "2026-10-01T05:00:00Z",
        "rollback_summary": "断电并拆除导轨，恢复机柜原始状态",
        "implementation_steps": [
            {"name": "导轨固定与设备上架", "rollback_action": "下架设备并拆除导轨"},
            {"name": "电源与网络接线", "rollback_action": "拆除线缆"},
            {"name": "上电与连通性验证", "rollback_action": "断电"},
        ],
    }
    payload.update(overrides)
    return payload


class DemandCalculationTests(unittest.TestCase):
    def test_demands_use_peak_power_cooling_factor_weight_and_ports(self) -> None:
        parsed = RackChangeDraft.from_dict(draft())
        demands = required_demands(parsed)
        self.assertEqual(demands["power"], 8)  # 功耗曲线峰值，而非额定或均值
        self.assertEqual(demands["cooling"], 8 * THERMAL_COOLING_FACTOR["AIR_L2"])
        self.assertEqual(demands["weight"], 71)
        self.assertEqual(demands["network_port:100g"], 4)
        self.assertEqual(demands["network_port:10g"], 2)

    def test_overlapping_u_positions_within_one_change_are_rejected(self) -> None:
        bad = draft(rack_locations=[{"rack_id": "r-a01", "u_start": 1, "u_size": 2},
                                    {"rack_id": "r-a01", "u_start": 2, "u_size": 1}])
        with self.assertRaises(ValidationFailed):
            RackChangeDraft.from_dict(bad)


class RackChangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("eng", "facilities"),
            ("eng2", "facilities"),
            ("risk", "risk"),
            ("plan", "planner"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility(
            "plan",
            {"facility_id": "dc1", "name": "北部机房", "kind": "edge-site", "timezone": "Asia/Shanghai",
             "capacity_gpu_hours": "1"},
        )
        for key, cap in (("power", "100"), ("cooling", "120"), ("weight", "2000"),
                         ("network_port:100g", "48"), ("network_port:10g", "96")):
            self.service.upsert_facility_constraint("eng", "dc1", {"constraint_key": key, "capacity": cap})

    def tearDown(self) -> None:
        self.connection.close()

    def _constraint(self, key: str) -> dict:
        return next(
            item for item in self.service.facility_constraints("dc1")["constraints"]
            if item["constraint_key"] == key
        )

    def test_impact_shows_remaining_for_every_constraint(self) -> None:
        result = self.service.submit_rack_change("eng", draft())
        self.assertEqual(result["state"], "submitted")
        self.assertTrue(result["impact"]["feasible"])
        rows = {item["constraint_key"]: item for item in result["impact"]["constraints"]}
        self.assertEqual(rows["power"]["available"], "100.000")
        self.assertEqual(rows["power"]["required"], "8.000")
        self.assertEqual(rows["power"]["remaining_after_change"], "92.000")
        self.assertEqual(rows["cooling"]["required"], "9.200")
        self.assertEqual(rows["network_port:100g"]["remaining_after_change"], "44.000")

    def test_undefined_constraint_and_overcommit_are_hard_conflicts(self) -> None:
        bad = draft(change_id="chg-x", port_demands=[{"kind": "400g", "count": 2}])
        result = self.service.submit_rack_change("eng", bad)
        self.assertFalse(result["impact"]["feasible"])
        self.assertIn("constraint_undefined", {item["type"] for item in result["impact"]["conflicts"]})
        overcommit = draft(change_id="chg-big", power_curve=[{"at": "01:00", "kw": "150"}])
        result = self.service.submit_rack_change("eng", overcommit)
        conflict = next(
            item for item in result["impact"]["conflicts"]
            if item["type"] == "capacity_overcommit" and item["constraint_key"] == "power"
        )
        self.assertEqual(conflict["shortfall"], "50.000")
        with self.assertRaises(Conflict):
            self.service.approve_rack_change("risk", "chg-big", 1, "强行批准")

    def test_competing_requests_are_listed_but_only_locked_versions_block(self) -> None:
        first = self.service.submit_rack_change("eng", draft("chg-1"))
        second = self.service.submit_rack_change(
            "eng2",
            draft("chg-2", rack_locations=[{"rack_id": "r-a01", "u_start": 2, "u_size": 1}]),
        )
        competing = {item["change_id"]: item for item in second["impact"]["competing_requests"]}
        self.assertIn("chg-1", competing)
        self.assertEqual(
            set(competing["chg-1"]["reasons"]),
            {"capacity_share", "window_overlap", "location_overlap"},
        )
        # 双方都在待批状态，互不构成硬性冲突。
        self.assertTrue(second["impact"]["feasible"])
        power_row = next(item for item in second["impact"]["constraints"] if item["constraint_key"] == "power")
        self.assertEqual(power_row["pending_demand_by_others"], "8.000")
        self.assertEqual(power_row["remaining_if_all_pending_approved"], "84.000")
        self.service.approve_rack_change("eng2", "chg-1", 1, "窗口可行，余量充足")
        refreshed = self.service.rack_change("chg-2")
        self.assertFalse(refreshed["impact"]["feasible"])
        overlap = next(item for item in refreshed["impact"]["conflicts"] if item["type"] == "location_overlap")
        self.assertEqual(overlap["other_change_id"], "chg-1")
        self.assertEqual(overlap["other_state"], "approved")

    def test_applicant_cannot_approve_own_change_and_basis_is_required(self) -> None:
        self.service.submit_rack_change("eng", draft())
        with self.assertRaises(Forbidden):
            self.service.approve_rack_change("eng", "chg-1", 1, "我自己批")
        with self.assertRaises(ValidationFailed):
            self.service.approve_rack_change("eng2", "chg-1", 1, "  ")
        decision = self.service.approve_rack_change("eng2", "chg-1", 1, "跨专业复核通过")
        self.assertEqual(decision["decision"]["by"], "eng2")
        self.assertEqual(decision["decision"]["basis"], "跨专业复核通过")
        self.assertEqual(self._constraint("power")["reserved"], "8.000")

    def test_revision_chain_supersedes_and_idempotent_content(self) -> None:
        first = self.service.submit_rack_change("eng", draft())
        # 内容未变时重放返回同一版本。
        replay = self.service.submit_rack_change("eng", draft())
        self.assertEqual(replay["revision"], 1)
        revised = self.service.submit_rack_change("eng", draft(bom_version="bom-2026.10",
                                                               power_curve=[{"at": "01:00", "kw": "6"}]))
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["revisions"][0]["state"], "superseded")
        self.assertEqual(revised["revisions"][0]["supersedes_revision"], None)
        self.assertEqual(revised["revisions"][1]["supersedes_revision"], 1)
        self.assertEqual(self.service.rack_change("chg-1", 1)["state"], "superseded")
        # 已锁定的版本不能再修订。
        self.service.approve_rack_change("eng2", "chg-1", 2, "ok")
        with self.assertRaises(InvalidState):
            self.service.submit_rack_change("eng", draft(bom_version="bom-2026.11"))

    def test_step_receipts_are_ordered_and_completion_consumes_reservations(self) -> None:
        self.service.submit_rack_change("eng", draft())
        self.service.approve_rack_change("risk", "chg-1", 1, "批准施工")
        with self.assertRaises(InvalidState):
            self.service.confirm_step("eng", "chg-1", 2, "跳步")
        self.service.confirm_step("eng", "chg-1", 1, "导轨固定完成")
        self.assertEqual(self.service.rack_change("chg-1")["state"], "in_progress")
        # 施工中途余量仍被锁定，未计入已用。
        self.assertEqual(self._constraint("power")["used"], "0")
        self.service.confirm_step("eng", "chg-1", 2, "接线完成")
        finished = self.service.confirm_step("eng", "chg-1", 3, "连通性验证通过")
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(self._constraint("power")["used"], "8.000")
        self.assertEqual(self._constraint("power")["reserved"], "0.000")
        self.assertTrue(all(item["state"] == "consumed" for item in finished["reservations"]))

    def test_failed_step_rolls_back_in_reverse_and_never_counts_partial_as_success(self) -> None:
        self.service.submit_rack_change("eng", draft())
        self.service.approve_rack_change("risk", "chg-1", 1, "批准施工")
        self.service.confirm_step("eng", "chg-1", 1, "ok")
        self.service.confirm_step("eng", "chg-1", 2, "ok")
        failed = self.service.fail_step("eng", "chg-1", 3, "上联端口不亮", "rollback")
        self.assertEqual(failed["state"], "rolling_back")
        self.assertEqual(failed["fail_reason"], "上联端口不亮")
        states = {item["sequence"]: item["state"] for item in failed["steps"]}
        self.assertEqual(states[3], "failed")
        # 部分完成绝不等于成功。
        self.assertNotEqual(failed["state"], "completed")
        # 回退必须逆序：先回退步骤 1 会被拒绝。
        with self.assertRaises(InvalidState):
            self.service.record_rollback_step("eng", "chg-1", 1, "逆行")
        self.service.record_rollback_step("eng", "chg-1", 2, "拆线完成")
        rolling = self.service.rack_change("chg-1")
        self.assertEqual(rolling["state"], "rolling_back")
        self.assertEqual(self._constraint("power")["reserved"], "8.000")
        rolled_back = self.service.record_rollback_step("eng", "chg-1", 1, "下架完成")
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertEqual(self._constraint("power")["reserved"], "0.000")
        self.assertEqual(self._constraint("power")["used"], "0")
        self.assertTrue(all(item["state"] == "released" for item in rolled_back["reservations"]))

    def test_failure_before_any_completion_releases_immediately(self) -> None:
        self.service.submit_rack_change("eng", draft())
        self.service.approve_rack_change("risk", "chg-1", 1, "批准")
        failed = self.service.fail_step("eng", "chg-1", 1, "现场发现导轨型号错误", "rollback")
        self.assertEqual(failed["state"], "rolled_back")
        self.assertEqual(self._constraint("power")["reserved"], "0.000")

    def test_manual_takeover_requires_independent_decision(self) -> None:
        self.service.submit_rack_change("eng", draft())
        self.service.approve_rack_change("eng2", "chg-1", 1, "批准")
        self.service.confirm_step("eng", "chg-1", 1, "ok")
        self.service.fail_step("eng", "chg-1", 2, "线缆标签与规划不符", "manual")
        # 申请人不能对自己的变更做接管结论。
        with self.assertRaises(Forbidden):
            self.service.resolve_manual_takeover("eng", "chg-1", "release", "自己判断")
        resolved = self.service.resolve_manual_takeover("risk", "chg-1", "release", "现场核验后决定恢复")
        self.assertEqual(resolved["state"], "rolled_back")
        self.assertEqual(self._constraint("power")["reserved"], "0.000")

    def test_audit_chain_covers_change_lifecycle(self) -> None:
        self.service.submit_rack_change("eng", draft())
        self.service.approve_rack_change("risk", "chg-1", 1, "批准")
        self.service.confirm_step("eng", "chg-1", 1, "ok")
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreaterEqual(chain["events"], 3)


class RackChangeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection)
        for user_id, role in (("eng", "facilities"), ("risk", "risk"), ("plan", "planner")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility(
            "plan",
            {"facility_id": "dc1", "name": "DC1", "kind": "edge-site", "timezone": "Asia/Shanghai",
             "capacity_gpu_hours": "1"},
        )
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_full_lifecycle_over_http(self) -> None:
        def call(method: str, path: str, actor: str, payload: dict | None = None):
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            return self.app.handle(method, path, {"X-Actor-Id": actor}, body)

        for key, cap in (("power", "100"), ("cooling", "120"), ("weight", "2000"),
                         ("network_port:100g", "48"), ("network_port:10g", "96")):
            response = call("PUT", "/facilities/dc1/constraints", "eng", {"constraint_key": key, "capacity": cap})
            self.assertEqual(response.status, 200)
        response = call("POST", "/rack-changes", "eng", draft())
        self.assertEqual(response.status, 201)
        self.assertTrue(response.body["impact"]["feasible"])
        forbidden = call("POST", "/rack-changes/chg-1/approve", "eng",
                         {"expected_revision": 1, "basis": "自批"})
        self.assertEqual(forbidden.status, 403)
        approved = call("POST", "/rack-changes/chg-1/approve", "risk",
                        {"expected_revision": 1, "basis": "整体审批：窗口、预留、回退齐备"})
        self.assertEqual(approved.status, 200)
        receipt = call("POST", "/rack-changes/chg-1/steps/1", "eng", {"note": "完成"})
        self.assertEqual(receipt.body["state"], "in_progress")
        listing = call("GET", "/rack-changes?facility_id=dc1", "eng")
        self.assertEqual(listing.body["changes"][0]["change_id"], "chg-1")
        remaining = call("GET", "/facilities/dc1/constraints", "eng")
        power = next(item for item in remaining.body["constraints"] if item["constraint_key"] == "power")
        self.assertEqual(power["reserved"], "8.000")


if __name__ == "__main__":
    unittest.main()
