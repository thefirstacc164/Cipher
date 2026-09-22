"""
Gunicorn tuning for the free Render tier (512 MB, shared CPU).

Render's default start command is `gunicorn app:app`. Point it at this file
instead and the app gets sized for the box it actually runs on:

    gunicorn -c gunicorn.conf.py app:app

Why one worker and threads instead of more workers: every worker is a full
copy of the Python process, and the in-process caches in app.py (user rows,
immunity, settings, schema) only help if requests land on the same worker.
On a 512 MB box, 1 worker x 8 threads beats 2-4 workers on both memory and
cache hit rate — and the background cleanup job only runs once instead of
once per worker.
"""

import multiprocessing

bind = "0.0.0.0:" + str(__import__("os").getenv("PORT", "10000"))
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
        "Cipher v1.2.0 up: workers=%s threads=%s cpus=%s",
        workers, threads, multiprocessing.cpu_count(),
    )
