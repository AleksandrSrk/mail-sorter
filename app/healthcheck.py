"""Docker HEALTHCHECK: сервис жив, если heartbeat обновлялся недавно."""
import os
import sys
import time

data_dir = os.environ.get("DATA_DIR", "/app/data")
limit = int(os.environ.get("POLL_INTERVAL", "60")) * 5 + 120
try:
    with open(os.path.join(data_dir, "heartbeat")) as f:
        age = time.time() - int(f.read().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < limit else 1)
