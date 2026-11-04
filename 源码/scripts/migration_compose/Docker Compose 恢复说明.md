# 农业育种 Milvus 向量库：Docker Compose 恢复说明

本包恢复 `agri_pdf_memories.agri_breeding_pdf_chunk`：1,413,886 条切片、28 个字段、原始 2560 维向量。恢复过程使用包内的 `milvus-backup 0.6.0`，在目标服务器重建 HNSW/COSINE 索引；不重新计算向量。

## 1. 接收方需要准备什么

- 一台 **Linux x86_64** 机器，已安装 Docker Engine 和 Docker Compose v2，当前用户能执行 `docker compose`。
- 首次启动能访问镜像仓库和 Python 包仓库，或已预先拉取完全相同的镜像及可用的 Python 依赖源。
- 建议预留至少 60 GiB 可用磁盘、32 GiB 内存；数据量和索引构建可能需要更多资源。7.65 GB 的 ZIP 不是运行时磁盘或内存需求。
- 默认宿主端口 `19534`（Milvus）和 `18003`（Attu）空闲；目标机器的防火墙按实际使用范围开放端口。

本 Compose 文件会自行启动目标 Milvus 2.6.9、etcd、MinIO、Attu，并创建对象存储数据桶和随机密钥。接收方无需单独安装或配置这些服务，也无需准备对象存储密钥。镜像及 Python 包不在 ZIP 中，镜像下载到接收方 Docker 本地，数据保存在该机器的 Docker 命名卷中。

### 镜像与来源

| 用途 | Compose 中固定的镜像 | 默认来源 |
|---|---|---|
| 随机密钥初始化 | `alpine:3.20` | Docker Hub |
| Milvus 目标服务 | `milvusdb/milvus:v2.6.9` | Docker Hub |
| 对象存储 | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | Docker Hub |
| 管理页面 | `zilliz/attu:v2.6.5` | Docker Hub |
| 元数据服务 | `quay.io/coreos/etcd:v3.5.25` | **Quay.io**，不是 Docker Hub |
| 恢复工具基础镜像 | `python:3.11-slim` | Docker Hub |

恢复工具镜像由包内 `工具及配置模板/Dockerfile.restore` 在本机构建；构建时从 PyPI 安装固定版本 `pymilvus==2.6.14`、`minio==7.2.20` 和 `PyYAML==6.0.3`，并校验包内 `milvus-backup 0.6.0` 的 SHA-256。不要自行把镜像标签改成 `latest` 或较旧版本。

正常情况下不需要手动下载镜像：第 3 节的命令会自动拉取。想提前确认网络，可在解压目录运行：

```bash
docker compose -f compose.yaml pull credentials etcd minio milvus attu
docker compose -f compose.yaml build restore
```

其中 etcd 从 `quay.io` 下载，其他基础镜像从 Docker Hub 下载；第二条命令另需访问 PyPI。提前拉取不是恢复的必需步骤。如果接收方只允许从 Docker Hub 下载，必须先让其网络能够访问 Quay.io，或由管理员提供完全相同的 etcd 镜像标签。

## 2. 校验与解压

将新版 ZIP 和对应的 `.zip.sha256` 文件放在同一目录；在该目录运行：

```bash
sha256sum -c agri_breeding_pdf_chunk_28fields_20261010_224223_compose_v2.zip.sha256
unzip agri_breeding_pdf_chunk_28fields_20261010_224223_compose_v2.zip
cd agri_breeding_pdf_chunk_28fields_20261010_224223_compose_v2
docker compose -f compose.yaml config --quiet
```

校验输出 `OK` 且 Compose 配置检查无报错后继续。整个解压目录需要保留，恢复工具会读取其中全部备份对象及校验资料。请不要只复制 `compose.yaml` 或只复制 `binlogs`。

## 3. 一条 Compose 命令恢复

在上面的解压目录运行：

```bash
docker compose -f compose.yaml run --build --rm restore
```

首次执行会拉取镜像、构建恢复工具镜像，然后按顺序启动密钥初始化、etcd、MinIO、数据桶初始化、Milvus 和 Attu。接着恢复工具依次：

1. 用包内 SHA-256 清单校验文件。
2. 确认目标是 Milvus 2.6.9，且同名集合尚不存在。
3. 上传 101 个备份对象到新 MinIO 的 `a-bucket/migration_backup/`。
4. 从备份恢复集合，重建 HNSW 索引并加载集合。
5. 核对 28 个字段、1,413,886 条逻辑记录、索引、示例元数据和向量查询。

成功时输出 `恢复检查通过`。命令的 `--rm` 仅清理一次性的恢复工具容器；Milvus、etcd、MinIO、Attu 仍运行，数据存在 Docker 命名卷中。恢复时间主要取决于目标机器的磁盘、CPU、内存和索引构建速度。注意：**同一个已成功恢复的项目不可重复执行这条命令**，脚本会拒绝覆盖已存在的集合。

## 4. 查看结果

```bash
docker compose -f compose.yaml ps
```

在浏览器打开 `http://目标服务器IP:18003` 进入 Attu；若提示填写连接地址，在 Attu 容器内部填写 `milvus:19530`。数据库为 `agri_pdf_memories`，集合为 `agri_breeding_pdf_chunk`。其他程序连接该机器的 Milvus 地址为 `目标服务器IP:19534`。

如 `19534` 或 `18003` 与同事机器上的其他服务冲突，可在首次执行前设置 `AGRI_MILVUS_PORT` 或 `AGRI_ATTU_PORT`，例如：

```bash
AGRI_MILVUS_PORT=19535 AGRI_ATTU_PORT=18004 docker compose -f compose.yaml run --build --rm restore
```

此例的访问端口随之变为 `19535` 和 `18004`，容器内部地址不变。

如需对全部 1,413,886 条记录逐条核对原 8 字段和新增 20 字段，可在基础恢复成功后另行执行：

```bash
docker compose -f compose.yaml run --rm restore python3 '/package/工具及配置模板/restore_auto.py' verify-full
```

全量核验可能花较长时间；不会重复恢复或重算向量。

## 5. 失败时怎么处理

- 拉取镜像超时：检查能否访问 Docker Hub 和 Quay.io。构建工具镜像失败则检查 PyPI 网络；解决下载问题后重新运行命令。若已进入备份上传阶段，先看下一条。
- 端口占用：在运行前按第 4 节更换宿主端口。
- 报目标集合已存在或备份前缀已存在：工具会停止，不覆盖。上传或恢复中断后，目标可能已有部分数据，请先查看日志和目标状态；不要盲目重复执行。
- 查看服务日志：`docker compose -f compose.yaml logs milvus minio etcd`。一次性 `restore` 容器的运行输出也应保存。
- **不要运行 `docker compose down -v`，不要删除相关 Docker 命名卷。**这样会删除接收方已恢复的数据。

包中的原始备份数据不会因恢复而修改；Compose 不连接 231 源服务器。原 PDF 与模型服务不在包中。此包做了文件和 ZIP 校验，但未在接收方的机器上完成实际恢复演练；接收方应以第 3 节的运行结果为准。
