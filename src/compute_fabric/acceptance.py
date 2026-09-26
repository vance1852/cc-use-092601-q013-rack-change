"""贯通算力单价、互联通道、算力库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"),
                          ("eng", "facilities"), ("eng2", "facilities")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部数据中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_gpu_hours": "500000"})
    service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_gpu_hours": "800000"})
    service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "gpu-h100", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "cluster-a", "product": "gpu-h100", "grade": "PEAK_VALLEY", "quantity_gpu_hours": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fabric-a-b", "shipper_id": "tenant-east", "service_date": "2026-09-25", "requested_gpu_hours": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fabric-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "fabric-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:gpu-h100": "-5"}})
    service.approve_scenario("risk", "fabric-recovery", 1)
    scenario = service.run_scenario("plan", "fabric-recovery", "2026-09-23")
    for key, cap in (
        ("power", "120"),
        ("cooling", "150"),
        ("weight", "3000"),
        ("network_port:100g", "64"),
        ("network_port:25g", "96"),
    ):
        service.upsert_facility_constraint("eng", "cluster-a", {"constraint_key": key, "capacity": cap})
    rack_steps = [
        {"name": "导轨固定与设备上架", "rollback_action": "下架设备并拆除导轨"},
        {"name": "电源与网络接线", "rollback_action": "拆除线缆"},
        {"name": "上电与连通性验证", "rollback_action": "断电"},
    ]
    rack_payload = {
        "change_id": "chg-rack-1",
        "facility_id": "cluster-a",
        "title": "H100 节点机柜上架",
        "bom_version": "bom-2026.09.1",
        "devices": [{"device_id": "gx-01", "model": "H100-X", "weight_kg": "42", "rated_power_kw": "6.5", "quantity": 4}],
        "rack_locations": [{"rack_id": "r-01", "u_start": 1, "u_size": 4}],
        "power_curve": [{"at": "01:00", "kw": "18"}, {"at": "13:00", "kw": "24"}],
        "thermal_class": "AIR_L2",
        "port_demands": [{"kind": "100g", "count": 8}],
        "window_starts_at": "2026-09-27T01:00:00Z",
        "window_ends_at": "2026-09-27T05:00:00Z",
        "rollback_summary": "断电、拆线、下架并恢复机位",
        "implementation_steps": rack_steps,
    }
    service.submit_rack_change("eng", rack_payload)
    service.approve_rack_change("eng2", "chg-rack-1", 1, "窗口、资源预留与回退方案齐备，跨专业整体批准")
    rack_done = service.rack_change("chg-rack-1")
    for sequence in (1, 2, 3):
        rack_done = service.confirm_step("eng", "chg-rack-1", sequence, f"步骤{sequence}现场回执完成")
    failed_payload = dict(
        rack_payload,
        change_id="chg-rack-2",
        title="备节点上架",
        bom_version="bom-2026.09.2",
        rack_locations=[{"rack_id": "r-02", "u_start": 1, "u_size": 2}],
        power_curve=[{"at": "01:00", "kw": "6"}],
        port_demands=[{"kind": "25g", "count": 4}],
    )
    service.submit_rack_change("eng2", failed_payload)
    service.approve_rack_change("eng", "chg-rack-2", 1, "余量充足，批准")
    rack_failed = service.fail_step("eng2", "chg-rack-2", 1, "导轨孔位与到货设备不符", "rollback")
    pending_payload = dict(
        rack_payload,
        change_id="chg-rack-3",
        title="下一批扩容评估",
        bom_version="bom-2026.09.3",
        rack_locations=[{"rack_id": "r-03", "u_start": 1, "u_size": 2}],
    )
    rack_pending = service.submit_rack_change("eng", pending_payload)
    power_row = next(
        item for item in service.facility_constraints("cluster-a")["constraints"]
        if item["constraint_key"] == "power"
    )
    rack_summary = {
        "completed_state": rack_done["state"],
        "failed_state": rack_failed["state"],
        "power_remaining": power_row["remaining"],
        "pending_feasible": rack_pending["impact"]["feasible"],
        "competing_requests": [item["change_id"] for item in rack_pending["impact"]["competing_requests"]],
    }
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "rack_change": rack_summary, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行数据中心调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
