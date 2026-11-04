# PDF 向量化流水线服务

[English](README.en.md) | 简体中文

将 PDF 文献转为 Markdown、清洗并按章节和语义单元切分，调用 embedding 服务生成向量，写入 Milvus，并在 MongoDB 记录进度与失败项。

## 主要功能

- 按处理状态进行增量扫描、解析、向量化及失败重试。
- 在章节内生成切片并记录来源、字符偏移和章节路径。
- 包含监控、模拟处理、数据迁移和恢复辅助脚本。

## 源码与运行入口

`源码/src/` 主流水线；`源码/config/config.yaml` 配置；`源码/scripts/` 运行/迁移工具；`源码/deploy/` 构建材料；`源码/tests/` 测试。

Python、PDF 解析依赖、MongoDB、Milvus 及兼容的 embedding API。安装 `源码/requirements.txt`，先把源 PDF 和输出目录改成自己的路径。

入口 `源码/src/pipeline.py`。从 `源码/` 查看参数：`python src/pipeline.py --help`；真实运行会读写数据库和向量库。

## 配置

`MONGO_URI` 可覆盖 YAML 中的本机 MongoDB 示例；向量服务 Key 使用 `EMBEDDING_API_KEY`。本项目不会自动加载 `.env`，需在调用进程或 Compose 中显式注入。

配置示例位于源码中的 `.env.example`。真实凭证只保存在本机环境或不跟踪的 `.env` 中。`127.0.0.1` 与 `example.invalid` 为本地/不可解析示例地址，部署前替换为自己的配置。

## 验证与边界

历史数据来自农业育种场景，配置中的集合名与字段仍可体现该来源；不代表只能处理这一领域。迁移、恢复和真实 PDF 全链路未在本轮执行。

本目录于 2026-11-04 完成公开范围整理与静态核验。代码发布不等于已部署或全部功能可运行。配置与依赖准备好后，应再执行适用于目标环境的测试。

公开范围及 GitHub/Gitee 接手规则见[仓库发布说明](仓库发布说明.md)。资料库原有的文档、会话、经验和管理记录继续保留在本目录，只供本地知识库使用。
