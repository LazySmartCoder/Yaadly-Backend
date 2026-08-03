import multiprocessing
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
threads = 4
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
