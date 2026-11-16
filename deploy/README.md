# 部署说明

本目录包含 heartbeat 的容器化部署文件：

| 文件 | 说明 |
| --- | --- |
| `Dockerfile` | 镜像构建定义（Python 3.12-slim + gunicorn） |
| `docker-compose.yml` | 单机编排：端口映射、环境变量、数据卷、健康检查 |
| `gunicorn.conf.py` | gunicorn 配置，并在 master 的 `when_ready` 钩子中启动巡检调度器 |
| `export/` | 运行期数据与日志的宿主机挂载目录 |

构建上下文为**仓库根目录**（`Dockerfile` 中需要复制 `app.py`、`heartbeat/`、`templates/`、`pyproject.toml`），因此所有命令都需要在仓库根目录执行，并通过 `-f deploy/...` 指定文件。

## 前置条件

- Docker Engine 20.10+，并已安装 Docker Compose v2（`docker compose` 子命令）。
- 宿主机可访问外网，用于拉取基础镜像和安装 Python 依赖。
- 默认映射到宿主机 `5100` 端口，确保该端口未被占用。

## 快速开始

在**仓库根目录**执行：

```bash
# 1. 创建持久化目录（已存在则跳过）
mkdir -p deploy/export/data deploy/export/log

# 2. 构建并后台启动
docker compose -f deploy/docker-compose.yml up -d --build

# 3. 查看状态与健康检查结果
docker compose -f deploy/docker-compose.yml ps

# 4. 查看日志
tail -f deploy/export/log/*.log
```

启动后访问 <http://localhost:5100/>，在 `/config` 页面添加服务与心跳地址。

停止与清理：

```bash
# 停止并移除容器（deploy/export/ 下的数据库与日志会保留）
docker compose -f deploy/docker-compose.yml down

# 重新构建镜像（代码更新后）
docker compose -f deploy/docker-compose.yml up -d --build
```

## 配置项

通过环境变量覆盖，可在执行 `docker compose` 时直接前置，或写在与 `docker-compose.yml` 同目录的 `.env` 文件（Compose 会自动读取）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEARTBEAT_PORT` | `5100` | 映射到宿主机的端口（容器内固定监听 `5000`） |
| `START_SCHEDULER` | `1` | 是否在容器内启动每分钟巡检调度器；`0` 表示关闭，需外部调用 `POST /api/check-now` 触发 |
| `LOG_LEVEL` | `INFO` | 应用日志级别（`TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL`） |
| `LOG_DIR` | `/app/logs` | 应用日志目录，与 gunicorn 日志共用挂载卷 |
| `GUNICORN_WORKERS` | `1` | gunicorn worker 数，SQLite 场景建议保持 `1` |
| `GUNICORN_THREADS` | `4` | 每个 worker 的线程数 |
| `GUNICORN_LOG_LEVEL` | `info` | gunicorn 日志级别 |

示例：改用 `8080` 端口并关闭内置调度器。

```bash
HEARTBEAT_PORT=8080 START_SCHEDULER=0 docker compose -f deploy/docker-compose.yml up -d
```

## 数据持久化

容器以非 root 用户（UID `10001`）运行，需要挂载目录可写：

| 容器路径 | 宿主机路径 | 说明 |
| --- | --- | --- |
| `/data` | `deploy/export/data/` | SQLite 数据库 `heartbeat.db` |
| `/app/logs` | `deploy/export/log/` | gunicorn `access.log` / `error.log`，应用日志 `heartbeat.log` / `heartbeat-error.log` |

备份数据库：

```bash
docker compose -f deploy/docker-compose.yml stop heartbeat
cp deploy/export/data/heartbeat.db deploy/export/data/heartbeat-$(date +%F).db.bak
docker compose -f deploy/docker-compose.yml start heartbeat
```

> 若宿主机目录属主与容器 UID 不一致导致写入失败，可执行
> `sudo chown -R 10001:10001 deploy/export/data deploy/export/log`。

## 运行方式

- 进程管理：`gunicorn` + `gthread`，默认 `1 worker × 4 threads`，入口 `heartbeat.app:app`。
- 巡检调度器：在 gunicorn master 的 `when_ready` 钩子中只启动一份，多 worker 也不会重复巡检。
- 健康检查：镜像内置 `HEALTHCHECK` 与 compose `healthcheck`，使用 Python 标准库请求 `http://127.0.0.1:5000/`，无需额外安装 curl。
- 时区：巡检按本地时区（默认 `Asia/Shanghai`，可通过 compose 的 `TZ` 或 `HEARTBEAT_TZ` 覆盖）写入分钟/日期字段并展示时间。

## 常见问题

**容器启动后不断重启**
查看 `deploy/export/log/error.log`、`deploy/export/log/heartbeat-error.log` 与 `docker compose -f deploy/docker-compose.yml logs heartbeat`。常见原因是数据目录不可写（见上文 `chown`）。

**健康检查显示 unhealthy**
确认容器内 `PORT` 与健康检查一致（默认均为 `5000`）；若自定义了 `PORT`，健康检查会读取同名环境变量，一般无需改动。

**页面有服务但没有检查数据**
确认 `START_SCHEDULER=1`；若已设为 `0`，需由外部定时任务调用 `POST /api/check-now`。

**需要多副本或更高并发写入**
默认 SQLite 适合单实例。多副本请改用 PostgreSQL：自定义镜像安装 `psycopg2` 驱动，并将 `HEARTBEAT_DB` 指向 PostgreSQL DSN，同时在 compose 中调整数据卷。
