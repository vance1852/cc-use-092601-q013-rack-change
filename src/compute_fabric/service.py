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
from .models import IndexQuote, Facility, InventoryLot, NominationRequest, Route, SupplyScenario, decimal_value, required_text
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
from .rack_change import (
    VISIBLE_STATES,
    RackChangeDraft,
    build_impact,
    normalize_constraint_key,
    required_demands,
    unit_for,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "rack_change.read"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read", "rack_change.approve", "rack_change.read"},
    "auditor": {"report.read", "audit.read", "rack_change.read"},
    "facilities": {
        "constraint.write",
        "rack_change.write",
        "rack_change.approve",
        "rack_change.execute",
        "rack_change.read",
        "report.read",
    },
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
    # 设施约束容量目录
    # ------------------------------------------------------------------

    def upsert_facility_constraint(self, actor_id: str, facility_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "constraint.write")
        if self.connection.execute("SELECT 1 FROM facilities WHERE facility_id=?", (facility_id,)).fetchone() is None:
            raise NotFound("设施不存在")
        constraint_key = normalize_constraint_key(raw.get("constraint_key"))
        capacity = decimal_value(raw.get("capacity"), "capacity", minimum=Decimal("0"))
        unit = required_text(raw.get("unit"), "unit", 16) if raw.get("unit") else unit_for(constraint_key)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT * FROM facility_constraints WHERE facility_id=? AND constraint_key=?",
                (facility_id, constraint_key),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO facility_constraints(facility_id,constraint_key,unit,capacity,used,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (facility_id, constraint_key, unit, decimal_text(capacity), "0", self._now()),
                )
            else:
                locked_total = Decimal(existing["used"]) + Decimal(existing["reserved"])
                if capacity < locked_total:
                    raise Conflict("容量不能小于已用与已锁定余量之和")
                self.connection.execute(
                    "UPDATE facility_constraints SET capacity=?,unit=?,revision=revision+1 "
                    "WHERE facility_id=? AND constraint_key=?",
                    (decimal_text(capacity), unit, facility_id, constraint_key),
                )
            self._audit(
                "facility_constraint",
                f"{facility_id}:{constraint_key}",
                "constraint.upserted",
                actor_id,
                {"facility_id": facility_id, "constraint_key": constraint_key, "capacity": decimal_text(capacity)},
            )
        return self.facility_constraints(facility_id)

    def facility_constraints(self, facility_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM facility_constraints WHERE facility_id=? ORDER BY constraint_key",
            (facility_id,),
        ).fetchall()
        items = []
        for row in rows:
            capacity = Decimal(row["capacity"])
            used = Decimal(row["used"])
            reserved = Decimal(row["reserved"])
            items.append(
                {
                    "constraint_key": row["constraint_key"],
                    "unit": row["unit"],
                    "capacity": decimal_text(capacity),
                    "used": decimal_text(used),
                    "reserved": decimal_text(reserved),
                    "remaining": decimal_text(quantize_volume(capacity - used - reserved)),
                    "revision": row["revision"],
                }
            )
        return {"facility_id": facility_id, "constraints": items}

    # ------------------------------------------------------------------
    # 机柜上架变更
    # ------------------------------------------------------------------

    def _facility_snapshot(self, facility_id: str, exclude_change: str | None = None) -> dict[str, Any]:
        constraints = [
            dict(row)
            for row in self.connection.execute(
                "SELECT facility_id,constraint_key,unit,capacity,used,reserved,revision "
                "FROM facility_constraints WHERE facility_id=? ORDER BY constraint_key",
                (facility_id,),
            ).fetchall()
        ]
        placeholders = ",".join("?" for _ in VISIBLE_STATES)
        rows = self.connection.execute(
            f"SELECT * FROM rack_changes WHERE facility_id=? AND state IN ({placeholders}) ORDER BY change_id,revision",
            (facility_id, *sorted(VISIBLE_STATES)),
        ).fetchall()
        others: list[dict[str, Any]] = []
        for row in rows:
            if exclude_change is not None and row["change_id"] == exclude_change:
                continue
            others.append(
                {
                    "change_id": row["change_id"],
                    "revision": row["revision"],
                    "state": row["state"],
                    "window_starts_at": row["window_starts_at"],
                    "window_ends_at": row["window_ends_at"],
                    "demands": [
                        {"constraint_key": item["constraint_key"], "required_value": item["required_value"]}
                        for item in self.connection.execute(
                            "SELECT constraint_key,required_value FROM rack_change_demands "
                            "WHERE change_id=? AND revision=? ORDER BY constraint_key",
                            (row["change_id"], row["revision"]),
                        ).fetchall()
                    ],
                    "locations": [
                        {"rack_id": item["rack_id"], "u_start": item["u_start"], "u_size": item["u_size"]}
                        for item in self.connection.execute(
                            "SELECT rack_id,u_start,u_size FROM rack_change_locations "
                            "WHERE change_id=? AND revision=? ORDER BY rack_id,u_start",
                            (row["change_id"], row["revision"]),
                        ).fetchall()
                    ],
                }
            )
        return {"facility_id": facility_id, "constraints": constraints, "others": others}

    def _recompute_impact(self, draft: RackChangeDraft, demands: Mapping[str, Decimal], exclude_change: str) -> dict[str, Any]:
        snapshot = self._facility_snapshot(draft.facility_id, exclude_change)
        locations = [location.as_dict() for location in draft.locations]
        window = {"starts_at": draft.window_starts_at, "ends_at": draft.window_ends_at}
        impact = build_impact(snapshot, draft.change_id, demands, locations, window)
        impact["snapshot_sha256"] = digest({"constraints": snapshot["constraints"], "others": snapshot["others"]})
        return impact

    def submit_rack_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rack_change.write")
        draft = RackChangeDraft.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM facilities WHERE facility_id=?", (draft.facility_id,)).fetchone() is None:
            raise NotFound("设施不存在")
        demands = required_demands(draft)
        content = draft.content()
        content_sha256 = digest(content)
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT * FROM rack_changes WHERE change_id=? ORDER BY revision DESC LIMIT 1",
                (draft.change_id,),
            ).fetchone()
            revision = 1
            if latest is not None:
                if latest["state"] not in {"submitted", "rejected"}:
                    raise InvalidState("当前版本不处于可修订状态")
                revision = int(latest["revision"]) + 1
                if latest["state"] == "submitted" and latest["content_sha256"] == content_sha256:
                    return self.rack_change(draft.change_id, int(latest["revision"]))
            impact = self._recompute_impact(draft, demands, draft.change_id)
            self.connection.execute(
                "INSERT INTO rack_changes(change_id,revision,facility_id,title,bom_version,content_json,"
                "content_sha256,state,window_starts_at,window_ends_at,submitted_by,supersedes_revision,"
                "impact_json,impact_built_at,snapshot_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.change_id,
                    revision,
                    draft.facility_id,
                    draft.title,
                    draft.bom_version,
                    canonical_json(content),
                    content_sha256,
                    "submitted",
                    draft.window_starts_at,
                    draft.window_ends_at,
                    actor_id,
                    None if latest is None else int(latest["revision"]),
                    canonical_json(impact),
                    self._now(),
                    impact["snapshot_sha256"],
                    self._now(),
                ),
            )
            if latest is not None and latest["state"] == "submitted":
                self.connection.execute(
                    "UPDATE rack_changes SET state='superseded' WHERE change_id=? AND revision=? AND state='submitted'",
                    (draft.change_id, int(latest["revision"])),
                )
            for key, value in demands.items():
                self.connection.execute(
                    "INSERT INTO rack_change_demands(change_id,revision,constraint_key,unit,required_value) "
                    "VALUES(?,?,?,?,?)",
                    (draft.change_id, revision, key, unit_for(key), decimal_text(value)),
                )
            for location in draft.locations:
                self.connection.execute(
                    "INSERT INTO rack_change_locations(change_id,revision,rack_id,u_start,u_size) VALUES(?,?,?,?,?)",
                    (draft.change_id, revision, location.rack_id, location.u_start, location.u_size),
                )
            for step in draft.steps:
                self.connection.execute(
                    "INSERT INTO rack_change_steps(change_id,revision,sequence,name,rollback_action) VALUES(?,?,?,?,?)",
                    (draft.change_id, revision, step.sequence, step.name, step.rollback_action),
                )
            self._audit(
                "rack_change",
                draft.change_id,
                "rack_change.submitted",
                actor_id,
                {"revision": revision, "bom_version": draft.bom_version, "feasible": impact["feasible"]},
            )
        return self.rack_change(draft.change_id, revision)

    def _revision_row(self, change_id: str, revision: int | None) -> sqlite3.Row:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM rack_changes WHERE change_id=? ORDER BY revision DESC LIMIT 1",
                (change_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM rack_changes WHERE change_id=? AND revision=?",
                (change_id, revision),
            ).fetchone()
        if row is None:
            raise NotFound("机柜上架变更不存在")
        return row

    def rack_change(self, change_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self._revision_row(change_id, revision)
        revisions = [
            {
                "revision": item["revision"],
                "state": item["state"],
                "bom_version": item["bom_version"],
                "content_sha256": item["content_sha256"],
                "supersedes_revision": item["supersedes_revision"],
                "submitted_by": item["submitted_by"],
                "created_at": item["created_at"],
                "decision_by": item["decision_by"],
                "decision_at": item["decision_at"],
                "decision_basis": item["decision_basis"],
                "fail_reason": item["fail_reason"],
            }
            for item in self.connection.execute(
                "SELECT revision,state,bom_version,content_sha256,supersedes_revision,submitted_by,created_at,"
                "decision_by,decision_at,decision_basis,fail_reason FROM rack_changes WHERE change_id=? ORDER BY revision",
                (change_id,),
            ).fetchall()
        ]
        steps = [
            {
                "sequence": item["sequence"],
                "name": item["name"],
                "rollback_action": item["rollback_action"],
                "state": item["state"],
                "receipt_by": item["receipt_by"],
                "receipt_note": item["receipt_note"],
                "completed_at": item["completed_at"],
            }
            for item in self.connection.execute(
                "SELECT sequence,name,rollback_action,state,receipt_by,receipt_note,completed_at "
                "FROM rack_change_steps WHERE change_id=? AND revision=? ORDER BY sequence",
                (change_id, row["revision"]),
            ).fetchall()
        ]
        reservations = [
            dict(item)
            for item in self.connection.execute(
                "SELECT constraint_key,reserved_value,released_value,state,locked_at,released_at "
                "FROM rack_change_reservations WHERE change_id=? AND revision=? ORDER BY constraint_key",
                (change_id, row["revision"]),
            ).fetchall()
        ]
        if row["state"] == "submitted":
            # 待批版本按当前快照实时展示剩余量与冲突；批准后展示锁定时快照。
            impact = self._stored_impact(row)
            impact["freshness"] = "live"
        else:
            impact = json.loads(row["impact_json"])
            impact["freshness"] = "locked_at_decision"
        return {
            "change_id": change_id,
            "revision": row["revision"],
            "facility_id": row["facility_id"],
            "title": row["title"],
            "bom_version": row["bom_version"],
            "state": row["state"],
            "submitted_by": row["submitted_by"],
            "window_starts_at": row["window_starts_at"],
            "window_ends_at": row["window_ends_at"],
            "content": json.loads(row["content_json"]),
            "impact": impact,
            "decision": {
                "by": row["decision_by"],
                "at": row["decision_at"],
                "basis": row["decision_basis"],
                "snapshot_sha256": row["snapshot_sha256"],
            },
            "fail_reason": row["fail_reason"],
            "steps": steps,
            "reservations": reservations,
            "revisions": revisions,
        }

    def list_rack_changes(self, facility_id: str | None = None) -> dict[str, Any]:
        if facility_id is None:
            rows = self.connection.execute(
                "SELECT * FROM rack_changes r WHERE revision=(SELECT max(revision) FROM rack_changes WHERE change_id=r.change_id) "
                "ORDER BY change_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM rack_changes r WHERE facility_id=? AND revision="
                "(SELECT max(revision) FROM rack_changes WHERE change_id=r.change_id) ORDER BY change_id",
                (facility_id,),
            ).fetchall()
        return {
            "changes": [
                {
                    "change_id": row["change_id"],
                    "revision": row["revision"],
                    "facility_id": row["facility_id"],
                    "title": row["title"],
                    "state": row["state"],
                    "submitted_by": row["submitted_by"],
                    "window_starts_at": row["window_starts_at"],
                    "window_ends_at": row["window_ends_at"],
                    "feasible": json.loads(row["impact_json"])["feasible"],
                }
                for row in rows
            ]
        }

    def _stored_impact(self, row: sqlite3.Row) -> dict[str, Any]:
        demands = {
            item["constraint_key"]: Decimal(item["required_value"])
            for item in self.connection.execute(
                "SELECT constraint_key,required_value FROM rack_change_demands WHERE change_id=? AND revision=?",
                (row["change_id"], row["revision"]),
            ).fetchall()
        }
        locations = [
            dict(item)
            for item in self.connection.execute(
                "SELECT rack_id,u_start,u_size FROM rack_change_locations WHERE change_id=? AND revision=?",
                (row["change_id"], row["revision"]),
            ).fetchall()
        ]
        snapshot = self._facility_snapshot(row["facility_id"], row["change_id"])
        window = {"starts_at": row["window_starts_at"], "ends_at": row["window_ends_at"]}
        impact = build_impact(snapshot, row["change_id"], demands, locations, window)
        impact["snapshot_sha256"] = digest({"constraints": snapshot["constraints"], "others": snapshot["others"]})
        return impact

    def _decide_rack_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        approved: bool,
        basis: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "rack_change.approve")
        basis = basis.strip() if isinstance(basis, str) else ""
        if not basis:
            raise ValidationFailed("审批必须给出决定依据")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM rack_changes WHERE change_id=? AND revision=?",
                (change_id, expected_revision),
            ).fetchone()
            if row is None:
                raise NotFound("机柜上架变更版本不存在")
            if row["state"] != "submitted":
                raise InvalidState("只有待批版本可以审批")
            if row["submitted_by"] == actor_id:
                raise Forbidden("申请人不能批准自己的变更")
            if approved:
                impact = self._stored_impact(row)
                if not impact["feasible"]:
                    raise Conflict("影响分析存在硬性冲突，不能批准锁定")
                for item in impact["constraints"]:
                    key = item["constraint_key"]
                    value = Decimal(item["required"])
                    constraint = self.connection.execute(
                        "SELECT * FROM facility_constraints WHERE facility_id=? AND constraint_key=?",
                        (row["facility_id"], key),
                    ).fetchone()
                    if constraint is None:
                        raise Conflict(f"设施目录缺少约束 {key}")
                    reserved = Decimal(constraint["reserved"]) + value
                    self.connection.execute(
                        "UPDATE facility_constraints SET reserved=?,revision=revision+1 "
                        "WHERE facility_id=? AND constraint_key=?",
                        (decimal_text(quantize_volume(reserved)), row["facility_id"], key),
                    )
                    self.connection.execute(
                        "UPDATE rack_change_demands SET snapshot_capacity=?,snapshot_used=?,snapshot_reserved=? "
                        "WHERE change_id=? AND revision=? AND constraint_key=?",
                        (
                            constraint["capacity"],
                            constraint["used"],
                            constraint["reserved"],
                            change_id,
                            expected_revision,
                            key,
                        ),
                    )
                    self.connection.execute(
                        "INSERT INTO rack_change_reservations(change_id,revision,facility_id,constraint_key,"
                        "reserved_value,locked_at) VALUES(?,?,?,?,?,?)",
                        (change_id, expected_revision, row["facility_id"], key, decimal_text(value), self._now()),
                    )
                new_state = "approved"
                self.connection.execute(
                    "UPDATE rack_changes SET impact_json=?,impact_built_at=?,snapshot_sha256=? "
                    "WHERE change_id=? AND revision=?",
                    (
                        canonical_json(impact),
                        self._now(),
                        impact["snapshot_sha256"],
                        change_id,
                        expected_revision,
                    ),
                )
            else:
                new_state = "rejected"
            self.connection.execute(
                "UPDATE rack_changes SET state=?,decision_by=?,decision_at=?,decision_basis=? "
                "WHERE change_id=? AND revision=?",
                (new_state, actor_id, self._now(), basis, change_id, expected_revision),
            )
            self._audit(
                "rack_change",
                change_id,
                "rack_change.approved" if approved else "rack_change.rejected",
                actor_id,
                {"revision": expected_revision, "basis": basis},
            )
        return self.rack_change(change_id, expected_revision)

    def approve_rack_change(self, actor_id: str, change_id: str, expected_revision: int, basis: str) -> dict[str, Any]:
        return self._decide_rack_change(actor_id, change_id, expected_revision, True, basis)

    def reject_rack_change(self, actor_id: str, change_id: str, expected_revision: int, basis: str) -> dict[str, Any]:
        return self._decide_rack_change(actor_id, change_id, expected_revision, False, basis)

    def cancel_rack_change(self, actor_id: str, change_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "rack_change.write")
        with transaction(self.connection, immediate=True):
            row = self._revision_row(change_id, None)
            if row["state"] != "submitted":
                raise InvalidState("只有待批变更可以撤回")
            if row["submitted_by"] != actor_id:
                raise Forbidden("只能撤回本人提交的变更")
            self.connection.execute(
                "UPDATE rack_changes SET state='cancelled' WHERE change_id=? AND revision=?",
                (change_id, row["revision"]),
            )
            self._audit(
                "rack_change", change_id, "rack_change.cancelled", actor_id, {"revision": row["revision"], "note": note}
            )
        return self.rack_change(change_id, row["revision"])

    def _set_state(self, change_id: str, revision: int, state: str, fail_reason: str | None = None) -> None:
        self.connection.execute(
            "UPDATE rack_changes SET state=?,fail_reason=? WHERE change_id=? AND revision=?",
            (state, fail_reason, change_id, revision),
        )

    def _release_reservations(self, change_id: str, revision: int) -> None:
        rows = self.connection.execute(
            "SELECT * FROM rack_change_reservations WHERE change_id=? AND revision=? AND state='locked'",
            (change_id, revision),
        ).fetchall()
        for row in rows:
            constraint = self.connection.execute(
                "SELECT reserved FROM facility_constraints WHERE facility_id=? AND constraint_key=?",
                (row["facility_id"], row["constraint_key"]),
            ).fetchone()
            remaining = quantize_volume(Decimal(constraint["reserved"]) - Decimal(row["reserved_value"]))
            self.connection.execute(
                "UPDATE facility_constraints SET reserved=?,revision=revision+1 WHERE facility_id=? AND constraint_key=?",
                (decimal_text(remaining), row["facility_id"], row["constraint_key"]),
            )
            self.connection.execute(
                "UPDATE rack_change_reservations SET state='released',released_value=reserved_value,released_at=? "
                "WHERE change_id=? AND revision=? AND constraint_key=?",
                (self._now(), change_id, revision, row["constraint_key"]),
            )

    def _consume_reservations(self, change_id: str, revision: int) -> None:
        rows = self.connection.execute(
            "SELECT * FROM rack_change_reservations WHERE change_id=? AND revision=? AND state='locked'",
            (change_id, revision),
        ).fetchall()
        for row in rows:
            constraint = self.connection.execute(
                "SELECT used,reserved FROM facility_constraints WHERE facility_id=? AND constraint_key=?",
                (row["facility_id"], row["constraint_key"]),
            ).fetchone()
            used = quantize_volume(Decimal(constraint["used"]) + Decimal(row["reserved_value"]))
            reserved = quantize_volume(Decimal(constraint["reserved"]) - Decimal(row["reserved_value"]))
            self.connection.execute(
                "UPDATE facility_constraints SET used=?,reserved=?,revision=revision+1 "
                "WHERE facility_id=? AND constraint_key=?",
                (decimal_text(used), decimal_text(reserved), row["facility_id"], row["constraint_key"]),
            )
            self.connection.execute(
                "UPDATE rack_change_reservations SET state='consumed' WHERE change_id=? AND revision=? AND constraint_key=?",
                (change_id, revision, row["constraint_key"]),
            )

    def confirm_step(self, actor_id: str, change_id: str, sequence: int, note: str) -> dict[str, Any]:
        self._require(actor_id, "rack_change.execute")
        with transaction(self.connection, immediate=True):
            row = self._revision_row(change_id, None)
            if row["state"] not in {"approved", "in_progress"}:
                raise InvalidState("变更未进入可施工状态")
            steps = self.connection.execute(
                "SELECT * FROM rack_change_steps WHERE change_id=? AND revision=? ORDER BY sequence",
                (change_id, row["revision"]),
            ).fetchall()
            target = next((item for item in steps if item["sequence"] == sequence), None)
            if target is None:
                raise NotFound("实施步骤不存在")
            if target["state"] != "pending":
                raise InvalidState("该步骤已经回执，不能重复确认")
            for item in steps:
                if item["sequence"] < sequence and item["state"] != "done":
                    raise InvalidState("必须按步骤顺序回执，前序步骤尚未完成")
            self.connection.execute(
                "UPDATE rack_change_steps SET state='done',receipt_by=?,receipt_note=?,completed_at=? "
                "WHERE change_id=? AND revision=? AND sequence=?",
                (actor_id, note, self._now(), change_id, row["revision"], sequence),
            )
            if row["state"] == "approved":
                self._set_state(change_id, row["revision"], "in_progress")
            remaining = self.connection.execute(
                "SELECT count(*) n FROM rack_change_steps WHERE change_id=? AND revision=? AND state!='done'",
                (change_id, row["revision"]),
            ).fetchone()["n"]
            completed = remaining == 0
            if completed:
                self._consume_reservations(change_id, row["revision"])
                self._set_state(change_id, row["revision"], "completed")
            self._audit(
                "rack_change",
                change_id,
                "rack_change.step_confirmed",
                actor_id,
                {"revision": row["revision"], "sequence": sequence, "completed": completed},
            )
        return self.rack_change(change_id, row["revision"])

    def fail_step(
        self,
        actor_id: str,
        change_id: str,
        sequence: int,
        reason: str,
        mode: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "rack_change.execute")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("失败步骤必须记录原因")
        if mode not in {"rollback", "manual"}:
            raise ValidationFailed("mode 必须是 rollback 或 manual")
        with transaction(self.connection, immediate=True):
            row = self._revision_row(change_id, None)
            if row["state"] not in {"approved", "in_progress"}:
                raise InvalidState("变更未处于可施工状态")
            steps = self.connection.execute(
                "SELECT * FROM rack_change_steps WHERE change_id=? AND revision=? ORDER BY sequence",
                (change_id, row["revision"]),
            ).fetchall()
            target = next((item for item in steps if item["sequence"] == sequence), None)
            if target is None:
                raise NotFound("实施步骤不存在")
            if target["state"] != "pending":
                raise InvalidState("只能对待执行步骤登记失败")
            for item in steps:
                if item["sequence"] < sequence and item["state"] != "done":
                    raise InvalidState("失败步骤之前存在未完成步骤，回执顺序不正确")
            self.connection.execute(
                "UPDATE rack_change_steps SET state='failed',receipt_by=?,receipt_note=?,completed_at=? "
                "WHERE change_id=? AND revision=? AND sequence=?",
                (actor_id, reason, self._now(), change_id, row["revision"], sequence),
            )
            # 失败点之后的步骤从未执行，标记 skipped，部分完成绝不能被视为成功。
            self.connection.execute(
                "UPDATE rack_change_steps SET state='skipped' WHERE change_id=? AND revision=? "
                "AND sequence>? AND state='pending'",
                (change_id, row["revision"], sequence),
            )
            new_state = "rolling_back" if mode == "rollback" else "manual_takeover"
            fail_reason = reason.strip()
            if mode == "rollback":
                done_count = self.connection.execute(
                    "SELECT count(*) n FROM rack_change_steps WHERE change_id=? AND revision=? AND state='done'",
                    (change_id, row["revision"]),
                ).fetchone()["n"]
                if done_count == 0:
                    # 没有任何步骤落地，直接释放锁定余量并结束为已回退。
                    self._release_reservations(change_id, row["revision"])
                    new_state = "rolled_back"
            self._set_state(change_id, row["revision"], new_state, fail_reason)
            self._audit(
                "rack_change",
                change_id,
                "rack_change.step_failed",
                actor_id,
                {"revision": row["revision"], "sequence": sequence, "mode": mode, "reason": reason.strip()},
            )
        return self.rack_change(change_id, row["revision"])

    def record_rollback_step(self, actor_id: str, change_id: str, sequence: int, note: str) -> dict[str, Any]:
        self._require(actor_id, "rack_change.execute")
        with transaction(self.connection, immediate=True):
            row = self._revision_row(change_id, None)
            if row["state"] != "rolling_back":
                raise InvalidState("变更不处于回退中")
            done_steps = self.connection.execute(
                "SELECT * FROM rack_change_steps WHERE change_id=? AND revision=? AND state='done' ORDER BY sequence",
                (change_id, row["revision"]),
            ).fetchall()
            target = next((item for item in done_steps if item["sequence"] == sequence), None)
            if target is None:
                raise InvalidState("该步骤不是待回退的已完成步骤")
            highest = max(item["sequence"] for item in done_steps)
            if sequence != highest:
                raise InvalidState("必须按逆序回退，先回退最近完成的步骤")
            self.connection.execute(
                "UPDATE rack_change_steps SET state='rolled_back',receipt_note=? WHERE change_id=? AND revision=? AND sequence=?",
                (f"回退：{note}", change_id, row["revision"], sequence),
            )
            remaining = self.connection.execute(
                "SELECT count(*) n FROM rack_change_steps WHERE change_id=? AND revision=? AND state='done'",
                (change_id, row["revision"]),
            ).fetchone()["n"]
            rolled_back = remaining == 0
            if rolled_back:
                self._release_reservations(change_id, row["revision"])
                self._set_state(change_id, row["revision"], "rolled_back")
            self._audit(
                "rack_change",
                change_id,
                "rack_change.step_rolled_back",
                actor_id,
                {"revision": row["revision"], "sequence": sequence, "rolled_back": rolled_back},
            )
        return self.rack_change(change_id, row["revision"])

    def resolve_manual_takeover(
        self,
        actor_id: str,
        change_id: str,
        outcome: str,
        note: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "rack_change.approve")
        if outcome not in {"complete", "release"}:
            raise ValidationFailed("outcome 必须是 complete 或 release")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("人工接管结论必须记录依据")
        with transaction(self.connection, immediate=True):
            row = self._revision_row(change_id, None)
            if row["state"] != "manual_takeover":
                raise InvalidState("变更不处于人工接管状态")
            if row["submitted_by"] == actor_id:
                raise Forbidden("申请人不能决定自己变更的人工接管结论")
            if outcome == "complete":
                self.connection.execute(
                    "UPDATE rack_change_steps SET state='manual' WHERE change_id=? AND revision=? AND state IN ('pending','failed')",
                    (change_id, row["revision"]),
                )
                self._consume_reservations(change_id, row["revision"])
                self._set_state(change_id, row["revision"], "completed", row["fail_reason"])
            else:
                self.connection.execute(
                    "UPDATE rack_change_steps SET state='rolled_back' WHERE change_id=? AND revision=? AND state='done'",
                    (change_id, row["revision"]),
                )
                self.connection.execute(
                    "UPDATE rack_change_steps SET state='skipped' WHERE change_id=? AND revision=? AND state='pending'",
                    (change_id, row["revision"]),
                )
                self._release_reservations(change_id, row["revision"])
                self._set_state(change_id, row["revision"], "rolled_back")
            self.connection.execute(
                "UPDATE rack_changes SET decision_by=?,decision_at=?,decision_basis=? WHERE change_id=? AND revision=?",
                (actor_id, self._now(), f"人工接管：{note.strip()}", change_id, row["revision"]),
            )
            self._audit(
                "rack_change",
                change_id,
                "rack_change.manual_resolved",
                actor_id,
                {"revision": row["revision"], "outcome": outcome, "note": note.strip()},
            )
        return self.rack_change(change_id, row["revision"])

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
