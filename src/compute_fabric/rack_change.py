"""机柜上架变更的输入契约与容量影响分析。

变更同时占用四类设施约束：电力（功耗曲线峰值 kW）、制冷（按散热等级换算 kW）、
承重（设备重量 kg）和网络端口（按端口类型计数）。本模块只做确定性的校验与计算，
所有数据库状态由 ``SupplyService`` 在事务内组装成快照后调用 :func:`build_impact`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed
from .models import decimal_value, identifier, positive_integer, required_text
from .planning import decimal_text, quantize_volume


TIMEPOINT = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

THERMAL_CLASSES = {"AIR_L1", "AIR_L2", "LIQUID_L3"}
THERMAL_COOLING_FACTOR = {
    "AIR_L1": Decimal("1.00"),
    "AIR_L2": Decimal("1.15"),
    "LIQUID_L3": Decimal("0.60"),
}

#: 约束维度 -> 标准计量单位。网络端口按 ``network_port:<类型>`` 细分。
STANDARD_CONSTRAINTS = {"power": "kW", "cooling": "kW", "weight": "kg"}
NETWORK_PREFIX = "network_port:"

#: 已锁定余量、位置不可再被其它变更使用的版本状态。
LOCKED_STATES = {"approved", "in_progress", "failed", "rolling_back", "manual_takeover"}
#: 位置被实际占用的状态（含施工完成后的永久占用）。
ACTIVE_LOCATION_STATES = LOCKED_STATES | {"completed"}
#: 仍会出现在冲突申请列表里的全部状态。
VISIBLE_STATES = ACTIVE_LOCATION_STATES | {"submitted"}


def unit_for(constraint_key: str) -> str:
    if constraint_key in STANDARD_CONSTRAINTS:
        return STANDARD_CONSTRAINTS[constraint_key]
    if constraint_key.startswith(NETWORK_PREFIX):
        return "ports"
    raise ValidationFailed(f"未知约束维度 {constraint_key}")


def normalize_constraint_key(raw: object) -> str:
    key = required_text(raw, "constraint_key", 64)
    if key in STANDARD_CONSTRAINTS:
        return key
    if key.startswith(NETWORK_PREFIX):
        kind = identifier(key[len(NETWORK_PREFIX):], "network_port 类型")
        return NETWORK_PREFIX + kind
    raise ValidationFailed("constraint_key 必须是 power、cooling、weight 或 network_port:<类型>")


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    model: str
    weight_kg: Decimal
    rated_power_kw: Decimal
    quantity: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "weight_kg": decimal_text(self.weight_kg),
            "rated_power_kw": decimal_text(self.rated_power_kw),
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True)
class RackLocation:
    rack_id: str
    u_start: int
    u_size: int

    def as_dict(self) -> dict[str, Any]:
        return {"rack_id": self.rack_id, "u_start": self.u_start, "u_size": self.u_size}


@dataclass(frozen=True, slots=True)
class PowerPoint:
    at: str
    kw: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "kw": decimal_text(self.kw)}


@dataclass(frozen=True, slots=True)
class PortDemand:
    kind: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "count": self.count}


@dataclass(frozen=True, slots=True)
class ImplementationStep:
    sequence: int
    name: str
    rollback_action: str

    def as_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "name": self.name, "rollback_action": self.rollback_action}


@dataclass(frozen=True, slots=True)
class RackChangeDraft:
    change_id: str
    facility_id: str
    title: str
    bom_version: str
    devices: tuple[Device, ...]
    locations: tuple[RackLocation, ...]
    power_curve: tuple[PowerPoint, ...]
    thermal_class: str
    port_demands: tuple[PortDemand, ...]
    window_starts_at: str
    window_ends_at: str
    rollback_summary: str
    steps: tuple[ImplementationStep, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RackChangeDraft":
        devices_raw = raw.get("devices")
        if not isinstance(devices_raw, list) or not devices_raw:
            raise ValidationFailed("设备清单不能为空")
        devices: list[Device] = []
        for index, item in enumerate(devices_raw, start=1):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"devices[{index}] 必须是对象")
            quantity = item.get("quantity", 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValidationFailed(f"devices[{index}].quantity 必须是正整数")
            devices.append(
                Device(
                    device_id=identifier(item.get("device_id"), f"devices[{index}].device_id"),
                    model=required_text(item.get("model"), f"devices[{index}].model", 64),
                    weight_kg=decimal_value(item.get("weight_kg"), f"devices[{index}].weight_kg", minimum=Decimal("0")),
                    rated_power_kw=decimal_value(
                        item.get("rated_power_kw"), f"devices[{index}].rated_power_kw", minimum=Decimal("0")
                    ),
                    quantity=quantity,
                )
            )
        locations_raw = raw.get("rack_locations")
        if not isinstance(locations_raw, list) or not locations_raw:
            raise ValidationFailed("机柜位置不能为空")
        locations: list[RackLocation] = []
        for index, item in enumerate(locations_raw, start=1):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"rack_locations[{index}] 必须是对象")
            u_start = item.get("u_start")
            u_size = item.get("u_size")
            if isinstance(u_start, bool) or not isinstance(u_start, int) or u_start < 1:
                raise ValidationFailed(f"rack_locations[{index}].u_start 必须是正整数")
            if isinstance(u_size, bool) or not isinstance(u_size, int) or u_size < 1:
                raise ValidationFailed(f"rack_locations[{index}].u_size 必须是正整数")
            if u_start + u_size - 1 > 52:
                raise ValidationFailed(f"rack_locations[{index}] 超出机柜 U 位上限")
            locations.append(
                RackLocation(
                    rack_id=identifier(item.get("rack_id"), f"rack_locations[{index}].rack_id"),
                    u_start=u_start,
                    u_size=u_size,
                )
            )
        _ensure_locations_do_not_overlap(locations)
        curve_raw = raw.get("power_curve")
        if not isinstance(curve_raw, list) or not curve_raw:
            raise ValidationFailed("功耗曲线不能为空")
        curve: list[PowerPoint] = []
        seen: set[str] = set()
        for index, item in enumerate(curve_raw, start=1):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"power_curve[{index}] 必须是对象")
            at = required_text(item.get("at"), f"power_curve[{index}].at", 5)
            if not TIMEPOINT.fullmatch(at):
                raise ValidationFailed(f"power_curve[{index}].at 必须是 HH:MM")
            if at in seen:
                raise ValidationFailed(f"功耗曲线 {at} 重复")
            seen.add(at)
            curve.append(
                PowerPoint(
                    at=at,
                    kw=decimal_value(item.get("kw"), f"power_curve[{index}].kw", minimum=Decimal("0")),
                )
            )
        if not any(point.kw > 0 for point in curve):
            raise ValidationFailed("功耗曲线至少包含一个正功率点")
        thermal_class = required_text(raw.get("thermal_class"), "thermal_class", 16).upper()
        if thermal_class not in THERMAL_CLASSES:
            raise ValidationFailed("thermal_class 必须是 AIR_L1、AIR_L2 或 LIQUID_L3")
        ports_raw = raw.get("port_demands", [])
        if not isinstance(ports_raw, list):
            raise ValidationFailed("port_demands 必须是数组")
        ports: list[PortDemand] = []
        for index, item in enumerate(ports_raw, start=1):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"port_demands[{index}] 必须是对象")
            ports.append(
                PortDemand(
                    kind=identifier(item.get("kind"), f"port_demands[{index}].kind"),
                    count=positive_integer(item.get("count"), f"port_demands[{index}].count"),
                )
            )
        try:
            start = parse_utc(required_text(raw.get("window_starts_at"), "window_starts_at", 40), "window_starts_at")
            end = parse_utc(required_text(raw.get("window_ends_at"), "window_ends_at", 40), "window_ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("施工窗口结束时间必须晚于开始时间")
        steps_raw = raw.get("implementation_steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValidationFailed("实施步骤不能为空")
        steps: list[ImplementationStep] = []
        for index, item in enumerate(steps_raw, start=1):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"implementation_steps[{index}] 必须是对象")
            steps.append(
                ImplementationStep(
                    sequence=index,
                    name=required_text(item.get("name"), f"implementation_steps[{index}].name", 128),
                    rollback_action=required_text(
                        item.get("rollback_action"), f"implementation_steps[{index}].rollback_action", 256
                    ),
                )
            )
        return cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            title=required_text(raw.get("title"), "title", 128),
            bom_version=identifier(raw.get("bom_version"), "bom_version"),
            devices=tuple(devices),
            locations=tuple(locations),
            power_curve=tuple(sorted(curve, key=lambda point: point.at)),
            thermal_class=thermal_class,
            port_demands=tuple(ports),
            window_starts_at=start.isoformat().replace("+00:00", "Z"),
            window_ends_at=end.isoformat().replace("+00:00", "Z"),
            rollback_summary=required_text(raw.get("rollback_summary"), "rollback_summary", 512),
            steps=tuple(steps),
        )

    def content(self) -> dict[str, Any]:
        """可重复序列化的工程内容，用于内容哈希与修订链比对。"""
        return {
            "title": self.title,
            "bom_version": self.bom_version,
            "devices": [device.as_dict() for device in sorted(self.devices, key=lambda item: item.device_id)],
            "rack_locations": [
                location.as_dict() for location in sorted(self.locations, key=lambda item: (item.rack_id, item.u_start))
            ],
            "power_curve": [point.as_dict() for point in self.power_curve],
            "thermal_class": self.thermal_class,
            "port_demands": [port.as_dict() for port in sorted(self.port_demands, key=lambda item: item.kind)],
            "window": {"starts_at": self.window_starts_at, "ends_at": self.window_ends_at},
            "rollback_summary": self.rollback_summary,
            "implementation_steps": [step.as_dict() for step in self.steps],
        }


def _ensure_locations_do_not_overlap(locations: Sequence[RackLocation]) -> None:
    by_rack: dict[str, list[RackLocation]] = {}
    for location in locations:
        by_rack.setdefault(location.rack_id, []).append(location)
    for rack_id, items in by_rack.items():
        ordered = sorted(items, key=lambda item: item.u_start)
        for earlier, later in zip(ordered, ordered[1:]):
            if later.u_start < earlier.u_start + earlier.u_size:
                raise ValidationFailed(f"机柜 {rack_id} 内部 U 位区间重叠")


def peak_power_kw(draft: RackChangeDraft) -> Decimal:
    return max(point.kw for point in draft.power_curve)


def required_demands(draft: RackChangeDraft) -> dict[str, Decimal]:
    """汇总本变更对每个约束维度的需求量。"""
    peak = peak_power_kw(draft)
    demands = {
        "power": quantize_volume(peak),
        "cooling": quantize_volume(peak * THERMAL_COOLING_FACTOR[draft.thermal_class]),
        "weight": quantize_volume(
            sum((device.weight_kg * device.quantity for device in draft.devices), Decimal("0"))
        ),
    }
    port_totals: dict[str, int] = {}
    for port in draft.port_demands:
        port_totals[port.kind] = port_totals.get(port.kind, 0) + port.count
    for kind, count in sorted(port_totals.items()):
        demands[NETWORK_PREFIX + kind] = Decimal(count)
    return dict(sorted(demands.items()))


def _windows_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_start = left.get("window_starts_at", left.get("starts_at"))
    left_end = left.get("window_ends_at", left.get("ends_at"))
    right_start = right.get("window_starts_at", right.get("starts_at"))
    right_end = right.get("window_ends_at", right.get("ends_at"))
    return not (left_end <= right_start or right_end <= left_start)


def _location_overlaps(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    overlaps = []
    for mine in left:
        for theirs in right:
            if mine["rack_id"] != theirs["rack_id"]:
                continue
            if mine["u_start"] < theirs["u_start"] + theirs["u_size"] and theirs["u_start"] < mine["u_start"] + mine["u_size"]:
                overlaps.append(
                    {
                        "rack_id": mine["rack_id"],
                        "u_start": max(mine["u_start"], theirs["u_start"]),
                        "u_end": min(mine["u_start"] + mine["u_size"], theirs["u_start"] + theirs["u_size"]) - 1,
                    }
                )
    return overlaps


def build_impact(
    snapshot: Mapping[str, Any],
    change_id: str,
    demands: Mapping[str, Decimal],
    locations: Sequence[Mapping[str, Any]],
    window: Mapping[str, str],
) -> dict[str, Any]:
    """基于设施快照评估上架变更的影响。

    ``snapshot["others"]`` 中的每条申请都带有状态、窗口、需求与位置；
    已锁定/完成的申请决定硬性冲突，待批申请只作为冲突申请展示。
    """
    catalog = {item["constraint_key"]: item for item in snapshot["constraints"]}
    others = sorted(
        (item for item in snapshot["others"] if item["change_id"] != change_id),
        key=lambda item: (item["change_id"], item["revision"]),
    )
    # 其它待批申请在每个约束维度上的合计需求（尚未锁定，仅供预判互相挤占）。
    pending_demand: dict[str, Decimal] = {}
    for other in others:
        if other["state"] != "submitted":
            continue
        for item in other["demands"]:
            key = item["constraint_key"]
            pending_demand[key] = pending_demand.get(key, Decimal("0")) + Decimal(str(item["required_value"]))
    constraint_rows: list[dict[str, Any]] = []
    hard_conflicts: list[dict[str, Any]] = []
    for key in sorted(demands):
        required = quantize_volume(demands[key])
        row = catalog.get(key)
        if row is None:
            constraint_rows.append(
                {
                    "constraint_key": key,
                    "unit": unit_for(key),
                    "capacity": None,
                    "used": None,
                    "reserved": None,
                    "available": None,
                    "required": decimal_text(required),
                    "remaining_after_change": None,
                    "feasible": False,
                }
            )
            hard_conflicts.append(
                {
                    "type": "constraint_undefined",
                    "constraint_key": key,
                    "required": decimal_text(required),
                    "message": "设施目录未配置该约束容量",
                }
            )
            continue
        capacity = Decimal(str(row["capacity"]))
        used = Decimal(str(row["used"]))
        reserved = Decimal(str(row["reserved"]))
        available = quantize_volume(capacity - used - reserved)
        remaining = quantize_volume(available - required)
        others_pending = quantize_volume(pending_demand.get(key, Decimal("0")))
        remaining_with_pending = quantize_volume(remaining - others_pending)
        feasible = remaining >= 0
        constraint_rows.append(
            {
                "constraint_key": key,
                "unit": row["unit"],
                "capacity": decimal_text(capacity),
                "used": decimal_text(used),
                "reserved": decimal_text(reserved),
                "available": decimal_text(available),
                "required": decimal_text(required),
                "remaining_after_change": decimal_text(remaining),
                "pending_demand_by_others": decimal_text(others_pending),
                "remaining_if_all_pending_approved": decimal_text(remaining_with_pending),
                "feasible": feasible,
            }
        )
        if not feasible:
            hard_conflicts.append(
                {
                    "type": "capacity_overcommit",
                    "constraint_key": key,
                    "available": decimal_text(available),
                    "required": decimal_text(required),
                    "shortfall": decimal_text(-remaining),
                }
            )

    for other in others:
        if other["state"] not in ACTIVE_LOCATION_STATES:
            continue
        for overlap in _location_overlaps(locations, other["locations"]):
            hard_conflicts.append(
                {
                    "type": "location_overlap",
                    "constraint_key": "weight",
                    "rack_id": overlap["rack_id"],
                    "u_start": overlap["u_start"],
                    "u_end": overlap["u_end"],
                    "other_change_id": other["change_id"],
                    "other_revision": other["revision"],
                    "other_state": other["state"],
                }
            )

    competing: list[dict[str, Any]] = []
    for other in others:
        reasons: list[str] = []
        shared_keys = sorted(set(demands) & {item["constraint_key"] for item in other["demands"]})
        if shared_keys:
            reasons.append("capacity_share")
        window_overlaps = _windows_overlap(window, other)
        if window_overlaps:
            reasons.append("window_overlap")
        location_overlap = _location_overlaps(locations, other["locations"])
        if location_overlap:
            reasons.append("location_overlap")
        if not reasons:
            continue
        competing.append(
            {
                "change_id": other["change_id"],
                "revision": other["revision"],
                "state": other["state"],
                "window_starts_at": other["window_starts_at"],
                "window_ends_at": other["window_ends_at"],
                "window_overlaps": window_overlaps,
                "reasons": reasons,
                "shared_constraints": shared_keys,
                "location_overlaps": location_overlap,
                "demands": [dict(item) for item in other["demands"]],
            }
        )

    return {
        "facility_id": snapshot["facility_id"],
        "feasible": not hard_conflicts,
        "constraints": constraint_rows,
        "conflicts": hard_conflicts,
        "competing_requests": competing,
    }
