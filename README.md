# heartbeat

网站健康巡检与状态面板。按分钟探测服务心跳地址，按日/年聚合可用性，并提供状态首页、服务状态日历与配置页面。

## 功能特性

- **服务与子服务**：服务支持父子层级（`parent_id`），首页按根服务聚合展示子服务状态。
- **多心跳地址**：一个服务可配置多个 heartbeat endpoint，全部通过才算健康。
- **可配置成功规则**：
  - `status==200`（默认）：HTTP 状态码等于期望值即成功；
  - `body_contains:OK`：响应体包含指定文本即成功。
- **每分钟巡检**：内置调度器每 1 分钟检查所有启用服务，结果写入当天对应的分钟字段。
- **三级聚合**：
  - 分钟级：当天 00:00–23:59 共 1440 个检查点；
  - 日级：年内每天的健康状态；
  - 年度：服务状态日历按年展示不健康日期。
- **失败记录**：失败时记录检查时间、状态码和明细，首页展示最近一次失败提示。
- **邮件报警与频控**：每个 endpoint 可独立开启 email 报警、配置收件人，并按“x 分钟内最多发送 n 次”限流；SMTP 通过环境变量全局配置。
- **报警记录**：每次实际发送（成功或失败）都会落库，记录当次检测状态与全部 endpoint 细节，可在报警记录页按服务、日期、发送状态追溯。
- **可视化页面**：状态首页、服务检测配置页（支持编辑已有服务）、服务状态日历页、报警记录页。

## 技术栈

- Python >= 3.9
- Flask 3.x（Jinja2 模板渲染）
- SQLAlchemy 2.x（ORM + SQLite）
- schedule（分钟级后台调度）
- python-dotenv（读取 `.env`）
- Loguru（结构化日志、控制台与文件轮转输出）

## 项目结构

```
heartbeat/
├── app.py                        # 启动入口：调用 heartbeat.app.main()
├── heartbeat/
│   ├── __init__.py               # 导出 main
│   ├── app.py                    # create_app()、调度器启动、应用入口；导入时自动建表
│   ├── alarm.py                  # 邮件报警发送、频控判断、报警记录写入
│   ├── config.py                 # 读取 .env 中的运行配置
│   ├── logging_config.py         # Loguru 初始化、标准库日志接管与文件轮转
│   ├── model.py                  # SQLAlchemy 模型、engine、session 与建表逻辑
│   ├── router.py                 # 页面路由与 REST API
│   └── service.py                # 巡检逻辑、成功规则判定、状态聚合
├── templates/
│   ├── layout.html               # 公共布局与导航
│   ├── status_home.html          # 状态首页
│   ├── service_config.html       # 服务检测配置页
│   ├── service_status_calendar.html # 年度服务状态日历
│   └── alarm_records.html        # 报警记录页
├── tests/
│   └── test_heartbeat.py         # unittest 测试（内存 SQLite）
├── deploy/
│   ├── Dockerfile                # 镜像构建定义
│   ├── docker-compose.yml        # 单机编排（端口、环境变量、数据卷、健康检查）
│   └── gunicorn.conf.py          # gunicorn 配置与调度器启动钩子
├── .dockerignore                 # Docker 构建上下文忽略规则（必须位于仓库根目录）
├── .env.example                  # 环境变量模板
├── pyproject.toml                # 依赖与打包配置
└── uv.lock                       # uv 锁定的依赖版本
```

## 快速开始

1. 创建虚拟环境并安装依赖：

   ```bash
   python -m venv .venv
   . .venv/bin/activate
   python -m pip install -e .
   ```

2. 复制环境变量模板：

   ```bash
   cp .env.example .env
   ```

3. 按需修改 `.env`（默认端口 5100，默认数据库 `sqlite:///heartbeat.db`）。

4. 启动应用：

   ```bash
   python app.py
   ```

5. 打开 <http://localhost:5100/>；在 `/config` 页面添加服务与心跳地址。

## 配置

