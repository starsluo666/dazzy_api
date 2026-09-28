#!/usr/bin/env sh
set -eu

case "${1:-api}" in
  api)
    echo '[启动] 收集 Django 后台静态文件'
    python manage.py collectstatic --noinput
    echo '[启动] 启动 Gunicorn API 服务'
    exec gunicorn config.wsgi:application --config deploy/gunicorn.conf.py
    ;;
  worker)
    echo '[启动] 启动 Celery Worker'
    exec celery -A config worker --loglevel="${DAZZY_CELERY_LOG_LEVEL:-info}" \
      --concurrency="${DAZZY_WORKER_CONCURRENCY:-2}" --hostname='celery@%h'
    ;;
  beat)
    echo '[启动] 启动 Celery Beat（同一环境只允许一个实例）'
    exec celery -A config beat --loglevel="${DAZZY_CELERY_LOG_LEVEL:-info}" \
      --schedule=/app/run/celerybeat-schedule --pidfile=/tmp/celerybeat.pid
    ;;
  *) exec "$@" ;;
esac
