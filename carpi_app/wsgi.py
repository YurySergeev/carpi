"""Entry point for hosting:  gunicorn -w 1 --threads 8 -b 0.0.0.0:7860 carpi_app.wsgi:server

Configure with environment variables (see config.py): CARPI_PUBLIC=1, CARPI_LOGS, CARPI_CACHE, CARPI_ABOUT.
Use ONE worker: drives are cached in that process's memory, and threads share it.
"""
import threading

from . import config
from .app import create_app
from .data import Store

store = Store(config.LOG_DIRS)
print(f"CarPi Analyzer: {len(store.drives)} drives, public={config.PUBLIC}", flush=True)
threading.Thread(target=store.preload, daemon=True).start()   # first clicks stay fast
app = create_app(store)
server = app.server
