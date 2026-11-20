# 铁路晚点月报对账 API（含到站台账）

车站一条到站记录改归属后，统计员要用更正后的到站次数预检晚点月报。本服务在
原联发预检能力之上新增**到站台账**：批量提交归档流水号、修订号、归属月、线路
与车站（也可撤销），按修订规则维护每线×每站×每月的到站次数汇总；台账版
联发预检直接读取同一份已提交的月汇总作为到站上限，无需手填、也无需扫描流水。

## 启动

```bash
docker compose up --build
```

服务监听 `8000` 端口，Swagger UI 位于 `http://localhost:8000/docs`。
台账使用 SQLite，库文件位于容器内 `/data/ledger.db`，由命名卷
`ledger-data` 持久化，容器重建/重启后台账与版号仍可用。
可用环境变量 `LEDGER_DB_PATH` 覆盖库文件路径。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
LEDGER_DB_PATH=./data/ledger.db uvicorn app.main:app --reload
pytest            # 跑测试
```

## 台账规则

- **批量提交**：每批 1–500 条，同一流水号每批限一条（违反返回 422）。
  `upsert` 必须带 `month/line_id/station_id`；`revoke` 不允许带归属字段。
- **只按最高修订计数**：每流水号保存最高修订号记录，只有 `active` 状态计数；
  撤销后流水与修订号保留（状态 `revoked`），不再计数。
- **不改账的报送**（返回 200，列入 `skipped`，版号不递增）：
  - 修订号低于现行 → `lower_revision`；
  - 同修订号且内容完全一致的原样重送 → `identical_resubmission`；
  - 对已撤销流水号重发同号撤销 → `already_revoked`。
- **整批退回**（返回 409，不写任何数据，原账与版号保留）：
  - 同修订号但归属内容不同（同版异文）→ `same_revision_conflict`，整批连带退回；
  - 撤销一个从未入账的流水号 → `serial_not_found`。
- **跨月更正**：更高修订号的 `upsert` 在一个事务内从旧月（旧线×站）扣一笔、
  向新月（新线×站）加一笔；撤销后用更高修订号重新入账则只加新笔。
- **原子性与版号**：流水、受影响月份的月汇总、全局版号在同一 SQLite 事务内
  提交，异常回滚；仅当批内确有生效变更时版号才 +1。
- **月份状态**：月份一经入账即永久标记为已入账。已入账但被清空的月份保留
  0 次汇总行，预检按零次处理；从未入账的月份预检返回 404 并列明缺数月份。

## 接口

### `POST /api/v1/ledger/batches` — 批量提交到站流水

请求：`{"items": [{"serial_no", "revision", "action", "month", "line_id", "station_id"}]}`
（`action` 取 `upsert`/`revoke`，默认 `upsert`）。

成功响应含：`ledger_version`（提交后的全局版号）、`applied`（逐条生效类型：
`insert/replace/reactivate/revoke/revoke_revision_bump`）、`skipped`（跳过原因）、
`affected_months`（汇总发生变化的月份）。

### `POST /api/v1/ledger/precheck` — 台账版联发预检（可一次多月）

请求：`{"months": [{"month", "lines", "stations"}]}`，其中 `lines/stations`
结构与原预检一致（线路核定晚点次数、每站已发版/待发版），**不再需要手填
`arrivals`**——服务直接读台账月汇总填充每格上限，不扫描流水。

响应按月份给出与原预检同构的可行交叉表、所选版号，并额外返回：

- 顶层 `ledger_version`：本次预检读取的台账版号；
- 每月 `arrivals_used`：实际采用的到站上限（读自月汇总，含 0 次行）。

任一月份从未入账时整体返回 404，`missing_months` 列出全部缺数月份。

### 查询接口

- `GET /api/v1/ledger/months/{month}/summary`：月汇总（`booked` 标明是否曾入账，
  清空后仍为 `true`，行内 `count` 可为 0）。
- `GET /api/v1/ledger/serials/{serial_no}`：某流水号现行最高修订记录（不存在 404）。
- `GET /api/v1/ledger/version`：当前全局版号。
- `POST /api/v1/reconciliation/precheck`：原手填到站上限的单月预检，保持不变。
- `GET /health`：健康检查（Compose healthcheck 使用）。

## 求解方法

版本选择按待发站子集从大到小枚举（总量先剪枝），第一个可行子集即
"采用待发版站数最多"的最优解；可行性判定与矩阵构造用 Dinic 最大流
（源→线路容量=核定数，线路→车站容量=到站数，车站→汇容量=所选版晚点数）。
供需与容量均为整数，故最大流直接给出整数矩阵。

## Swagger 试跑路径（跨月更正 + 两月预检）

1. `POST /api/v1/ledger/batches` 入账两条 `2026-09 / L1 / S1`（如 `ARC-0001`、
   `ARC-0002`，`revision=1`）。
2. 再提交 `ARC-0001`、`revision=2`、归属改到 `2026-10`：响应
   `effect=replace`，`affected_months=["2026-09","2026-10"]`，旧月汇总变 0、
   新月汇总为 1，版号递增。
3. `POST /api/v1/ledger/precheck` 同时提交 `2026-09`、`2026-10` 两个月的
   线路核定与车站版本：两月各出一张交叉表，顶层返回所用 `ledger_version`，
   `arrivals_used` 分别为 0 与 1。

## 实测结果

- `pytest`：**32 项全部通过**（求解器 6 + 原 API 8 + 台账 18），原 14 项无回归。
  台账测试覆盖：入账计数与版号、低修订/原样重送跳过、同版异文整批退回且原账
  保留、跨月更正旧月扣新月加、撤销保留修订号、撤销后再入账、撤销不存在流水号、
  重启重开库文件持久化、多月预检读汇总、未入账 404、已入账清空按零次（含零上限
  下核定 1 时无法联发）、跨月更正端到端。
- 实跑服务（与容器同一 `uvicorn app.main:app` 命令，临时 SQLite 文件）通过 HTTP
  冒烟 14 个调用：健康检查、入账、两类跳过、同版异文 409（连带流水号未入账）、
  跨月更正、撤销、两月预检（cells `[[0]]`/`[[1]]`，10 月正确采用待发版）、
  未入账月份 404；杀进程后以同一库文件重启，版号（3）、撤销状态、0 次汇总行
  均完整保留；`/docs` 返回 200，OpenAPI 含全部 7 个路径。
- 原手填预检回归：两站更正场景仍返回 `cells=[[6,6]]`、待发站数 2。

## 剩余事项

- 本沙箱无 Docker 守护进程，`docker compose up --build` 未实际执行；
  Dockerfile（`python:3.12-slim`，启动命令与本地验证一致）、compose 命名卷与
  `LEDGER_DB_PATH` 配置需在有 Docker 的环境复核。
- 持久化采用单节点 SQLite + 进程内锁，适合单副本部署；若需多副本水平扩展，
  需换用 Postgres 等外部数据库并改造事务边界。
- 台账版号为全局单调递增整数（有生效变更的批次才 +1）；若业务要求按月份或
  线路分段版号，需要另行设计。
- 预检不落库；如需留存每次预检记录，需另加表。
