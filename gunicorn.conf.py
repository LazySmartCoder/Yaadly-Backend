import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
threads = 4
# The /api/transcribe/ endpoint calls Google Cloud Speech-to-Text synchronously
# and can take up to 90s (the client and service timeouts). Gunicorn must not
# kill the worker before then.
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
