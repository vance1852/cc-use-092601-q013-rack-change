# 增加机柜上架变更与容量影响审批基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景、硬件稳定性准入以及**机柜上架变更与多约束容量影响审批**。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配、情景分析，以及机柜上架变更管理；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析和芯片准入流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 机柜上架变更管理

设施快照（`facility_snapshots`）版本化保存站点电力、制冷、楼层承重、机柜 U 位/承重/受电/散热等级和网络端口池；存在未结案变更时禁止重建快照。机柜上架变更保存设备清单版本、逐台设备的机柜位置、24 点功耗曲线、散热等级与端口需求，以及整体提交的施工窗口、施工步骤和回退方案。

流程与规则：

1. `POST /facility/snapshots`（planner）登记版本化设施快照；`GET /facility/{site}/capacity` 展示每项约束的总量、基线已用、已完成消耗、已批准未结案预留与**剩余量**。
2. `POST /rack-changes`（dispatcher）提交变更，自动基于当前快照生成影响分析：每项约束剩余量、机柜 U 位冲突、散热等级越限以及**冲突申请**（窗口重叠、机位重叠、容量竞争，区分硬预留与待审批竞争）与**决定依据**。
3. 施工窗口、资源预留与回退方案**整体审批**：risk 角色 `POST /rack-changes/{id}/decision`，申请人不能批准自己的变更；批准时按最新快照与全部在途变更复算，冲突未消除不能批准并写入 `basis` 决定依据。
4. 批准后按维度（站点电力/制冷/承重、机柜承重/受电、端口类型）写入预留锁；待审批竞争变更只提示不扣减。所有状态迁移均纳入哈希审计链。
5. 现场回执严格按步骤顺序推进：全部成功才完成（余量转为已完成消耗、释放锁）；任一步骤失败进入 `failed`，**不把部分完成视为成功**，必须显式开始回退（成功回退后释放预留）或由 risk 转人工接管（继续保留预留锁）。
6. 退回/驳回后申请人可修订，旧修订置为 `superseded` 并保留完整**修订链**（`revision_chain`、历次审批依据）；待审批期间详情按当前在途变更实时复算影响。
