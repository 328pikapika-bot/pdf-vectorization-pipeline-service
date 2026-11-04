#!/bin/bash
# deploy.sh -- 构建镜像并部署到 231 服务器

set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="pdf-pipeline"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SERVER="127.0.0.1"
MONITOR_PORT="${MONITOR_PORT:-18080}"
ARCHIVE_PATH="${TMPDIR:-/tmp}/pdf-pipeline-${IMAGE_TAG}.tar.gz"

trap 'rm -f "${ARCHIVE_PATH}"' EXIT

if [ -z "$EMBEDDING_API_KEY" ]; then
    echo "错误: 请先设置 EMBEDDING_API_KEY"
    exit 1
fi

echo "=== 1. 构建镜像 ==="
docker build -f "${PROJECT_ROOT}/deploy/Dockerfile" -t "${IMAGE_NAME}:${IMAGE_TAG}" "${PROJECT_ROOT}"

echo ""
echo "=== 2. 导出镜像 ==="
docker save "${IMAGE_NAME}:${IMAGE_TAG}" | gzip > "${ARCHIVE_PATH}"

echo ""
echo "=== 3. 上传到 ${SERVER} ==="
scp "${ARCHIVE_PATH}" "chenshuang@${SERVER}:/tmp/pdf-pipeline.tar.gz"

echo ""
echo "=== 4. 服务器端加载并运行 ==="
ssh "chenshuang@${SERVER}" << ENDSSH
  docker load < /tmp/pdf-pipeline.tar.gz
  rm /tmp/pdf-pipeline.tar.gz

  mkdir -p /data/pdf-sources /data/md-output

  docker stop pdf-pipeline 2>/dev/null || true
  docker rm pdf-pipeline 2>/dev/null || true

  docker run -d --name pdf-pipeline \\
    --network milvus \\
    -e EMBEDDING_API_KEY="${EMBEDDING_API_KEY}" \\
    -p ${MONITOR_PORT}:18080 \\
    -v /data/pdf-sources:/data/pdf-sources:ro \\
    -v /data/md-output:/data/md-output \\
    -v /data/compose/1502/config.yaml:/app/config/config.yaml:ro \\
    ${IMAGE_NAME}:${IMAGE_TAG} \\
    --config /app/config/config.yaml --auto-retry

  echo ""
  echo "=== 容器已启动 ==="
  echo "监控页面: http://${SERVER}:${MONITOR_PORT}"
  docker ps --filter name=pdf-pipeline
ENDSSH

echo ""
echo "=== 部署完成 ==="
echo "监控页面: http://${SERVER}:${MONITOR_PORT}"
