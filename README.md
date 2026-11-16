# heartbeat
heartbeat for website health

## Quick start

1. Install dependencies:
   ```bash
   python -m venv .venv
   . .venv/bin/activate
   python -m pip install -e .
   ```
2. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
3. Set your database and runtime settings in `.env`.
4. Start the app:
   ```bash
   python app.py
   ```

## Web routes

- `/`: 状态首页（展示根服务及其子服务的健康状态，并显示最近失败提示）
- `/config`: 服务检测配置页

## Configuration

The application reads environment variables from a `.env` file automatically via `python-dotenv`.

Example settings:

```env
HEARTBEAT_DB=sqlite:///heartbeat.db
PORT=5000
START_SCHEDULER=1
```

- `HEARTBEAT_DB`: SQLAlchemy database URL. Use a SQLite file path such as `sqlite:///heartbeat.db` or a PostgreSQL DSN such as `postgresql+psycopg2://user:password@localhost:5432/heartbeat`.
- `PORT`: HTTP port for Flask when running the app directly.
- `START_SCHEDULER`: set to `1` to start the 1-minute health-check scheduler, or `0` to disable it during local debugging.

The repository includes `.env.example` as the template for local configuration.
