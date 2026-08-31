#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-tg-monitor}"
CONTAINER_NAME="${CONTAINER_NAME:-tg_msg_monitor}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8010/}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
IMAGE_RETENTION="${IMAGE_RETENTION:-168h}"
LOCK_FILE="${UPDATE_LOCK_FILE:-/var/lock/tgmsgmonitor-update.lock}"

log() {
  printf '[tgMsgMonitor update] %s\n' "$*"
}

on_error() {
  local exit_code=$?
  log "更新失败（exit=${exit_code}），未执行更新后清理。"
}
trap on_error ERR

command -v docker >/dev/null
command -v curl >/dev/null
command -v flock >/dev/null

mkdir -p "$(dirname -- "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "已有另一个更新流程正在运行，拒绝并发更新。" >&2
  exit 75
fi

cd "$COMPOSE_DIR"
docker info >/dev/null
docker compose config --quiet

before_container_id="$(docker compose ps -q "$SERVICE_NAME" 2>/dev/null || true)"
before_image_id=""
if [[ -n "$before_container_id" ]]; then
  before_image_id="$(docker inspect --format '{{.Image}}' "$before_container_id")"
fi

remove_stale_named_container() {
  if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    return 0
  fi

  local running working_dir compose_service
  running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")"
  if [[ "$running" == "true" ]]; then
    return 0
  fi

  working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER_NAME" 2>/dev/null || true)"
  compose_service="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$CONTAINER_NAME" 2>/dev/null || true)"
  if [[ "$working_dir" != "$COMPOSE_DIR" || "$compose_service" != "$SERVICE_NAME" ]]; then
    log "发现停止的 ${CONTAINER_NAME}，但它不属于当前 Compose 项目；拒绝自动删除。" >&2
    return 1
  fi

  log "删除当前 Compose 项目遗留的停止容器 ${CONTAINER_NAME}。"
  docker rm "$CONTAINER_NAME" >/dev/null
}

log "从 Compose 配置的镜像仓库拉取 ${SERVICE_NAME}。"
docker compose pull "$SERVICE_NAME"
remove_stale_named_container

log "以 no-build 模式重建服务，并清理同一 Compose 项目的孤儿容器。"
docker compose up -d --no-build --remove-orphans "$SERVICE_NAME"

deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
healthy=0
while (( SECONDS < deadline )); do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null; then
    healthy=1
    break
  fi
  sleep 2
done

if (( healthy == 0 )); then
  log "健康检查超时：${HEALTH_URL}" >&2
  docker compose ps "$SERVICE_NAME" || true
  exit 1
fi

after_container_id="$(docker compose ps -q "$SERVICE_NAME" 2>/dev/null || true)"
if [[ -z "$after_container_id" ]]; then
  log "健康检查通过但无法定位 Compose 容器，停止清理以避免误删。" >&2
  exit 1
fi
after_image_id="$(docker inspect --format '{{.Image}}' "$after_container_id")"

log "健康检查通过；before_image=${before_image_id:-none} after_image=${after_image_id}。"

# 只清理超过保留窗口的 dangling 镜像；保留最近版本作为回滚窗口。
log "清理超过 ${IMAGE_RETENTION} 的无标签旧镜像。"
docker image prune --force --filter "until=${IMAGE_RETENTION}"

# 生产更新使用 pull/no-build，未使用的构建缓存不承担回滚职责，可以全部回收。
log "清理未使用的 BuildKit 构建缓存。"
docker builder prune --all --force

# 仅按当前 Compose 工作目录过滤，避免影响其他项目的运行中容器。
log "清理当前 Compose 项目的停止容器。"
docker container prune --force --filter "label=com.docker.compose.project.working_dir=${COMPOSE_DIR}"

log "更新完成，旧容器/镜像清理步骤已执行。"
