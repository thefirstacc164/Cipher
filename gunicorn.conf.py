"""
Gunicorn tuning for the free Render tier (512 MB, shared CPU).

Render's start command points at this file:

    gunicorn -c gunicorn.conf.py app:app

PORT BINDING — why we listen on three ports at once
---------------------------------------------------
Render's network configuration remembers whichever port this service
answered on historically: v1.1 ran `gunicorn app:app`, which binds
gunicorn's own default 127.0.0.1:8000 (and the even earlier `python app.py`
era bound 5000). v1.2 bound only 10000 — Render saw that as a brand-new
primary port on every deploy, restarted the deploy "to update network
configuration", and SIGTERMed the fresh worker seconds after its health
check passed, over and over ("No open HTTP ports detected ... Port scan
timeout"). Binding 5000 + 8000 + 10000 (+ whatever $PORT says) means the
port Render probes is always answered on the first scan: no detection, no
restart loop, no dead deploys.

Why one worker and threads instead of more workers: every worker is a full
copy of the Python process, and the in-process caches in app.py (user rows,
immunity, settings, schema) only help if requests land on the same worker.
On a 512 MB box, 1 worker x 8 threads beats 2-4 workers on both memory and
cache hit rate — and the background cleanup job only runs once instead of
once per worker.
"""

import multiprocessing
import os

_bind = []
for _p in (os.getenv("PORT", "10000"), "10000", "8000", "5000"):
    _entry = "0.0.0.0:" + str(_p)
    if _entry not in _bind:
        _bind.append(_entry)
bind = _bind
workers = 1
threads = 8
worker_class = "gthread"
timeout = 60
graceful_timeout = 20
keepalive = 5

# The app is small; loading it per worker is fine and keeps the scheduler
# from being duplicated by fork().
preload_app = False

max_requests = 1000          # recycle before a slow leak becomes an OOM
max_requests_jitter = 100

accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(m)s %(U)s %(s)s %(L)ss %(b)sB'


def when_ready(server):
    server.log.info(
        "Cipher v1.2.0 up: workers=%s threads=%s cpus=%s ports=%s",
        workers, threads, multiprocessing.cpu_count(), "+".join(_bind),
    )
