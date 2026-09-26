"""算力单价、算力库存、互联通道和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import IndexQuote, Facility, InventoryLot, NominationRequest, Route, SupplyScenario
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .rackchange import (
    LOCKED_STATES,
    PENDING_STATES,
    FacilitySnapshot,
    RackChangeRequest,
    annotate_conflicts,
    build_impact,
    change_totals,
    validate_devices_against_snapshot,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "quote.write", "catalog.write", "scenario.write", "scenario.run",
        "facility.snapshot.write",
    },
    "dispatcher": {
        "nomination.write", "allocation.run", "transfer.write", "inventory.write",
        "rackchange.write", "rackchange.execute",
    },
    "risk": {"outage.write", "scenario.approve", "report.read", "rackchange.approve"},
    "auditor": {"report.read", "audit.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("算力单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准算力单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_gpu_hours,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_gpu_hours),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("互联通道编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("互联通道不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_gpu_hours,available_gpu_hours,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_gpu_hours),
                        decimal_text(lot.quantity_gpu_hours),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("加速卡资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("加速卡资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("互联通道当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_gpu_hours,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_gpu_hours),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("互联通道不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_gpu_hours"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_gpu_hours"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_gpu_hours=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_gpu_hours"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可交付版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("加速卡资源批次不存在")
        allocated = Decimal(nomination["allocated_gpu_hours"])
        available = Decimal(lot["available_gpu_hours"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("加速卡资源批次与互联通道起点或资源类型不匹配")
        if available < allocated:
            raise Conflict("算力库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_gpu_hours=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_gpu_hours,"
                "expected_delivered_gpu_hours,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_gpu_hours": decimal_text(allocated),
            "expected_delivered_gpu_hours": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用算力单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_gpu_hours AS REAL)) available_gpu_hours "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    # ------------------------------------------------------------------
    # 机柜上架变更管理
    # ------------------------------------------------------------------

    CHANGE_TERMINAL_STATES = {"completed", "rolled_back", "rejected", "cancelled"}

    def register_facility_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "facility.snapshot.write")
        snapshot = FacilitySnapshot.from_dict(raw)
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT revision FROM facility_snapshots WHERE site_id=? ORDER BY revision DESC LIMIT 1",
                (snapshot.site_id,),
            ).fetchone()
            open_changes = self.connection.execute(
                "SELECT count(*) FROM rack_changes WHERE site_id=? AND state NOT IN "
                "('completed','rolled_back','rejected','cancelled')",
                (snapshot.site_id,),
            ).fetchone()[0]
            if open_changes:
                raise InvalidState("存在未结案的机柜变更，不能重建设施快照")
            revision = 1 if previous is None else int(previous["revision"]) + 1
            if previous is not None:
                self.connection.execute(
                    "UPDATE facility_snapshots SET state='superseded' WHERE site_id=? AND revision=?",
                    (snapshot.site_id, previous["revision"]),
                )
            definition = canonical_json(snapshot.as_dict())
            self.connection.execute(
                "INSERT INTO facility_snapshots(site_id,revision,name,definition_json,content_sha256,"
                "state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot.site_id, revision, snapshot.name, definition,
                    snapshot.content_sha256, "active", actor_id, self._now(),
                ),
            )
            self._audit(
                "facility_snapshot", f"{snapshot.site_id}:{revision}",
                "facility.snapshot.registered", actor_id,
                {"site_id": snapshot.site_id, "revision": revision, "sha256": snapshot.content_sha256},
            )
        return {
            "site_id": snapshot.site_id,
            "revision": revision,
            "state": "active",
            "content_sha256": snapshot.content_sha256,
        }

    def facility_snapshot(self, site_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self._snapshot_row(site_id, revision)
        return {
            "site_id": row["site_id"],
            "revision": row["revision"],
            "state": row["state"],
            "content_sha256": row["content_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "snapshot": json.loads(row["definition_json"]),
        }

    def _snapshot_row(self, site_id: str, revision: int | None = None) -> sqlite3.Row:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM facility_snapshots WHERE site_id=? ORDER BY revision DESC LIMIT 1",
                (site_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM facility_snapshots WHERE site_id=? AND revision=?",
                (site_id, revision),
            ).fetchone()
        if row is None:
            raise NotFound("设施快照不存在")
        return row

    def _load_snapshot(self, row: sqlite3.Row) -> FacilitySnapshot:
        return FacilitySnapshot.from_dict(json.loads(row["definition_json"]))

    def _peer_revision_rows(self, site_id: str, exclude_change: str | None = None) -> list[sqlite3.Row]:
        """各同站点其他变更的最新修订（用于余量与冲突计算）。"""

        sql = (
            "SELECT r.* FROM rack_change_revisions r JOIN ("
            "SELECT change_id, max(revision) AS revision FROM rack_change_revisions "
            "WHERE site_id=? GROUP BY change_id"
            ") latest ON latest.change_id=r.change_id AND latest.revision=r.revision "
            "WHERE r.site_id=?"
        )
        params: list[object] = [site_id, site_id]
        if exclude_change is not None:
            sql += " AND r.change_id<>?"
            params.append(exclude_change)
        sql += " ORDER BY r.change_id"
        return self.connection.execute(sql, params).fetchall()

    def _compute_impact(
        self,
        snapshot: FacilitySnapshot,
        devices: list,
        window_start: str,
        window_end: str,
        exclude_change: str | None,
    ) -> dict[str, Any]:
        peers = self._peer_revision_rows(snapshot.site_id, exclude_change)
        impact = build_impact(snapshot, devices, peers)
        return annotate_conflicts(impact, snapshot, devices, window_start, window_end)

    def submit_rack_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rackchange.write")
        request = RackChangeRequest.from_dict(raw)
        snapshot_row = self._snapshot_row(request.site_id)
        snapshot = self._load_snapshot(snapshot_row)
        validate_devices_against_snapshot(request.devices, snapshot)
        devices_data = [device.as_dict() for device in request.devices]
        totals = change_totals(request.devices)
        impact = self._compute_impact(
            snapshot, request.devices, request.window_starts_at,
            request.window_ends_at, request.change_id,
        )
        now = self._now()
        definition = canonical_json(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rack_changes(change_id,site_id,title,current_revision,state,"
                    "snapshot_revision,submitted_by,created_at,updated_at) "
                    "VALUES(?,?,?,1,'pending_approval',?,?,?,?)",
                    (
                        request.change_id, request.site_id, request.title,
                        snapshot_row["revision"], actor_id, now, now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO rack_change_revisions(change_id,revision,site_id,snapshot_revision,"
                    "device_list_version,definition_json,totals_json,impact_json,window_starts_at,"
                    "window_ends_at,devices_json,state,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending_approval',?,?)",
                    (
                        request.change_id, 1, request.site_id, snapshot_row["revision"],
                        request.device_list_version, definition, canonical_json(totals),
                        canonical_json(impact), request.window_starts_at, request.window_ends_at,
                        canonical_json(devices_data), actor_id, now,
                    ),
                )
                self._audit(
                    "rack_change", request.change_id, "rackchange.submitted", actor_id,
                    {"revision": 1, "site_id": request.site_id, "feasible": impact["feasible"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("机柜上架变更编号已经存在") from exc
        return self.rack_change(request.change_id)

    def revise_rack_change(
        self, actor_id: str, change_id: str, raw: Mapping[str, Any], note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "rackchange.write")
        change = self._change_row(change_id)
        if change["submitted_by"] != actor_id:
            raise Forbidden("只有申请人可以修订自己的变更")
        if change["state"] not in ("returned", "rejected"):
            raise InvalidState("只有被退回或驳回的变更可以修订")
        request = RackChangeRequest.from_dict(raw)
        if request.change_id != change_id:
            raise ValidationFailed("修订内容的 change_id 必须与原变更一致")
        if request.site_id != change["site_id"]:
            raise ValidationFailed("修订不能改变站点")
        snapshot_row = self._snapshot_row(request.site_id)
        snapshot = self._load_snapshot(snapshot_row)
        validate_devices_against_snapshot(request.devices, snapshot)
        devices_data = [device.as_dict() for device in request.devices]
        totals = change_totals(request.devices)
        impact = self._compute_impact(
            snapshot, request.devices, request.window_starts_at,
            request.window_ends_at, change_id,
        )
        new_revision = int(change["current_revision"]) + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE rack_change_revisions SET state='superseded' WHERE change_id=? AND revision=?",
                (change_id, change["current_revision"]),
            )
            self.connection.execute(
                "INSERT INTO rack_change_revisions(change_id,revision,site_id,snapshot_revision,"
                "device_list_version,definition_json,totals_json,impact_json,window_starts_at,"
                "window_ends_at,devices_json,state,supersedes_revision,revision_note,"
                "submitted_by,submitted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending_approval',?,?,?,?)",
                (
                    change_id, new_revision, request.site_id, snapshot_row["revision"],
                    request.device_list_version, canonical_json(raw), canonical_json(totals),
                    canonical_json(impact), request.window_starts_at, request.window_ends_at,
                    canonical_json(devices_data), change["current_revision"], note, actor_id, now,
                ),
            )
            self.connection.execute(
                "UPDATE rack_changes SET current_revision=?,state='pending_approval',"
                "snapshot_revision=?,title=?,updated_at=? WHERE change_id=?",
                (new_revision, snapshot_row["revision"], request.title, now, change_id),
            )
            self._audit(
                "rack_change", change_id, "rackchange.revised", actor_id,
                {"revision": new_revision, "supersedes": change["current_revision"]},
            )
        return self.rack_change(change_id)

    def _change_row(self, change_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rack_changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("机柜上架变更不存在")
        return row

    def _revision_row(self, change_id: str, revision: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rack_change_revisions WHERE change_id=? AND revision=?",
            (change_id, revision),
        ).fetchone()
        if row is None:
            raise NotFound("变更修订不存在")
        return row

    def decide_rack_change(
        self,
        actor_id: str,
        change_id: str,
        decision: str,
        comment: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "rackchange.approve")
        if decision not in ("approved", "returned", "rejected"):
            raise ValidationFailed("decision 必须是 approved、returned 或 rejected")
        change = self._change_row(change_id)
        revision = expected_revision or int(change["current_revision"])
        revision_row = self._revision_row(change_id, revision)
        if int(change["current_revision"]) != revision:
            raise InvalidState("该修订不是当前版本")
        if change["state"] not in ("pending_approval", "returned"):
            raise InvalidState("只有待审批或已退回的变更可以作出决定")
        if change["state"] == "returned" and decision != "rejected":
            raise InvalidState("已退回的变更必须由申请人修订后重新提交，审批人只能将其驳回结案")
        if change["submitted_by"] == actor_id:
            raise Forbidden("申请人不能批准自己的变更")
        if not isinstance(comment, str) or not comment.strip():
            raise ValidationFailed("审批意见不能为空")

        now = self._now()
        with transaction(self.connection, immediate=True):
            if decision == "approved":
                # 批准前必须基于最新设施快照并按当前在途变更复算，防止排队期间被挤占
                latest_snapshot = self._snapshot_row(change["site_id"])
                if latest_snapshot["revision"] != int(revision_row["snapshot_revision"]):
                    raise InvalidState("设施快照已更新，申请人需要基于最新快照修订后重新提交")
                snapshot = self._load_snapshot(latest_snapshot)
                devices = [type(self)._device_from_json(item) for item in json.loads(revision_row["devices_json"])]
                impact = self._compute_impact(
                    snapshot, devices, revision_row["window_starts_at"],
                    revision_row["window_ends_at"], change_id,
                )
                if not impact["feasible"]:
                    raise InvalidState("影响分析未通过：存在容量、机位或散热冲突，不能批准")
                self.connection.execute(
                    "UPDATE rack_change_revisions SET impact_json=? WHERE change_id=? AND revision=?",
                    (canonical_json(impact), change_id, revision),
                )
                self._insert_locks(change_id, revision, change["site_id"], impact, devices, now)
                self._insert_step_receipts(change_id, revision, "execution", json.loads(revision_row["definition_json"]))
                new_state = "reserved"
            else:
                impact = json.loads(revision_row["impact_json"])
                new_state = "returned" if decision == "returned" else "rejected"
            basis = {
                "decision": decision,
                "decided_by": actor_id,
                "submitted_by": change["submitted_by"],
                "snapshot_revision": revision_row["snapshot_revision"],
                "feasible": impact.get("feasible"),
                "constraints": {
                    "site": impact.get("site_constraints"),
                    "ports": impact.get("port_constraints"),
                    "racks": list((impact.get("rack_constraints") or {}).keys()),
                },
                "conflicting_changes": [
                    item["change_id"] for item in impact.get("conflicting_changes", [])
                ],
                "rule": (
                    "施工窗口、资源预留与回退方案整体审批；审批人不得为申请人；"
                    "批准时按最新设施快照与全部在途变更复算余量。"
                ),
            }
            try:
                self.connection.execute(
                    "INSERT INTO change_approvals(change_id,revision,decision,decided_by,comment,"
                    "basis_json,decided_at) VALUES(?,?,?,?,?,?,?)",
                    (change_id, revision, decision, actor_id, comment.strip(), canonical_json(basis), now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该修订已经有相同决定") from exc
            self.connection.execute(
                "UPDATE rack_change_revisions SET state=? WHERE change_id=? AND revision=?",
                (new_state, change_id, revision),
            )
            self.connection.execute(
                "UPDATE rack_changes SET state=?,updated_at=? WHERE change_id=?",
                (new_state, now, change_id),
            )
            self._audit(
                "rack_change", change_id, f"rackchange.{decision}", actor_id,
                {"revision": revision, "comment": comment.strip()},
            )
        return self.rack_change(change_id)

    @staticmethod
    def _device_from_json(raw: Mapping[str, Any]) -> Any:
        from .rackchange import DeviceAsset

        payload = dict(raw)
        payload.pop("u_end", None)
        payload.pop("peak_power_kw", None)
        return DeviceAsset.from_dict(payload)

    def _insert_locks(
        self, change_id: str, revision: int, site_id: str,
        impact: Mapping[str, Any], devices: list, now: str,
    ) -> None:
        totals = impact["totals"]
        for dimension in ("power_kw", "cooling_kw", "weight_kg"):
            if Decimal(totals[dimension]) == 0:
                continue
            self.connection.execute(
                "INSERT INTO change_locks(change_id,revision,site_id,dimension,scope,scope_key,amount,locked_at) "
                "VALUES(?,?,?,?, 'site', ?, ?, ?)",
                (change_id, revision, site_id, dimension, site_id, totals[dimension], now),
            )
        for rack_id, node in impact["rack_constraints"].items():
            if Decimal(node["rack_weight_kg"]["requested"]) > 0:
                self.connection.execute(
                    "INSERT INTO change_locks(change_id,revision,site_id,dimension,scope,scope_key,amount,locked_at) "
                    "VALUES(?,?,?,?, 'rack', ?, ?, ?)",
                    (
                        change_id, revision, site_id, "rack_weight_kg", rack_id,
                        node["rack_weight_kg"]["requested"], now,
                    ),
                )
            if Decimal(node["rack_power_kw"]["requested"]) > 0:
                self.connection.execute(
                    "INSERT INTO change_locks(change_id,revision,site_id,dimension,scope,scope_key,amount,locked_at) "
                    "VALUES(?,?,?,?, 'rack', ?, ?, ?)",
                    (
                        change_id, revision, site_id, "rack_power_kw", rack_id,
                        node["rack_power_kw"]["requested"], now,
                    ),
                )
        for port_type, node in impact["port_constraints"].items():
            if int(node["requested"]) == 0:
                continue
            self.connection.execute(
                "INSERT INTO change_locks(change_id,revision,site_id,dimension,scope,scope_key,amount,locked_at) "
                "VALUES(?,?,?,?, 'port', ?, ?, ?)",
                (
                    change_id, revision, site_id, "port_count", port_type,
                    str(node["requested"]), now,
                ),
            )

    def _insert_step_receipts(
        self, change_id: str, revision: int, phase: str, definition: Mapping[str, Any]
    ) -> None:
        if phase == "execution":
            steps = [
                (index, item["code"], item["title"])
                for index, item in enumerate(definition["execution_steps"])
            ]
        else:
            steps = [
                (index, item["code"], item["title"])
                for index, item in enumerate(definition["rollback_plan"]["steps"])
            ]
        for index, code, title in steps:
            self.connection.execute(
                "INSERT INTO change_step_receipts(change_id,revision,phase,step_index,code,title,state) "
                "VALUES(?,?,?,?,?,?,'pending')",
                (change_id, revision, phase, index, code, title),
            )

    def start_rack_execution(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "rackchange.execute")
        change = self._change_row(change_id)
        if change["state"] != "reserved":
            raise InvalidState("只有已预留余量的变更可以开始施工")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE rack_changes SET state='in_progress',updated_at=? WHERE change_id=? AND state='reserved'",
                (now, change_id),
            )
            self.connection.execute(
                "UPDATE rack_change_revisions SET state='in_progress' WHERE change_id=? AND revision=?",
                (change_id, change["current_revision"]),
            )
            self._audit("rack_change", change_id, "rackchange.execution_started", actor_id, {})
        return self.rack_change(change_id)

    def record_execution_step(
        self, actor_id: str, change_id: str, step_index: int, success: bool, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "rackchange.execute")
        change = self._change_row(change_id)
        if change["state"] != "in_progress":
            raise InvalidState("变更不在施工中，不能登记施工回执")
        self._assert_ordered_step(change_id, change["current_revision"], "execution", step_index)
        now = self._now()
        with transaction(self.connection, immediate=True):
            if success:
                self._mark_step(change_id, change["current_revision"], "execution", step_index, "done", actor_id, note, now)
                remaining = self.connection.execute(
                    "SELECT count(*) FROM change_step_receipts WHERE change_id=? AND revision=? "
                    "AND phase='execution' AND state<>'done'",
                    (change_id, change["current_revision"]),
                ).fetchone()[0]
                if remaining == 0:
                    self._complete_change(change_id, actor_id, now)
                    event = "rackchange.completed"
                else:
                    event = "rackchange.step_done"
            else:
                self._mark_step(change_id, change["current_revision"], "execution", step_index, "failed", actor_id, note, now)
                self.connection.execute(
                    "UPDATE rack_changes SET state='failed',updated_at=? WHERE change_id=?",
                    (now, change_id),
                )
                self.connection.execute(
                    "UPDATE rack_change_revisions SET state='failed' WHERE change_id=? AND revision=?",
                    (change_id, change["current_revision"]),
                )
                event = "rackchange.step_failed"
            self._audit("rack_change", change_id, event, actor_id,
                        {"step_index": step_index, "success": bool(success)})
        return self.rack_change(change_id)

    def _assert_ordered_step(
        self, change_id: str, revision: int, phase: str, step_index: int
    ) -> None:
        current = self.connection.execute(
            "SELECT * FROM change_step_receipts WHERE change_id=? AND revision=? AND phase=? AND step_index=?",
            (change_id, revision, phase, step_index),
        ).fetchone()
        if current is None:
            raise NotFound("施工步骤不存在")
        if current["state"] == "done":
            raise InvalidState("该步骤已有成功回执，不能重复登记")
        if current["state"] == "failed" and phase == "execution":
            raise InvalidState("该步骤已失败，必须先回退或申请人工接管")
        previous = self.connection.execute(
            "SELECT state FROM change_step_receipts WHERE change_id=? AND revision=? AND phase=? "
            "AND step_index<? ORDER BY step_index",
            (change_id, revision, phase, step_index),
        ).fetchall()
        if any(row["state"] != "done" for row in previous):
            raise InvalidState("必须按步骤顺序登记回执，前置步骤尚未全部成功")

    def _mark_step(
        self, change_id: str, revision: int, phase: str, step_index: int,
        state: str, actor_id: str, note: str, now: str,
    ) -> None:
        self.connection.execute(
            "UPDATE change_step_receipts SET state=?,recorded_by=?,recorded_at=?,note=? "
            "WHERE change_id=? AND revision=? AND phase=? AND step_index=?",
            (state, actor_id, now, note, change_id, revision, phase, step_index),
        )

    def _complete_change(self, change_id: str, actor_id: str, now: str) -> None:
        revision_row = self._revision_row(change_id, self._change_row(change_id)["current_revision"])
        self.connection.execute(
            "UPDATE rack_changes SET state='completed',updated_at=? WHERE change_id=?",
            (now, change_id),
        )
        self.connection.execute(
            "UPDATE rack_change_revisions SET state='completed' WHERE change_id=? AND revision=?",
            (change_id, revision_row["revision"]),
        )
        # 余量转为已完成消耗（经已完成修订计入基线之外的消耗），预留锁释放
        self.connection.execute(
            "DELETE FROM change_locks WHERE change_id=? AND revision=?",
            (change_id, revision_row["revision"]),
        )

    def begin_rollback(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "rackchange.execute")
        change = self._change_row(change_id)
        if change["state"] != "failed":
            raise InvalidState("只有施工失败的变更可以开始回退")
        revision_row = self._revision_row(change_id, change["current_revision"])
        definition = json.loads(revision_row["definition_json"])
        now = self._now()
        with transaction(self.connection, immediate=True):
            self._insert_step_receipts(change_id, change["current_revision"], "rollback", definition)
            self.connection.execute(
                "UPDATE rack_changes SET state='rollback_in_progress',updated_at=? WHERE change_id=?",
                (now, change_id),
            )
            self.connection.execute(
                "UPDATE rack_change_revisions SET state='rollback_in_progress' WHERE change_id=? AND revision=?",
                (change_id, change["current_revision"]),
            )
            self._audit("rack_change", change_id, "rackchange.rollback_started", actor_id, {})
        return self.rack_change(change_id)

    def record_rollback_step(
        self, actor_id: str, change_id: str, step_index: int, success: bool, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "rackchange.execute")
        change = self._change_row(change_id)
        if change["state"] != "rollback_in_progress":
            raise InvalidState("变更不在回退中，不能登记回退回执")
        revision = int(change["current_revision"])
        now = self._now()
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT state FROM change_step_receipts WHERE change_id=? AND revision=? "
                "AND phase='rollback' AND step_index=?",
                (change_id, revision, step_index),
            ).fetchone()
            if current is None:
                raise NotFound("回退步骤不存在")
            if current["state"] == "done":
                raise InvalidState("该回退步骤已完成，不能重复登记")
            if current["state"] != "failed":
                self._assert_ordered_step(change_id, revision, "rollback", step_index)
            if success:
                self._mark_step(change_id, revision, "rollback", step_index, "done", actor_id, note, now)
                remaining = self.connection.execute(
                    "SELECT count(*) FROM change_step_receipts WHERE change_id=? AND revision=? "
                    "AND phase='rollback' AND state<>'done'",
                    (change_id, revision),
                ).fetchone()[0]
                if remaining == 0:
                    self.connection.execute(
                        "UPDATE rack_changes SET state='rolled_back',updated_at=? WHERE change_id=?",
                        (now, change_id),
                    )
                    self.connection.execute(
                        "UPDATE rack_change_revisions SET state='rolled_back' WHERE change_id=? AND revision=?",
                        (change_id, revision),
                    )
                    self.connection.execute(
                        "DELETE FROM change_locks WHERE change_id=? AND revision=?",
                        (change_id, revision),
                    )
                    event = "rackchange.rolled_back"
                else:
                    event = "rackchange.rollback_step_done"
            else:
                self._mark_step(change_id, revision, "rollback", step_index, "failed", actor_id, note, now)
                event = "rackchange.rollback_step_failed"
            self._audit("rack_change", change_id, event, actor_id,
                        {"step_index": step_index, "success": bool(success)})
        return self.rack_change(change_id)

    def manual_takeover(self, actor_id: str, change_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "rackchange.approve")
        change = self._change_row(change_id)
        if change["state"] not in ("failed", "rollback_in_progress"):
            raise InvalidState("只有施工失败或回退受阻的变更可以转人工接管")
        if not note.strip():
            raise ValidationFailed("人工接管必须说明处置安排")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE rack_changes SET state='manual_takeover',updated_at=? WHERE change_id=?",
                (now, change_id),
            )
            self.connection.execute(
                "UPDATE rack_change_revisions SET state='manual_takeover' WHERE change_id=? AND revision=?",
                (change_id, change["current_revision"]),
            )
            # 人工接管期间继续保留余量锁，避免余量被其他申请挤占
            self._audit("rack_change", change_id, "rackchange.manual_takeover", actor_id, {"note": note.strip()})
        return self.rack_change(change_id)

    def cancel_rack_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "rackchange.write")
        change = self._change_row(change_id)
        if change["submitted_by"] != actor_id:
            raise Forbidden("只有申请人可以撤销自己的变更")
        if change["state"] not in ("pending_approval", "returned"):
            raise InvalidState("只有待审批或退回的变更可以撤销")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE rack_changes SET state='cancelled',updated_at=? WHERE change_id=?",
                (now, change_id),
            )
            self.connection.execute(
                "UPDATE rack_change_revisions SET state='cancelled' WHERE change_id=? AND revision=?",
                (change_id, change["current_revision"]),
            )
            self._audit("rack_change", change_id, "rackchange.cancelled", actor_id, {})
        return self.rack_change(change_id)

    def rack_change(self, change_id: str) -> dict[str, Any]:
        change = self._change_row(change_id)
        revision_rows = self.connection.execute(
            "SELECT * FROM rack_change_revisions WHERE change_id=? ORDER BY revision",
            (change_id,),
        ).fetchall()
        approvals = self.connection.execute(
            "SELECT approval_id,revision,decision,decided_by,comment,basis_json,decided_at "
            "FROM change_approvals WHERE change_id=? ORDER BY approval_id",
            (change_id,),
        ).fetchall()
        receipts = self.connection.execute(
            "SELECT * FROM change_step_receipts WHERE change_id=? AND revision=? "
            "ORDER BY phase,step_index",
            (change_id, change["current_revision"]),
        ).fetchall()
        locks = self.connection.execute(
            "SELECT dimension,scope,scope_key,amount,locked_at FROM change_locks "
            "WHERE change_id=? AND revision=? ORDER BY dimension,scope,scope_key",
            (change_id, change["current_revision"]),
        ).fetchall()
        latest = revision_rows[-1]
        stored_impact = json.loads(latest["impact_json"]) if latest["impact_json"] else None
        # 待审批期间余量可能被其他变更挤占：详情按当前快照与在途变更实时复算，
        # 审批时点的依据则永久保存在 approvals[].basis。
        impact = stored_impact
        if change["state"] in ("pending_approval", "returned"):
            snapshot_row = self.connection.execute(
                "SELECT * FROM facility_snapshots WHERE site_id=? AND revision=?",
                (change["site_id"], latest["snapshot_revision"]),
            ).fetchone()
            if snapshot_row is not None:
                snapshot = self._load_snapshot(snapshot_row)
                devices = [self._device_from_json(item) for item in json.loads(latest["devices_json"])]
                impact = self._compute_impact(
                    snapshot, devices, latest["window_starts_at"],
                    latest["window_ends_at"], change_id,
                )
        return {
            "change_id": change_id,
            "site_id": change["site_id"],
            "title": change["title"],
            "state": change["state"],
            "current_revision": change["current_revision"],
            "snapshot_revision": change["snapshot_revision"],
            "submitted_by": change["submitted_by"],
            "created_at": change["created_at"],
            "updated_at": change["updated_at"],
            "device_list_version": latest["device_list_version"],
            "window": {
                "starts_at": latest["window_starts_at"],
                "ends_at": latest["window_ends_at"],
            },
            "totals": json.loads(latest["totals_json"]),
            "impact": impact,
            "revision_chain": [
                {
                    "revision": row["revision"],
                    "state": row["state"],
                    "snapshot_revision": row["snapshot_revision"],
                    "device_list_version": row["device_list_version"],
                    "supersedes_revision": row["supersedes_revision"],
                    "revision_note": row["revision_note"],
                    "submitted_by": row["submitted_by"],
                    "submitted_at": row["submitted_at"],
                }
                for row in revision_rows
            ],
            "approvals": [
                {
                    "approval_id": row["approval_id"],
                    "revision": row["revision"],
                    "decision": row["decision"],
                    "decided_by": row["decided_by"],
                    "comment": row["comment"],
                    "decided_at": row["decided_at"],
                    "basis": json.loads(row["basis_json"]),
                }
                for row in approvals
            ],
            "locks": [dict(row) for row in locks],
            "receipts": {
                "execution": [
                    self._receipt_dict(row) for row in receipts if row["phase"] == "execution"
                ],
                "rollback": [
                    self._receipt_dict(row) for row in receipts if row["phase"] == "rollback"
                ],
            },
        }

    @staticmethod
    def _receipt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "step_index": row["step_index"],
            "code": row["code"],
            "title": row["title"],
            "state": row["state"],
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
            "note": row["note"],
        }

    def list_rack_changes(self, site_id: str | None = None) -> dict[str, Any]:
        if site_id is None:
            rows = self.connection.execute(
                "SELECT * FROM rack_changes ORDER BY site_id,change_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM rack_changes WHERE site_id=? ORDER BY change_id", (site_id,)
            ).fetchall()
        return {"changes": [dict(row) for row in rows]}

    def site_capacity(self, site_id: str) -> dict[str, Any]:
        """展示站点每项约束的总量、基线消耗、已完成消耗、已锁定预留与剩余量。"""

        snapshot_row = self._snapshot_row(site_id)
        snapshot = self._load_snapshot(snapshot_row)
        peers = self._peer_revision_rows(site_id)
        impact = build_impact(snapshot, [], peers)
        return {
            "site_id": site_id,
            "snapshot_revision": snapshot_row["revision"],
            "content_sha256": snapshot_row["content_sha256"],
            "site_constraints": impact["site_constraints"],
            "port_constraints": impact["port_constraints"],
            "open_changes": [
                {
                    "change_id": row["change_id"],
                    "revision": row["revision"],
                    "state": row["state"],
                    "window_starts_at": row["window_starts_at"],
                    "window_ends_at": row["window_ends_at"],
                    "totals": json.loads(row["totals_json"]),
                }
                for row in peers
                if row["state"] in LOCKED_STATES or row["state"] in PENDING_STATES
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
