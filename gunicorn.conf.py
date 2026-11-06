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
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Every request path is a search keystroke or a word the reader looked up; the access
# log is a debugging aid, not a record to keep. uvicorn's access logger reuses these handlers.
accesslog = "-" if loglevel == "debug" else None
errorlog = "-"
# gunicorn >= 25.1 opens a control socket under $XDG_RUNTIME_DIR or $HOME/.gunicorn.
# Nothing talks to it here and the fallback path is not writable after dropping root.
control_socket_disable = True
