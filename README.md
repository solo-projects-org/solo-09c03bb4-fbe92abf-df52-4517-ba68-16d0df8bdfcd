# 铁路晚点月报对账 API

车站补交晚点月报更正后，数据员提交一个月的对账数据，本服务做**联发预检**：
在每站已发版/待发版中各选一版，使**采用待发版的站数最多**，并给出一张
同时满足以下约束的可行晚点次数交叉表，供复核：

- 每格为不超过该线路在该站实际到站次数的非负整数（未列出的线路×车站组合视为 0 次到站）；
- 按线路合计 = 该线路核定晚点次数；
- 按站合计 = 该站所选版本的晚点次数。

若任何版本组合都不可行，返回"本次无法联发"，不生成对账表。

## 启动

```bash
docker compose up --build
```

服务监听 `8000` 端口，Swagger UI 位于 `http://localhost:8000/docs`，
在 `POST /api/v1/reconciliation/precheck` 上点 "Try it out" 即可试跑预检。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest            # 跑测试
```

## 接口

### `POST /api/v1/reconciliation/precheck`

请求体（一个月）：

| 字段 | 说明 |
| --- | --- |
| `month` | 月份，`YYYY-MM` |
| `lines` | 1–12 条线路：`line_id` + 核定晚点次数 `approved_delays` |
| `stations` | 1–12 个车站：`station_id` + 已发版 `published` + 至多一个待发版 `pending`（各含版号 `version` 与晚点次数 `delay_count`） |
| `arrivals` | 各线路在各站的实际到站次数；未列出的组合视为 0 |

响应：

- `feasible=true`：`selected_versions` 给出每站所选版号（`source` 标明已发/待发），
  `table` 为可行交叉表（`cells` 行=线路、列=车站，附 `line_totals`/`station_totals`），
  `pending_station_count` 为采用待发版的站数（已最大化）。
- `feasible=false`：`table`/`selected_versions` 为 `null`，`message` 报告本次无法联发。

### `GET /health`

健康检查（Compose healthcheck 使用）。

## 求解方法

版本选择按待发站子集从大到小枚举（总量先剪枝），第一个可行子集即
"采用待发版站数最多"的最优解；可行性判定与矩阵构造用 Dinic 最大流
（源→线路容量=核定数，线路→车站容量=实际到站数，车站→汇容量=所选版晚点数）。
供需与容量均为整数，故最大流直接给出整数矩阵。12×12 最坏情况
（924 个子集跑最大流）实测约 0.2s。

## 验证结果

- `pytest`：14 项测试全部通过（求解器 6 项 + API 8 项），含
  **两站更正必须同时采用才能对平**的情形（核定 12，两站已发各 4、待发各 6，
  单独采用任一站都对不平，两站同时采用待发版后 `[[6,6]]` 对平）。
- 实跑服务（与容器同一 `uvicorn` 命令）验证：Swagger `/docs` 正常打开；
  关键场景、无可行组合（返回无法联发且不生成表）、多线路优先全待发、
  13 条线路返回 422、12×12 压力场景均符合预期。

## 剩余事项

- 本沙箱无 Docker 守护进程，`docker compose up` 未实际执行；镜像构建与
  容器内运行需在有 Docker 的环境复核（Dockerfile 使用 `python:3.12-slim`，
  启动命令与本地验证一致）。
- 版本选择在"待发站数最多"的并列解中取枚举序首个；如需其他平局偏好
  （如改动格子最少），可再加排序规则。
- 预检不落库；如需留存每次预检记录或对接归档系统，需另加持久化。
