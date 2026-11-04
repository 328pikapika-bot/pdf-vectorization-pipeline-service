# PDF Vectorization Pipeline Service

English | [简体中文](README.md)

Converts PDF literature to Markdown, cleans and chunks it by chapter and semantic unit, generates embeddings, writes them to Milvus, and tracks progress and failures in MongoDB.

## Capabilities

- Scans records incrementally by processing state, parses PDFs, vectorizes content, and retries failures.
- Stores source references, character offsets, and chapter paths for each chunk.
- Includes monitoring, simulation, migration, and recovery helpers.

## Code and setup

`源码/src/` holds the main pipeline, `源码/config/config.yaml` the configuration, `源码/scripts/` operational tools, `源码/deploy/` build material, and `源码/tests/` tests. Install `源码/requirements.txt` and provide PDF input/output paths, MongoDB, Milvus, and a compatible embedding API.

The entry is `源码/src/pipeline.py`. From `源码/`, run `python src/pipeline.py --help` to inspect options. A real run reads and writes databases and the vector store.

## Configuration

`MONGO_URI` overrides the local MongoDB example in YAML; `EMBEDDING_API_KEY` supplies the embedding service key. Use `源码/.env.example` as a template, but inject variables into the process or Compose explicitly: this project does not load `.env` automatically. Loopback and `example.invalid` addresses are placeholders.

## Verification and limits

The historical data came from an agricultural breeding use case; collection names and fields may still reflect it, while the processing pipeline is not limited to that domain. Migration, recovery, and a real end-to-end PDF run were not exercised in the 2026-11-04 public-code review.

See the [publication notes](仓库发布说明.md) for scope; local archives remain excluded.
