"""机柜上架变更的设备清单契约与设施容量影响计算。

约束维度：站点电力、制冷、楼层承重、机柜 U 位、机柜承重/受电、网络端口
池与设备散热等级。所有数值计算使用 Decimal，输出为可直接 JSON 序列化的
原始类型（Decimal 统一转字符串）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed
from .models import decimal_value, identifier, positive_integer, required_text
from .planning import canonical_json, decimal_text, digest, quantize_volume


HEAT_CLASSES = {"AIR": 1, "ENHANCED_AIR": 2, "LIQUID": 3}

# 已批准并锁定余量、尚未结案的状态
LOCKED_STATES = {"reserved", "in_progress", "failed", "rollback_in_progress", "manual_takeover"}
# 等待审批或退回修订的状态
PENDING_STATES = {"pending_approval", "returned"}
# 终态：不再占用容量、也不再阻塞快照重建
TERMINAL_STATES = {"completed", "rolled_back", "rejected", "cancelled", "superseded"}

POWER = "power_kw"
COOLING = "cooling_kw"
WEIGHT = "weight_kg"


def _bounded_int(value: object, field: str, *, minimum: int = 1, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationFailed(f"{field} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def _heat(value: object, field: str = "heat_class") -> str:
    result = required_text(value, field, 24).upper()
    if result not in HEAT_CLASSES:
        raise ValidationFailed(f"{field} 必须是 AIR、ENHANCED_AIR 或 LIQUID")
    return result


@dataclass(frozen=True, slots=True)
class URange:
    """机柜内一段已占用 U 位（快照基线设备）。"""

    u_start: int
    u_size: int
    label: str
    weight_kg: Decimal = Decimal("0")
    peak_power_kw: Decimal = Decimal("0")
    heat_class: str = "AIR"

    @property
    def u_end(self) -> int:
        return self.u_start + self.u_size - 1

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "URange":
        return cls(
            u_start=_bounded_int(raw.get("u_start"), "baseline_u.u_start", maximum=100),
            u_size=_bounded_int(raw.get("u_size"), "baseline_u.u_size", maximum=100),
            label=required_text(raw.get("label"), "baseline_u.label", 128),
            weight_kg=decimal_value(raw.get("weight_kg", "0"), "baseline_u.weight_kg", minimum=Decimal("0")),
            peak_power_kw=decimal_value(
                raw.get("peak_power_kw", "0"), "baseline_u.peak_power_kw", minimum=Decimal("0")
            ),
            heat_class=_heat(raw.get("heat_class", "AIR"), "baseline_u.heat_class"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "u_start": self.u_start,
            "u_size": self.u_size,
            "u_end": self.u_end,
            "label": self.label,
            "weight_kg": decimal_text(self.weight_kg),
            "peak_power_kw": decimal_text(self.peak_power_kw),
            "heat_class": self.heat_class,
        }


@dataclass(frozen=True, slots=True)
class RackSpec:
    rack_id: str
    u_size: int
    weight_limit_kg: Decimal
    power_limit_kw: Decimal
    max_heat_class: str
    baseline_u: tuple[URange, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RackSpec":
        occupied = raw.get("baseline_u", [])
        if not isinstance(occupied, list):
            raise ValidationFailed("racks.baseline_u 必须是数组")
        ranges = tuple(URange.from_dict(item) for item in occupied)
        return cls(
            rack_id=identifier(raw.get("rack_id"), "racks.rack_id"),
            u_size=_bounded_int(raw.get("u_size"), "racks.u_size", maximum=100),
            weight_limit_kg=decimal_value(
                raw.get("weight_limit_kg"), "racks.weight_limit_kg", minimum=Decimal("0")
            ),
            power_limit_kw=decimal_value(
                raw.get("power_limit_kw"), "racks.power_limit_kw", minimum=Decimal("0")
            ),
            max_heat_class=_heat(raw.get("max_heat_class"), "racks.max_heat_class"),
            baseline_u=ranges,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rack_id": self.rack_id,
            "u_size": self.u_size,
            "weight_limit_kg": decimal_text(self.weight_limit_kg),
            "power_limit_kw": decimal_text(self.power_limit_kw),
            "max_heat_class": self.max_heat_class,
            "baseline_u": [item.as_dict() for item in self.baseline_u],
        }


@dataclass(frozen=True, slots=True)
class PortPool:
    port_type: str
    total: int
    baseline_used: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PortPool":
        total = positive_integer(raw.get("total"), "ports.total")
        used = raw.get("baseline_used", 0)
        if isinstance(used, bool) or not isinstance(used, int) or not 0 <= used <= total:
            raise ValidationFailed("ports.baseline_used 必须是 0 到 total 的整数")
        return cls(
            port_type=identifier(raw.get("port_type"), "ports.port_type"),
            total=total,
            baseline_used=used,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"port_type": self.port_type, "total": self.total, "baseline_used": self.baseline_used}


@dataclass(frozen=True, slots=True)
class FacilitySnapshot:
    """站点基础设施某一版本的容量快照。"""

    site_id: str
    name: str
    power_limit_kw: Decimal
    cooling_limit_kw: Decimal
    weight_limit_kg: Decimal
    baseline_power_kw: Decimal
    baseline_cooling_kw: Decimal
    baseline_weight_kg: Decimal
    racks: tuple[RackSpec, ...]
    port_pools: tuple[PortPool, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FacilitySnapshot":
        racks_value = raw.get("racks", [])
        ports_value = raw.get("port_pools", [])
        if not isinstance(racks_value, list) or not racks_value:
            raise ValidationFailed("racks 至少包含一个机柜")
        if not isinstance(ports_value, list) or not ports_value:
            raise ValidationFailed("port_pools 至少包含一种端口池")
        racks = tuple(RackSpec.from_dict(item) for item in racks_value)
        ports = tuple(PortPool.from_dict(item) for item in ports_value)
        snapshot = cls(
            site_id=identifier(raw.get("site_id"), "site_id"),
            name=required_text(raw.get("name"), "name"),
            power_limit_kw=decimal_value(raw.get("power_limit_kw"), POWER, minimum=Decimal("0")),
            cooling_limit_kw=decimal_value(raw.get("cooling_limit_kw"), COOLING, minimum=Decimal("0")),
            weight_limit_kg=decimal_value(raw.get("weight_limit_kg"), WEIGHT, minimum=Decimal("0")),
            baseline_power_kw=decimal_value(
                raw.get("baseline_power_kw", "0"), "baseline_power_kw", minimum=Decimal("0")
            ),
            baseline_cooling_kw=decimal_value(
                raw.get("baseline_cooling_kw", "0"), "baseline_cooling_kw", minimum=Decimal("0")
            ),
            baseline_weight_kg=decimal_value(
                raw.get("baseline_weight_kg", "0"), "baseline_weight_kg", minimum=Decimal("0")
            ),
            racks=racks,
            port_pools=ports,
        )
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        if self.baseline_power_kw > self.power_limit_kw:
            raise ValidationFailed("baseline_power_kw 不能超过 power_limit_kw")
        if self.baseline_cooling_kw > self.cooling_limit_kw:
            raise ValidationFailed("baseline_cooling_kw 不能超过 cooling_limit_kw")
        if self.baseline_weight_kg > self.weight_limit_kg:
            raise ValidationFailed("baseline_weight_kg 不能超过 weight_limit_kg")
        rack_ids: set[str] = set()
        for rack in self.racks:
            if rack.rack_id in rack_ids:
                raise ValidationFailed(f"机柜 {rack.rack_id} 重复")
            rack_ids.add(rack.rack_id)
            if any(item.u_end > rack.u_size for item in rack.baseline_u):
                raise ValidationFailed(f"机柜 {rack.rack_id} 基线 U 位超出机柜尺寸")
            if _ranges_overlap([(item.u_start, item.u_size) for item in rack.baseline_u]):
                raise ValidationFailed(f"机柜 {rack.rack_id} 基线 U 位互相重叠")
            if HEAT_CLASSES[rack.max_heat_class] < max(
                (HEAT_CLASSES[item.heat_class] for item in rack.baseline_u), default=1
            ):
                raise ValidationFailed(f"机柜 {rack.rack_id} 存在超出其散热等级的基线设备")
        port_types: set[str] = set()
        for pool in self.port_pools:
            if pool.port_type in port_types:
                raise ValidationFailed(f"端口类型 {pool.port_type} 重复")
            port_types.add(pool.port_type)

    def rack(self, rack_id: str) -> RackSpec | None:
        return next((item for item in self.racks if item.rack_id == rack_id), None)

    def port_types(self) -> set[str]:
        return {pool.port_type for pool in self.port_pools}

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "power_limit_kw": decimal_text(self.power_limit_kw),
            "cooling_limit_kw": decimal_text(self.cooling_limit_kw),
            "weight_limit_kg": decimal_text(self.weight_limit_kg),
            "baseline_power_kw": decimal_text(self.baseline_power_kw),
            "baseline_cooling_kw": decimal_text(self.baseline_cooling_kw),
            "baseline_weight_kg": decimal_text(self.baseline_weight_kg),
            "racks": [item.as_dict() for item in self.racks],
            "port_pools": [item.as_dict() for item in self.port_pools],
        }

    @property
    def content_sha256(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class DeviceAsset:
    """上架申请中的单台设备。"""

    asset_id: str
    model: str
    rack_id: str
    u_start: int
    u_size: int
    power_curve: tuple[Decimal, ...]
    cooling_load_kw: Decimal
    weight_kg: Decimal
    heat_class: str
    ports: Mapping[str, int]

    @property
    def u_end(self) -> int:
        return self.u_start + self.u_size - 1

    @property
    def peak_power_kw(self) -> Decimal:
        return max(self.power_curve, default=Decimal("0"))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeviceAsset":
        curve = raw.get("power_curve")
        if not isinstance(curve, list) or len(curve) != 24:
            raise ValidationFailed("devices.power_curve 必须是 24 个整点功耗值（kW）")
        ports = raw.get("ports", {})
        if not isinstance(ports, Mapping):
            raise ValidationFailed("devices.ports 必须是对象")
        parsed_ports = {
            identifier(key, "devices.ports 键"): positive_integer(value, f"devices.ports.{key}")
            for key, value in ports.items()
        }
        return cls(
            asset_id=identifier(raw.get("asset_id"), "devices.asset_id"),
            model=required_text(raw.get("model"), "devices.model", 128),
            rack_id=identifier(raw.get("rack_id"), "devices.rack_id"),
            u_start=_bounded_int(raw.get("u_start"), "devices.u_start", maximum=100),
            u_size=_bounded_int(raw.get("u_size"), "devices.u_size", maximum=100),
            power_curve=tuple(
                decimal_value(value, "devices.power_curve", minimum=Decimal("0")) for value in curve
            ),
            cooling_load_kw=decimal_value(
                raw.get("cooling_load_kw"), "devices.cooling_load_kw", minimum=Decimal("0")
            ),
            weight_kg=decimal_value(raw.get("weight_kg"), "devices.weight_kg", minimum=Decimal("0")),
            heat_class=_heat(raw.get("heat_class"), "devices.heat_class"),
            ports=parsed_ports,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "model": self.model,
            "rack_id": self.rack_id,
            "u_start": self.u_start,
            "u_size": self.u_size,
            "u_end": self.u_end,
            "power_curve": [decimal_text(value) for value in self.power_curve],
            "peak_power_kw": decimal_text(self.peak_power_kw),
            "cooling_load_kw": decimal_text(self.cooling_load_kw),
            "weight_kg": decimal_text(self.weight_kg),
            "heat_class": self.heat_class,
            "ports": dict(self.ports),
        }


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    code: str
    title: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionStep":
        return cls(
            code=identifier(raw.get("code"), "steps.code"),
            title=required_text(raw.get("title"), "steps.title", 128),
        )

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "title": self.title}


@dataclass(frozen=True, slots=True)
class RollbackStep:
    code: str
    title: str
    instruction: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RollbackStep":
        return cls(
            code=identifier(raw.get("code"), "rollback_steps.code"),
            title=required_text(raw.get("title"), "rollback_steps.title", 128),
            instruction=required_text(raw.get("instruction"), "rollback_steps.instruction", 512),
        )

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "title": self.title, "instruction": self.instruction}


@dataclass(frozen=True, slots=True)
class RackChangeRequest:
    change_id: str
    title: str
    site_id: str
    device_list_version: str
    devices: tuple[DeviceAsset, ...]
    window_starts_at: str
    window_ends_at: str
    execution_steps: tuple[ExecutionStep, ...]
    rollback_trigger_notes: str
    rollback_steps: tuple[RollbackStep, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RackChangeRequest":
        devices_value = raw.get("devices")
        if not isinstance(devices_value, list) or not devices_value:
            raise ValidationFailed("devices 至少包含一台设备")
        steps_value = raw.get("execution_steps")
        if not isinstance(steps_value, list) or len(steps_value) < 2:
            raise ValidationFailed("execution_steps 至少包含两个施工步骤")
        plan = raw.get("rollback_plan")
        if not isinstance(plan, Mapping):
            raise ValidationFailed("rollback_plan 必须是对象")
        rollback_steps_value = plan.get("steps")
        if not isinstance(rollback_steps_value, list) or not rollback_steps_value:
            raise ValidationFailed("rollback_plan.steps 至少包含一个回退步骤")
        try:
            start = parse_utc(required_text(raw.get("window_starts_at"), "window_starts_at", 40), "window_starts_at")
            end = parse_utc(required_text(raw.get("window_ends_at"), "window_ends_at", 40), "window_ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("window_ends_at 必须晚于 window_starts_at")
        execution_steps = tuple(ExecutionStep.from_dict(item) for item in steps_value)
        rollback_steps = tuple(RollbackStep.from_dict(item) for item in rollback_steps_value)
        request = cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            title=required_text(raw.get("title"), "title"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            device_list_version=identifier(raw.get("device_list_version"), "device_list_version"),
            devices=tuple(DeviceAsset.from_dict(item) for item in devices_value),
            window_starts_at=start.isoformat().replace("+00:00", "Z"),
            window_ends_at=end.isoformat().replace("+00:00", "Z"),
            execution_steps=execution_steps,
            rollback_trigger_notes=required_text(plan.get("trigger_notes"), "rollback_plan.trigger_notes", 512),
            rollback_steps=rollback_steps,
        )
        request.validate()
        return request

    def validate(self) -> None:
        asset_ids: set[str] = set()
        codes: set[str] = set()
        for step in self.execution_steps:
            if step.code in codes:
                raise ValidationFailed(f"施工步骤 {step.code} 重复")
            codes.add(step.code)
        rollback_codes: set[str] = set()
        for step in self.rollback_steps:
            if step.code in rollback_codes:
                raise ValidationFailed(f"回退步骤 {step.code} 重复")
            rollback_codes.add(step.code)
        placements: dict[str, list[tuple[int, int, str]]] = {}
        for device in self.devices:
            if device.asset_id in asset_ids:
                raise ValidationFailed(f"设备 {device.asset_id} 重复")
            asset_ids.add(device.asset_id)
            placements.setdefault(device.rack_id, []).append(
                (device.u_start, device.u_size, device.asset_id)
            )
        for rack_id, ranges in placements.items():
            if _ranges_overlap([(start, size) for start, size, _ in ranges]):
                raise ValidationFailed(f"申请在机柜 {rack_id} 内的 U 位互相重叠")

    def rollback_plan(self) -> dict[str, Any]:
        return {
            "trigger_notes": self.rollback_trigger_notes,
            "steps": [step.as_dict() for step in self.rollback_steps],
        }


def _ranges_overlap(ranges: Sequence[tuple[int, int]]) -> bool:
    ordered = sorted(ranges)
    return any(left[0] + left[1] > right[0] for left, right in zip(ordered, ordered[1:]))


def ranges_overlap(start_a: int, size_a: int, start_b: int, size_b: int) -> bool:
    return start_a < start_b + size_b and start_b < start_a + size_a


def window_overlaps(
    start_a: str, end_a: str, start_b: str, end_b: str
) -> bool:
    return start_a < end_b and start_b < end_a


def change_totals(devices: Iterable[DeviceAsset]) -> dict[str, Any]:
    power = Decimal("0")
    cooling = Decimal("0")
    weight = Decimal("0")
    ports: dict[str, int] = {}
    for device in devices:
        power += device.peak_power_kw
        cooling += device.cooling_load_kw
        weight += device.weight_kg
        for port_type, count in device.ports.items():
            ports[port_type] = ports.get(port_type, 0) + count
    return {
        POWER: decimal_text(quantize_volume(power)),
        COOLING: decimal_text(quantize_volume(cooling)),
        WEIGHT: decimal_text(quantize_volume(weight)),
        "ports": dict(sorted(ports.items())),
    }


def _constraint(
    capacity: Decimal,
    baseline: Decimal,
    requested: Decimal,
    committed: list[tuple[str, Decimal]],
    locked: list[tuple[str, Decimal]],
) -> dict[str, Any]:
    committed_total = sum((amount for _, amount in committed), Decimal("0"))
    locked_total = sum((amount for _, amount in locked), Decimal("0"))
    remaining = capacity - baseline - committed_total - locked_total
    remaining_after = remaining - requested
    return {
        "capacity": decimal_text(capacity),
        "baseline_used": decimal_text(baseline),
        "consumed_by_completed": decimal_text(quantize_volume(committed_total)),
        "locked_by_approved": decimal_text(quantize_volume(locked_total)),
        "requested": decimal_text(quantize_volume(requested)),
        "remaining": decimal_text(quantize_volume(remaining)),
        "remaining_after_request": decimal_text(quantize_volume(remaining_after)),
        "feasible": remaining_after >= 0,
        "locked_contributors": [
            {"change_id": change_id, "amount": decimal_text(quantize_volume(amount))}
            for change_id, amount in locked
        ],
        "completed_contributors": [
            {"change_id": change_id, "amount": decimal_text(quantize_volume(amount))}
            for change_id, amount in committed
        ],
    }


def _port_constraint(total: int, baseline: int, requested: int, committed: int, locked: int) -> dict[str, Any]:
    remaining = total - baseline - committed - locked
    remaining_after = remaining - requested
    return {
        "capacity": total,
        "baseline_used": baseline,
        "consumed_by_completed": committed,
        "locked_by_approved": locked,
        "requested": requested,
        "remaining": remaining,
        "remaining_after_request": remaining_after,
        "feasible": remaining_after >= 0,
    }


def _peer_devices(peer: Mapping[str, Any]) -> list[DeviceAsset]:
    return [DeviceAsset.from_dict(item) for item in json.loads(peer["devices_json"])]

def build_impact(
    snapshot: FacilitySnapshot,
    devices: Sequence[DeviceAsset],
    peers: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """基于设施快照与各变更最新修订生成上架影响分析。

    peers 为同站点其他变更的最新修订行（sqlite Row 或映射），需包含
    state/window/totals_json/devices_json/change_id/revision 字段。
    已完成修订计入消耗，已批准未结案修订计入硬预留，待审批修订只列为
    竞争申请（不改变可行性结论）。
    """

    peer_list = list(peers)
    committed = [peer for peer in peer_list if peer["state"] == "completed"]
    locked = [peer for peer in peer_list if peer["state"] in LOCKED_STATES]
    pending = [peer for peer in peer_list if peer["state"] in PENDING_STATES]

    totals = change_totals(devices)
    requested_power = Decimal(totals[POWER])
    requested_cooling = Decimal(totals[COOLING])
    requested_weight = Decimal(totals[WEIGHT])

    def peer_totals(peer: Mapping[str, Any]) -> dict[str, Any]:
        return json.loads(peer["totals_json"])

    site_constraints = {
        POWER: _constraint(
            snapshot.power_limit_kw,
            snapshot.baseline_power_kw,
            requested_power,
            [(p["change_id"], Decimal(peer_totals(p)[POWER])) for p in committed],
            [(p["change_id"], Decimal(peer_totals(p)[POWER])) for p in locked],
        ),
        COOLING: _constraint(
            snapshot.cooling_limit_kw,
            snapshot.baseline_cooling_kw,
            requested_cooling,
            [(p["change_id"], Decimal(peer_totals(p)[COOLING])) for p in committed],
            [(p["change_id"], Decimal(peer_totals(p)[COOLING])) for p in locked],
        ),
        WEIGHT: _constraint(
            snapshot.weight_limit_kg,
            snapshot.baseline_weight_kg,
            requested_weight,
            [(p["change_id"], Decimal(peer_totals(p)[WEIGHT])) for p in committed],
            [(p["change_id"], Decimal(peer_totals(p)[WEIGHT])) for p in locked],
        ),
    }

    requested_ports = totals["ports"]
    port_constraints: dict[str, Any] = {}
    for pool in sorted(snapshot.port_pools, key=lambda item: item.port_type):
        committed_used = sum(int(peer_totals(p)["ports"].get(pool.port_type, 0)) for p in committed)
        locked_used = sum(int(peer_totals(p)["ports"].get(pool.port_type, 0)) for p in locked)
        port_constraints[pool.port_type] = _port_constraint(
            pool.total, pool.baseline_used, int(requested_ports.get(pool.port_type, 0)),
            committed_used, locked_used,
        )

    # 机柜维度：U 位、机柜承重、机柜受电、散热等级
    rack_nodes: dict[str, dict[str, Any]] = {}
    rack_feasible = True
    heat_violations: list[dict[str, str]] = []
    requested_by_rack: dict[str, list[DeviceAsset]] = {}
    for device in devices:
        requested_by_rack.setdefault(device.rack_id, []).append(device)

    for rack_id, rack_devices in requested_by_rack.items():
        rack = snapshot.rack(rack_id)
        if rack is None:
            continue  # 机柜不存在属于提交时的硬校验，不应进入影响分析
        occupied: list[dict[str, Any]] = [
            {**item.as_dict(), "source": "baseline", "change_id": None, "asset_id": item.label}
            for item in rack.baseline_u
        ]
        rack_committed_weight: list[tuple[str, Decimal]] = []
        rack_locked_weight: list[tuple[str, Decimal]] = []
        rack_committed_power: list[tuple[str, Decimal]] = []
        rack_locked_power: list[tuple[str, Decimal]] = []
        for peer in committed + locked:
            for device in _peer_devices(peer):
                if device.rack_id != rack_id:
                    continue
                occupied.append({
                    "u_start": device.u_start, "u_size": device.u_size, "u_end": device.u_end,
                    "label": device.asset_id, "weight_kg": decimal_text(device.weight_kg),
                    "peak_power_kw": decimal_text(device.peak_power_kw), "heat_class": device.heat_class,
                    "source": "completed" if peer in committed else "locked",
                    "change_id": peer["change_id"], "asset_id": device.asset_id,
                })
                bucket_w = rack_committed_weight if peer in committed else rack_locked_weight
                bucket_p = rack_committed_power if peer in committed else rack_locked_power
                bucket_w.append((peer["change_id"], device.weight_kg))
                bucket_p.append((peer["change_id"], device.peak_power_kw))

        overlaps: list[dict[str, str]] = []
        requested_u: list[dict[str, Any]] = []
        for device in rack_devices:
            requested_u.append({
                "asset_id": device.asset_id, "u_start": device.u_start,
                "u_size": device.u_size, "u_end": device.u_end,
            })
            for entry in occupied:
                if ranges_overlap(device.u_start, device.u_size, entry["u_start"], entry["u_size"]):
                    overlaps.append({
                        "asset_id": device.asset_id,
                        "rack_id": rack_id,
                        "u_start": str(device.u_start),
                        "u_size": str(device.u_size),
                        "conflicts_with": entry["asset_id"],
                        "source": entry["source"],
                        "change_id": entry["change_id"] or "",
                    })
            if HEAT_CLASSES[device.heat_class] > HEAT_CLASSES[rack.max_heat_class]:
                heat_violations.append({
                    "asset_id": device.asset_id,
                    "rack_id": rack_id,
                    "required_heat_class": device.heat_class,
                    "rack_max_heat_class": rack.max_heat_class,
                })

        request_weight = sum((device.weight_kg for device in rack_devices), Decimal("0"))
        request_power = sum((device.peak_power_kw for device in rack_devices), Decimal("0"))
        baseline_weight = sum((item.weight_kg for item in rack.baseline_u), Decimal("0"))
        baseline_power = sum((item.peak_power_kw for item in rack.baseline_u), Decimal("0"))
        weight_node = _constraint(
            rack.weight_limit_kg, baseline_weight, request_weight,
            rack_committed_weight, rack_locked_weight,
        )
        power_node = _constraint(
            rack.power_limit_kw, baseline_power, request_power,
            rack_committed_power, rack_locked_power,
        )
        node_feasible = weight_node["feasible"] and power_node["feasible"] and not overlaps
        rack_feasible = rack_feasible and node_feasible
        rack_nodes[rack_id] = {
            "rack_id": rack_id,
            "u_size": rack.u_size,
            "max_heat_class": rack.max_heat_class,
            "occupied_u": sorted(occupied, key=lambda item: (item["u_start"], item["label"])),
            "requested_u": sorted(requested_u, key=lambda item: item["u_start"]),
            "u_overlaps": overlaps,
            "rack_weight_kg": weight_node,
            "rack_power_kw": power_node,
            "feasible": node_feasible,
        }

    feasible = (
        all(node["feasible"] for node in site_constraints.values())
        and all(node["feasible"] for node in port_constraints.values())
        and rack_feasible
        and not heat_violations
    )

    return {
        "totals": totals,
        "site_constraints": site_constraints,
        "port_constraints": port_constraints,
        "rack_constraints": {key: rack_nodes[key] for key in sorted(rack_nodes)},
        "heat_class_violations": heat_violations,
        "feasible": feasible,
        "basis": (
            f"依据设施快照 {snapshot.site_id}（sha256={snapshot.content_sha256[:16]}…）："
            "余量=总量-基线已用-已完成变更消耗-已批准未结案变更预留，再扣本申请需求；"
            "待审批变更只列为竞争申请，不计入硬预留；机柜 U 位不得与基线设备、已完成或已锁定设备重叠。"
        ),
        "_peer_rows": list(locked + pending),
    }


def annotate_conflicts(
    impact: Mapping[str, Any],
    snapshot: FacilitySnapshot,
    devices: Sequence[DeviceAsset],
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """补充冲突申请明细：窗口重叠、机柜 U 位重叠、容量竞争约束。"""

    result = dict(impact)
    peer_rows = result.pop("_peer_rows", [])
    requested_ranges: dict[str, list[tuple[int, int, str]]] = {}
    for device in devices:
        requested_ranges.setdefault(device.rack_id, []).append(
            (device.u_start, device.u_size, device.asset_id)
        )
    conflicting: list[dict[str, Any]] = []
    for peer in peer_rows:
        reasons: list[str] = []
        locked = peer["state"] in LOCKED_STATES
        if window_overlaps(window_start, window_end, peer["window_starts_at"], peer["window_ends_at"]):
            reasons.append("window_overlap")
        overlap_assets: list[str] = []
        for device in _peer_devices(peer):
            for start, size, asset_id in requested_ranges.get(device.rack_id, []):
                if ranges_overlap(start, size, device.u_start, device.u_size):
                    overlap_assets.append(f"{asset_id}~{device.asset_id}@{device.rack_id}")
        if overlap_assets:
            reasons.append("rack_u_overlap")
        peer_totals = json.loads(peer["totals_json"])
        contested: list[str] = []
        # 竞争判定：站在本申请视角，若该申请也被批准，双方合计将超过当前剩余
        for key in (POWER, COOLING, WEIGHT):
            node = result["site_constraints"][key]
            other = Decimal(str(peer_totals[key]))
            if Decimal(str(node["remaining"])) - other < 0:
                contested.append(key)
        for port_type, node in result["port_constraints"].items():
            if node["remaining"] - int(peer_totals["ports"].get(port_type, 0)) < 0:
                contested.append(f"port:{port_type}")
        if contested:
            reasons.append("capacity_contention")
        conflicting.append({
            "change_id": peer["change_id"],
            "revision": peer["revision"],
            "state": peer["state"],
            "submitted_by": peer["submitted_by"],
            "window_starts_at": peer["window_starts_at"],
            "window_ends_at": peer["window_ends_at"],
            "window_overlap": "window_overlap" in reasons,
            "rack_u_overlaps": overlap_assets,
            "contested_constraints": contested,
            "hard_reservation": locked,
            "reasons": reasons,
        })
    conflicting.sort(key=lambda item: (item["change_id"], item["revision"]))
    result["conflicting_changes"] = conflicting
    return result


def validate_devices_against_snapshot(
    devices: Sequence[DeviceAsset], snapshot: FacilitySnapshot
) -> None:
    """提交前的硬性契约校验：机柜存在、物理尺寸、散热等级、端口类型与基线机位。"""

    port_types = snapshot.port_types()
    placements: dict[str, list[DeviceAsset]] = {}
    for device in devices:
        rack = snapshot.rack(device.rack_id)
        if rack is None:
            raise ValidationFailed(f"设备 {device.asset_id} 指定的机柜 {device.rack_id} 不存在")
        if device.u_end > rack.u_size:
            raise ValidationFailed(f"设备 {device.asset_id} 的 U 位超出机柜 {device.rack_id} 尺寸")
        if HEAT_CLASSES[device.heat_class] > HEAT_CLASSES[rack.max_heat_class]:
            raise ValidationFailed(
                f"设备 {device.asset_id} 散热等级 {device.heat_class} 超过机柜 {device.rack_id} 支持上限"
            )
        unknown = set(device.ports) - port_types
        if unknown:
            raise ValidationFailed(f"设备 {device.asset_id} 使用了未定义端口类型：{', '.join(sorted(unknown))}")
        for occupied in rack.baseline_u:
            if ranges_overlap(device.u_start, device.u_size, occupied.u_start, occupied.u_size):
                raise ValidationFailed(
                    f"设备 {device.asset_id} 的 U 位 {device.u_start}-{device.u_end} "
                    f"与机柜 {device.rack_id} 基线设备 {occupied.label} 重叠"
                )
        placements.setdefault(device.rack_id, []).append(device)
    for rack_id, rack_devices in placements.items():
        ordered = sorted(rack_devices, key=lambda item: item.u_start)
        for left, right in zip(ordered, ordered[1:]):
            if ranges_overlap(left.u_start, left.u_size, right.u_start, right.u_size):
                raise ValidationFailed(
                    f"机柜 {rack_id} 内设备 {left.asset_id} 与 {right.asset_id} 的 U 位重叠"
                )
