#!/bin/bash
# run.sh -- 构建并运行 PDF Pipeline Docker 镜像

set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="pdf-pipeline"
IMAGE_TAG="${IMAGE_TAG:-latest}"
CONFIG_FILE="${CONFIG_FILE:-${PROJECT_ROOT}/config/config.yaml}"
MONITOR_PORT="${MONITOR_PORT:-18083}"
PDF_SOURCE_DIR="${PDF_SOURCE_DIR:-${PROJECT_ROOT}/../农科PDF测试数据}"
MD_OUTPUT_DIR="${MD_OUTPUT_DIR:-${PROJECT_ROOT}/../test_output}"

if [ ! -f "${CONFIG_FILE}" ]; then
    echo "错误: 配置文件不存在: ${CONFIG_FILE}"
    exit 1
fi

if [ -z "$EMBEDDING_API_KEY" ]; then
    echo "错误: EMBEDDING_API_KEY 环境变量未设置"
    echo "用法: export EMBEDDING_API_KEY=sk-xxx && bash scripts/run.sh"
    exit 1
fi

echo "=== 构建镜像: ${IMAGE_NAME}:${IMAGE_TAG} ==="
docker build -f "${PROJECT_ROOT}/deploy/Dockerfile" -t "${IMAGE_NAME}:${IMAGE_TAG}" "${PROJECT_ROOT}"

echo ""
echo "=== 运行流水线 ==="
docker run --rm \
    -e EMBEDDING_API_KEY="${EMBEDDING_API_KEY}" \
    -p "${MONITOR_PORT}:18080" \
    -v "${PDF_SOURCE_DIR}:/农科PDF测试数据:ro" \
    -v "${MD_OUTPUT_DIR}:/test_output" \
    -v "${CONFIG_FILE}:/app/config/config.yaml:ro" \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    --config /app/config/config.yaml --auto-retry

echo ""
echo "=== 完成 ==="
