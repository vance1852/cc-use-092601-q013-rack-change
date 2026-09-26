# 增加机柜上架变更与容量影响审批基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配、情景分析与机柜上架变更审批；
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

扩容上架同时占用电力（功耗曲线峰值）、制冷（按散热等级 AIR_L1/AIR_L2/LIQUID_L3 换算）、
承重（设备重量）和网络端口（按类型分别计数）四类约束。设施约束目录登记每项容量、已用与预留：

- `PUT /facilities/{facility_id}/constraints`：维护某设施的约束容量（`constraint_key` 取
  `power`、`cooling`、`weight` 或 `network_port:<类型>`）；
- `GET /facilities/{facility_id}/constraints`：展示每项约束的总量、已用、预留与剩余量。

变更流程（角色 `facilities` 提交/施工，`risk` 或其他设施工程师审批）：

1. `POST /rack-changes` 提交申请，保存设备清单版本 `bom_version`、机柜 U 位、功耗曲线、
   散热等级、端口需求、施工窗口、回退方案与逐步实施步骤；响应含基于当前设施快照的影响分析
   （每项约束剩余量、超配/U 位重叠等硬性冲突、窗口与容量相关的冲突申请）。
2. 同一 `change_id` 再次提交形成新版本修订链，旧待批版本置为 `superseded`；内容不变时按幂等重放。
3. `POST /rack-changes/{id}/approve|reject` 整体审批窗口、预留与回退方案，必须填写 `basis`；
   **申请人不能批准自己的变更**。批准时重新校验快照并锁定余量，硬性冲突下禁止批准。
4. `POST /rack-changes/{id}/steps/{n}` 按顺序回执；任一步骤失败用
   `POST /rack-changes/{id}/steps/{n}/fail` 选择 `rollback`（按逆序回执回退，全部回退完成后释放锁定）
   或 `manual`（人工接管，由非申请人经 `manual-resolution` 判定完成或释放）。
   **部分完成永远不会被视为成功**；全部步骤完成后锁定量才转入已用。
5. `GET /rack-changes/{id}` 返回内容、影响分析（待批版本实时刷新）、决定依据、步骤回执、
   预留台账与完整修订链；`GET /rack-changes?facility_id=...` 列出各变更最新版本。

所有提交、审批、回执、回退与接管结论均写入哈希链审计事件，可用 `GET /audit/chain` 校验。
