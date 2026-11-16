import os

from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.getenv("HEARTBEAT_DB", "sqlite:///heartbeat.db")
PORT = int(os.getenv("PORT", "5000"))
START_SCHEDULER = os.getenv("START_SCHEDULER", "1") == "1"
