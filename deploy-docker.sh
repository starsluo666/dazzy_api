#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${PROJECT_DIR}/.deploy"
COMPOSE_ENV="${DAZZY_COMPOSE_ENV:-${PROJECT_DIR}/.env.docker}"
COMMAND="${1:-help}"
if (($#)); then shift; fi

log() { printf '[dazzy-docker] %s\n' "$*"; }
fail() { printf '[dazzy-docker] 错误：%s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
用法：bash ./deploy-docker.sh <命令>

  deploy [--migrate]  构建版本镜像、检查并发布；--migrate 在维护窗口执行迁移
  check              用当前镜像检查生产配置、数据库、缓存和迁移状态
  status             查看三个服务状态
  logs [api|worker|beat]  查看最近日志并持续跟踪（Ctrl+C 只退出日志）
  stop               依次停止 Beat、Worker、API，保留镜像和数据卷
  start              启动当前已发布版本并等待健康检查
  rollback [镜像名]  回退上一成功版本或指定本地镜像，不撤销数据库迁移
  manage <参数...>   用当前镜像运行 Django 管理命令，例如 grant_platform_admin 手机号

首次使用：复制 .env.docker.example 为 .env.docker，配置现有 Docker 网络；
业务配置放在 .env.online。迁移前做好数据库备份，旧启动脚本的 Beat 必须先停止。
默认 Compose 项目名固定为 dazzy-prod，同一套业务只部署一个 Beat。
部署记录保存在 .deploy/，旧镜像保留，便于回退；不会自动清理数据库或镜像。
EOF
}

case "$COMMAND" in
  help|-h|--help) usage; exit 0 ;;
  deploy|check|status|logs|stop|start|rollback|manage) ;;
  *) usage >&2; fail "未知命令：$COMMAND" ;;
esac

for tool in docker flock; do
  command -v "$tool" >/dev/null 2>&1 || fail "未找到 $tool，请在 Linux 服务器安装后重试。"