应用启动时通过 `python-dotenv` 自动读取仓库根目录的 `.env`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEARTBEAT_DB` | `sqlite:///heartbeat.db` | SQLAlchemy 数据库连接串。也可以使用 PostgreSQL DSN（如 `postgresql+psycopg2://user:password@localhost:5432/heartbeat`），但需自行安装 `psycopg2` 驱动。 |
| `PORT` | `5000` | 直接运行 `python app.py` 时的监听端口（`.env.example` 中为 5100）。 |
| `START_SCHEDULER` | `1` | 设为 `1` 启用每分钟巡检调度器；设为 `0` 关闭（本地调试或由外部触发时使用）。 |
| `LOG_LEVEL` | `INFO` | Loguru 日志级别（`TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL`）。 |
| `LOG_DIR` | `logs` | 日志文件目录；容器内为 `/app/logs`。 |
| `SMTP_HOST` | 空 | SMTP 服务器地址；为空时邮件报警发送会失败并记录原因。 |
| `SMTP_PORT` | `587` | SMTP 端口。 |
| `SMTP_USERNAME` | 空 | SMTP 登录账号，可留空。 |
| `SMTP_PASSWORD` | 空 | SMTP 登录密码。 |
| `SMTP_FROM` | `SMTP_USERNAME` | 报警邮件发件人地址。 |
| `SMTP_USE_TLS` | `1` | 非 SSL 连接是否使用 STARTTLS。 |
| `SMTP_USE_SSL` | `0` | 是否直接使用 SSL 连接（如 465 端口）。 |
| `SMTP_TIMEOUT` | `10` | SMTP 连接与发送超时（秒）。 |

成功规则（`success_rule`）示例：

```text
status==200
status==204
body_contains:OK
```

## 日志

应用通过 Loguru 统一记录关键事件与异常，同时输出到控制台与按大小轮转的文件：

- `heartbeat.log`：全量日志（启动、巡检开始/结束、服务配置变更、巡检失败明细等）；
- `heartbeat-error.log`：仅 ERROR 及以上，包含完整异常堆栈。

默认写入仓库根目录 `logs/`（容器内为 `/app/logs`），单文件 10 MB 轮转、保留 30 天、zip 压缩。Flask、Werkzeug、SQLAlchemy、schedule 的标准库日志会被自动接管。可通过 `LOG_LEVEL` 调整级别、`LOG_DIR` 调整目录。

## 页面与 API

### 页面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 状态首页：根服务及其子服务健康状态、最近失败提示 |
| GET | `/config` | 服务检测配置页 |
| GET | `/services/<service_id>/service-status-calendar` | 年度服务状态日历，可用查询参数 `year` 指定年份 |
| GET | `/alarm-records` | 报警记录页，可按服务、日期范围、发送状态筛选 |

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/services` | 列出全部服务及其心跳配置 |
| POST | `/api/services` | 创建服务。请求体：`name`（必填）、`parent_id`、`enabled`，以及 `heartbeat_configs: [{"heartbeat_url", "success_rule", "alarm_enabled", "alarm_channels", "alarm_recipients", "alarm_window_minutes", "alarm_max_per_window"}]`（或兼容的 `heartbeat_urls: []`，此时报警默认关闭） |
| PATCH | `/api/services/<service_id>` | 更新服务；传入 `heartbeat_configs` / `heartbeat_urls` 时会整体替换心跳地址 |
| DELETE | `/api/services/<service_id>` | 删除叶子服务；存在子服务时返回 409，并清理该服务的历史检测/报警数据 |
| POST | `/api/check-now` | 立即执行一次巡检 |
| GET | `/api/status-tree` | 根服务 + 子服务的状态树，含最近失败提示 |
| GET | `/api/services/<service_id>/status/current` | 当前状态与最近检查时间 |
| GET | `/api/services/<service_id>/status/recent` | 最近若干天的日状态，`days` 默认 7 |
| GET | `/api/services/<service_id>/status/recent-daily-detail` | 每日明细（健康/不健康/未知分钟数、失败时间点）。支持 `days`（默认 90，最大 180），或 `start_date` + `end_date`（`YYYY-MM-DD`，最大跨度 180 天） |
| GET | `/api/services/<service_id>/status/recent-hours` | 最近若干小时的分钟级状态，`hours` 默认 12，范围 1–24 |
| GET | `/api/services/<service_id>/children/status/recent` | 子服务最近若干天的日状态，`days` 默认 7 |
| GET | `/api/services/<service_id>/failures` | 指定日期的失败记录，必填 `date=YYYY-MM-DD` |
| GET | `/api/alarm-records` | 报警记录查询。支持 `service_id`、`start_date` + `end_date`（默认最近 7 天，最大 180 天）、`send_status`（`sent`/`failed`）、`limit`（默认 100，最大 500）、`offset` |

## 数据模型

| 表 | 说明 |
| --- | --- |
| `services` | 服务定义：名称、父服务 `parent_id`、是否启用 |
| `service_endpoints` | 服务的检测地址、成功规则，以及报警开关、报警渠道（当前仅 `email`）、收件人、频控窗口与窗口内次数上限 |
| `daily_checks` | 每个服务每天一行，`minute_0000`–`minute_1439` 记录每分钟结果（1 健康 / 0 不健康 / NULL 未检查） |
| `yearly_checks` | 每个服务每年一行，`day_MMDD` 记录每日结果，用于服务状态日历 |
| `failure_records` | 失败明细：检查时间、HTTP 状态码、日志（URL、规则、错误详情） |
| `alarm_records` | 报警发送记录：服务/地址快照、发送时间、渠道、收件人、发送状态与错误，以及当次检测状态和全部 endpoint 明细（JSON） |

## 测试

测试使用内存 SQLite，不会影响本地数据库：

```bash
python -m unittest discover -s tests -v
```

## Docker 部署

### 镜像与编排

部署文件位于 `deploy/`，构建上下文为仓库根目录（因此 `.dockerignore` 放在根目录）：

```bash
# 构建镜像
docker build -f deploy/Dockerfile -t heartbeat:latest .

