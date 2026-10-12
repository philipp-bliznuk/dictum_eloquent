import os

bind = os.environ.get("BIND", "0.0.0.0:3000")
# Single worker on purpose: the fuzzy search index lives in process memory and is
# invalidated on writes; multiple workers would serve stale results. Uvicorn's
# async loop handles LAN-scale concurrency fine.
workers = 1
worker_class = "uvicorn_worker.UvicornWorker"
timeout = 120
graceful_timeout = 20
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
