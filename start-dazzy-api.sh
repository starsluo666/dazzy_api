#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="${DAZZY_API_DIR:-$SCRIPT_DIR}"
ENV_FILE="${DAZZY_API_ENV_FILE:-$API_DIR/.env}"
RUN_DIR="${DAZZY_API_RUN_DIR:-$API_DIR/.run}"
DJANGO_BIND="${DAZZY_API_BIND:-127.0.0.1:8000}"
CELERY_LOG_LEVEL="${DAZZY_CELERY_LOG_LEVEL:-info}"
APPLY_MIGRATIONS=false

usage() {
  cat <<'EOF'
用法：./start-dazzy-api.sh [--migrate]

在一个终端中启动 DAZZY API 开发服务：
  - Django 开发服务器
  - Celery Worker
  - Celery Beat（仅启动一个实例）

脚本默认读取当前项目目录下的 .env，并要求 PostgreSQL/PostGIS 和 Redis 已经可以连接。
默认情况下，如果存在尚未应用的数据库迁移，脚本将拒绝启动。
传入 --migrate 可在启动各项服务前自动执行数据库迁移。

可通过以下环境变量覆盖默认配置：
  DAZZY_API_DIR             API 项目目录
  DAZZY_API_ENV_FILE        环境变量文件路径
  DAZZY_API_RUN_DIR         运行时文件目录
  DAZZY_API_BIND            Django 监听地址（默认 127.0.0.1:8000）
  DAZZY_CELERY_LOG_LEVEL    Celery 日志级别（默认 info）
  DAZZY_CELERY_POOL         Celery 进程池；Windows Git Bash 下默认使用 solo
  DJANGO_SETTINGS_MODULE    Django 配置模块（默认 config.settings.local）
EOF
}

case "${1:-}" in
  "") ;;
  --migrate) APPLY_MIGRATIONS=true ;;
  -h|--help) usage; exit 0 ;;
  *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "未在 PATH 中找到 uv，请先安装并配置 uv。" >&2
  exit 1
fi
if [[ ! -d "$API_DIR" || ! -f "$API_DIR/manage.py" ]]; then
  echo "DAZZY API 项目目录无效：$API_DIR" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "未找到环境变量文件：$ENV_FILE" >&2
  echo "请先将当前项目的 .env.example 复制为 .env，并填写所需配置。" >&2
  exit 1
fi

mkdir -p "$RUN_DIR"
cd "$API_DIR"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.local}"

echo "[预检] 执行 Django 系统检查"
uv run --env-file "$ENV_FILE" python manage.py check --settings="$DJANGO_SETTINGS_MODULE"
if [[ "$APPLY_MIGRATIONS" == true ]]; then
  echo "[预检] 应用数据库迁移"
  uv run --env-file "$ENV_FILE" python manage.py migrate --settings="$DJANGO_SETTINGS_MODULE"
else
  echo "[预检] 检查是否存在尚未应用的数据库迁移"
  uv run --env-file "$ENV_FILE" python manage.py migrate --check --settings="$DJANGO_SETTINGS_MODULE"
fi

worker_args=(uv run --env-file "$ENV_FILE" celery -A config worker --loglevel="$CELERY_LOG_LEVEL")
if [[ -n "${DAZZY_CELERY_POOL:-}" ]]; then
  worker_args+=(--pool="$DAZZY_CELERY_POOL")
else
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) worker_args+=(--pool=solo) ;;
  esac
fi

pids=()
names=()

start_process() {
  local name="$1"
  shift
  echo "[启动] $name"
  "$@" &
  pids+=("$!")
  names+=("$name")
}

shutdown() {
  trap - EXIT INT TERM
  if ((${#pids[@]})); then
    echo "[停止] 正在停止 DAZZY API 的全部进程"
    for pid in "${pids[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
    for pid in "${pids[@]}"; do
      wait "$pid" 2>/dev/null || true
    done
  fi
}
trap shutdown EXIT INT TERM

start_process "Django 服务" \
  uv run --env-file "$ENV_FILE" python manage.py runserver "$DJANGO_BIND" \
  --settings="$DJANGO_SETTINGS_MODULE"
start_process "Celery Worker" "${worker_args[@]}"
start_process "Celery Beat" \
  uv run --env-file "$ENV_FILE" celery -A config beat --loglevel="$CELERY_LOG_LEVEL" \
  --schedule="$RUN_DIR/celerybeat-schedule"

echo "[就绪] Django 服务地址：http://$DJANGO_BIND"
echo "[就绪] 按 Ctrl+C 可停止 Django、Celery Worker 和 Celery Beat。"

set +e
wait -n "${pids[@]}"
status=$?
set -e
echo "[退出] 某个进程已退出，状态码为 $status；正在停止全部服务。" >&2
exit "$status"
