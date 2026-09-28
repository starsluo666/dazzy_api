import os

bind = "0.0.0.0:8000"
workers = int(os.getenv("DAZZY_GUNICORN_WORKERS", "2"))
worker_class = "sync"
timeout = int(os.getenv("DAZZY_GUNICORN_TIMEOUT", "90"))
graceful_timeout = 90
max_requests = 1000
max_requests_jitter = 100
# Django trusts only X-Forwarded-Proto, set by the private reverse proxy.
# Do not additionally trust arbitrary scheme headers at the WSGI server layer.
secure_scheme_headers = {}
accesslog = "-"
errorlog = "-"
# OAuth codes and payment query parameters must not appear in application access logs.
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(L)s'
