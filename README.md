# 工厂能耗区间结算服务（FastAPI + SQLite）

按设备收集累计电表读数与换表交接，按相邻读数生成带来源编号的区间电量。
换表后新表起始值不会被算成负用电：跨表区间只有在区间内存在**唯一匹配**的
换表交接时才分段结算，否则标记为“无法判定”（`energy_kwh = null`），绝不报零。

## 启动（Docker Compose）

```bash
docker compose up --build
```

- API 地址：<http://localhost:8000>
- Swagger 操作入口：<http://localhost:8000/docs>（ReDoc：`/redoc`，OpenAPI：`/openapi.json`）
- SQLite 数据持久化在 compose 卷 `energy-data`（容器内 `/data/energy.db`）
- 测试用故障注入：把环境变量 `FAIL_DEVICE` 设为某设备号，该设备的重建将始终在提交前失败

## 数据模型与规则

### 读数 `POST /readings`

```json
{
  "source_id": "R1",          // 来源编号
  "revision": 1,              // 递增修订号（首次为 1，之后必须正好 +1）
  "device_id": "DEV-A",       // 设备
  "ts": "2026-11-10T00:00:00Z", // UTC 时刻（必须带时区）
  "meter_no": "M-100",        // 表号
  "cumulative_kwh": 1000.0    // 累计 kWh（非负）
}
```

### 换表交接 `POST /handovers`

```json
{
  "source_id": "HV1", "revision": 1, "device_id": "DEV-A",
  "ts": "2026-11-10T02:00:00Z",
  "old_meter_no": "M-100", "old_final_kwh": 1100.0,
  "new_meter_no": "M-200", "new_initial_kwh": 2.0
}
```

### 修订规则（读数与交接一致）

- 同一 `source_id` 的修订号必须严格连续递增（1 → 2 → 3…）；
- 同 `source_id + revision` 再次提交且**内容完全相同**：幂等，返回 `idempotent=true`；
- 同 `source_id + revision` 内容不同：**409 拒绝**（`content conflict`）；
- 回退旧修订号或跳号：**409 拒绝**（`revision rollback` / `non-consecutive revision`）。
- 内容指纹为除 `source_id/revision` 外业务字段的 SHA-256，入库保留全部历史修订。

### 区间电量 `GET /intervals`

相邻读数两两生成区间，区间上附两端读数的来源编号与修订号（以及使用的交接来源）：

| 情形 | 结算方式 |
| --- | --- |
| 同表 | 后值 − 前值；差值为负 → `null`（`negative_difference`），不报零 |
| 跨表，区间内有唯一匹配交接（表号、设备、时刻均匹配） | `(旧表末值−前读数)+(后读数−新表初值)`，任一段为负 → `null` |
| 跨表但无交接 / 交接表号不匹配 / 交接不唯一 | `null`（`handover_missing` / `ambiguous_handover`） |
| 真实零差值 | 正常写 `0.0`，`undeterminable=false` |

查询参数：`device_id`（可选）、`status=current|stale|all`（默认 current）。

## 迟到数据与重建的一致性保证

1. 读数/交接一旦**受理**，受影响设备的全部 `current` 区间在同一事务内立即
   置为 `stale`（失去当前供数资格，仍可按 `status=stale` 审计查询）。
2. 受理后自动按设备重建；也可 `POST /rebuild?device_id=...` 手动重试，
   或 `POST /rebuild`（默认只重建仍有 stale 区间的设备）。
3. 重建在单事务内完成：失败/进程崩溃时事务回滚，旧结果保持 stale，
   **不会复活为 current**；服务重启不自动重建，只把崩溃遗留的 running
   运行标记为 `interrupted`（见 `GET /rebuild-runs`）。
4. 重建按设备隔离：一个设备失败不影响其他设备，无关区间全程可查；
   重试可完成重建并恢复 current 供数。
5. 无法判定的区间以 `null` 表达，零值只表示实测零差值。

测试钩子：对提交或 `/rebuild` 加请求头 `X-Fail-Rebuild: true`，
对应设备的重建会在提交前失败，用于验证上述失败/重试场景。

## 本地开发与测试

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest -q
```

## 目录结构

```
app/            FastAPI 应用（models/database/service/main）
tests/          17 项端到端自测（TestClient + 临时 SQLite 文件）
Dockerfile      python:3.11-slim 镜像，含 /health 健康检查
docker-compose.yml
```