# 启动（后台运行并构建）
docker compose -f deploy/docker-compose.yml up -d --build

# 查看日志（gunicorn 访问/错误日志写入 deploy/export/log/）
tail -f deploy/export/log/*.log

# 停止并移除容器（deploy/export/ 下的数据与日志会保留）
docker compose -f deploy/docker-compose.yml down
```

启动后访问 <http://localhost:5100/>（宿主机端口可用 `HEARTBEAT_PORT` 覆盖）。

### 容器运行方式

- 使用 `gunicorn` 配合 `gthread`：默认 `1 worker × 4 threads`，入口为 `heartbeat.app:app`。
- 1 分钟巡检调度器在 gunicorn master 的 `when_ready` 钩子中启动，只启动一份；修改 `GUNICORN_WORKERS` 也无需担心重复巡检。
- 容器内监听 `5000`，数据库固定为 `/data/heartbeat.db`，由 compose 挂载到宿主机 `deploy/export/data/` 持久化。
- 健康检查通过 Python 标准库请求 `http://127.0.0.1:5000/`，镜像内无需额外安装 curl。

### 可调环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEARTBEAT_PORT` | `5100` | compose 映射到宿主机的端口 |
| `START_SCHEDULER` | `1` | 是否在容器内启动巡检调度器 |
| `LOG_LEVEL` | `INFO` | 应用日志级别 |
| `LOG_DIR` | `/app/logs` | 应用日志目录（与 gunicorn 日志共用挂载卷） |
| `GUNICORN_WORKERS` | `1` | gunicorn worker 数；SQLite 场景建议保持 1 |
| `GUNICORN_THREADS` | `4` | 每个 worker 的线程数 |
| `GUNICORN_LOG_LEVEL` | `info` | gunicorn 日志级别 |

示例（改端口并关闭内置调度，改由外部定时触发 `/api/check-now`）：

```bash
HEARTBEAT_PORT=8080 START_SCHEDULER=0 docker compose -f deploy/docker-compose.yml up -d
```

## 注意事项

- 当前默认数据库为 SQLite，适合单实例部署；如需多副本或高并发写入，请切换到 PostgreSQL 并补充驱动依赖。
- 关闭内置调度器（`START_SCHEDULER=0`）后，需要由外部定时任务调用 `POST /api/check-now` 才会产生检查数据。
- 模板目录位于仓库根级 `templates/`，应用通过包内相对路径 `../templates` 查找；部署时需保留源码目录结构（Docker 镜像已按此方式构建）。
- 检查结果使用本地时区（默认 `Asia/Shanghai`，可通过 `HEARTBEAT_TZ` 或 `TZ` 覆盖）写入分钟/日期字段并展示时间。
