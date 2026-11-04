# 农业育种 PDF 向量化流水线

该项目将 PDF 转换为 Markdown，清理文本后切分、向量化并写入 Milvus；MongoDB 用于处理状态追踪。

## 目录

| 目录或文件 | 用途 |
|---|---|
| `src/` | Python 运行模块：转换、切分、向量化、重试和监控 |
| `config/config.yaml` | MongoDB、Milvus、向量模型、输入输出路径等配置 |
| `scripts/` | 本地 Docker 运行脚本和远程部署脚本 |
| `deploy/` | Dockerfile 和 Portainer Stack 配置 |
| `docs/` | 全文切分及入库字段设计 |
| `requirements.txt` | Python 依赖 |

## 运行

设置 `EMBEDDING_API_KEY` 后，从项目根目录执行：

```bash
bash scripts/run.sh
```

也可直接运行 Python：

```bash
python src/pipeline.py --config config/config.yaml
python src/retry_failed.py --config config/config.yaml --dry-run
python src/server.py --config config/config.yaml
python -m unittest discover -s tests
```

对本地 PDF 做完整的非写入模拟：

```bash
python src/pipeline.py --simulate --config config/config.yaml
```

模拟会真实执行 PDF 转 Markdown、清理和语义切分，以零向量校验 8 字段入库行；不会调用向量 API、MongoDB 或 Milvus。报告和本次转换的 Markdown 会写入 `test_output/semantic_simulation/<时间戳>/`。如果单次转换被中断，使用报告目录名继续同一批次：

```bash
python src/pipeline.py --simulate --simulate-run-id <时间戳> --simulate-batch-size 12
```

对已转换的 PDF 分批执行真实向量化和 Milvus 写入：

```bash
python src/live_ingest.py --run-id <批次标识> --batch-size 6
```

首次运行加 `--reset` 会重建配置中的目标集合。该命令需要 `EMBEDDING_API_KEY`，并会真实调用向量服务、写入 Milvus。

`config/config.yaml` 中的 PDF 输入目录和 Markdown 输出目录相对于配置文件解析；默认仍指向项目同级的 `农科PDF测试数据` 与 `test_output` 目录。向量化使用生产 `qwen3-embed` 模型，默认写入 `agri_breeding_pdf_chunk`，使用章节内语义切分和 15% 相邻重叠；旧的 `pdf_chunks` 集合不会被修改。

## 部署

- Docker 构建文件：[deploy/Dockerfile](deploy/Dockerfile)
- Portainer Stack：[deploy/portainer-stack-agri-breeding-pdf-memories.yml](deploy/portainer-stack-agri-breeding-pdf-memories.yml)
- 远程部署脚本：`bash scripts/deploy.sh`