done
[[ -f "$COMPOSE_ENV" ]] || fail "缺少 $COMPOSE_ENV，请先复制并填写 .env.docker.example。"
COMPOSE_ENV="$(cd -- "$(dirname -- "$COMPOSE_ENV")" && pwd)/$(basename -- "$COMPOSE_ENV")"
cd "$PROJECT_DIR"
version="$(docker compose version --short)" || fail '请安装 Docker Compose v2.24 或更新版本。'
version="${version#v}"
IFS=. read -r major minor _ <<< "$version"
[[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || fail "无法识别 Compose 版本：$version"
(( major > 2 || (major == 2 && minor >= 24) )) || fail '需要 Docker Compose v2.24 或更新版本。'
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || fail '无法连接 Linux Docker 引擎。'

compose() {
  docker compose --project-name dazzy-prod --project-directory "$PROJECT_DIR" \
    --env-file "$COMPOSE_ENV" -f "$PROJECT_DIR/compose.production.yaml" "$@"
}

valid_image() { [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$ ]]; }
load_release() {
  current_image=""
  [[ ! -f "$STATE_DIR/current-image" ]] || current_image="$(< "$STATE_DIR/current-image")"
  export DAZZY_IMAGE="${current_image:-dazzy-api:local}"
  valid_image "$DAZZY_IMAGE" || fail '部署记录中的镜像名无效。'
}
load_release
compose config --quiet

require_image() {
  docker image inspect "$DAZZY_IMAGE" >/dev/null 2>&1 || fail "本地不存在镜像 $DAZZY_IMAGE，请先 deploy。"
}

run_job() { compose run --rm --no-deps -T --pull never api "$@"; }

preflight() {
  require_image
  log '检查生产配置和外部服务连接'
  run_job python manage.py check --deploy --fail-level ERROR
  run_job python -m deploy.check_dependencies
}

stop_services() {
  log '停止定时派发，再等待后台任务和 API 请求结束（每项最多 120 秒）'
  compose stop beat
  compose stop worker
  compose stop api
}

activate() {
  log "启动版本：$DAZZY_IMAGE"
  compose up -d --no-build --pull never --wait --wait-timeout 240 api worker beat
}

save_release() {
  if [[ -n "$current_image" && "$current_image" != "$DAZZY_IMAGE" ]]; then
    printf '%s\n' "$current_image" > "$STATE_DIR/previous-image.tmp"
    mv -- "$STATE_DIR/previous-image.tmp" "$STATE_DIR/previous-image"
  fi
  printf '%s\n' "$DAZZY_IMAGE" > "$STATE_DIR/current-image.tmp"
  mv -- "$STATE_DIR/current-image.tmp" "$STATE_DIR/current-image"
  printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$COMMAND" "$DAZZY_IMAGE" \
    >> "$STATE_DIR/releases.log"
}

release_error() {
  local status=$?
  trap - ERR
  log '操作失败。服务状态可能已改变，请运行 status / logs 检查；不会自动撤销迁移。' >&2
  if [[ -n "$current_image" ]]; then
    log "确认数据库兼容后可执行：bash ./deploy-docker.sh rollback $current_image" >&2
  fi
  exit "$status"
}

case "$COMMAND" in
  status) [[ $# == 0 ]] || fail 'status 不接受参数。'; compose ps -a; exit 0 ;;
  logs)
    [[ $# -le 1 ]] || fail 'logs 最多指定一个服务。'
    if (($#)); then
      case "$1" in api|worker|beat) ;; *) fail '服务名必须为 api、worker 或 beat。' ;; esac
    fi
    compose logs --tail 100 -f "$@"
    exit 0
    ;;
esac

mkdir -p "$STATE_DIR"
exec 9> "$STATE_DIR/deploy.lock"
flock -n 9 || fail '其他部署或管理命令正在执行，请稍后重试。'
# A release may have completed between the first read and acquiring this lock.
load_release
trap release_error ERR

case "$COMMAND" in
  deploy)
    migrate=false
    if (($#)); then
      [[ $# == 1 && "$1" == --migrate ]] || fail 'deploy 只支持 --migrate 参数。'
      migrate=true
    fi
    revision="$(git rev-parse --short HEAD 2>/dev/null || printf 'source')"
    export DAZZY_IMAGE="dazzy-api:${revision}-$(date -u '+%Y%m%d%H%M%S')-${RANDOM}"
    log "构建镜像：$DAZZY_IMAGE（保留旧版本）"
    compose build api
    preflight
    run_job python manage.py migrate --plan
    if [[ "$migrate" == true ]]; then
      log '进入维护窗口执行迁移；请确保已有可恢复的数据库备份。'
      stop_services
      run_job python manage.py migrate --noinput
    else
      # Failure here leaves the old release untouched. No service migrates on startup.
      run_job python manage.py migrate --check || fail '迁移检查未通过；若有待执行迁移，请先备份，再使用 deploy --migrate。'
      stop_services
    fi
    activate
    save_release
    log "发布成功：$DAZZY_IMAGE"
    ;;
  rollback)
    [[ $# -le 1 ]] || fail 'rollback 最多指定一个镜像名。'
    target="${1:-}"
    if [[ -z "$target" && -f "$STATE_DIR/previous-image" ]]; then
      target="$(< "$STATE_DIR/previous-image")"
    fi
    [[ -n "$target" ]] && valid_image "$target" || fail '没有可回退版本，请提供保留的镜像名。'
    export DAZZY_IMAGE="$target"
    log '回退代码版本；数据库和环境配置保持现状。'
    preflight
    run_job python manage.py migrate --check
    stop_services
    activate
    save_release
    log "已回退到：$DAZZY_IMAGE"
    ;;
  start)
    [[ $# == 0 ]] || fail 'start 不接受参数。'
    [[ -n "$current_image" ]] || fail '尚无成功发布记录，请使用 deploy。'
    preflight
    run_job python manage.py migrate --check
    activate
    ;;
  stop) [[ $# == 0 ]] || fail 'stop 不接受参数。'; stop_services ;;
  check)
    [[ $# == 0 ]] || fail 'check 不接受参数。'
    preflight
    run_job python manage.py migrate --check
    ;;
  manage)
    (($#)) || fail '请填写 Django 管理命令。'
    require_image
    # Keep stdin/TTY so changepassword can prompt without exposing passwords in arguments.
    compose run --rm --no-deps --pull never api python manage.py "$@"
    ;;
esac
