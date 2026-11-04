#!/usr/bin/env python3
"""
mongodb_client.py -- MongoDB 状态追踪模块。
封装所有对 pdf_records 集合的 CRUD 操作。
"""

from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError


class MongoTracker:
    """PDF 处理流水线的 MongoDB 追踪器。"""

    def __init__(self, uri: str, database: str):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self.db = self.client[database]
        self.collection: Collection = self.db["pdf_records"]
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.collection.create_index("pdf_filename", unique=True)
        self.collection.create_index("status")
        self.collection.create_index("created_at")

    # ---- 记录初始化 / 查询 ----

    def init_record(self, pdf_path: Path, source_dir: Path,
                    md_output_dir: str = "test_output") -> dict | None:
        """
        为 PDF 创建或获取记录。
        若已存在同文件名记录则直接返回；否则插入新记录（status=pending）。
        """
        filename = pdf_path.name
        existing = self.collection.find_one({"pdf_filename": filename})
        if existing:
            return existing

        doc = {
            "pdf_path": str(pdf_path),
            "pdf_filename": filename,
            "pdf_size_bytes": pdf_path.stat().st_size if pdf_path.exists() else 0,
            "md_path": str(
                Path(md_output_dir) / pdf_path.relative_to(source_dir).with_suffix(".md")
            ),
            "md_chars": 0,
            "status": "pending",
            "parse_start_at": None,
            "parse_end_at": None,
            "parse_duration_ms": 0,
            "parse_error": None,
            "parse_error_type": None,
            "vectorize_start_at": None,
            "vectorize_end_at": None,
            "vectorize_duration_ms": 0,
            "vectorize_error": None,
            "chunk_count": 0,
            "md_deleted": False,
            "milvus_collection": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        try:
            self.collection.insert_one(doc)
        except PyMongoError:
            # 并发插入时可能重复，查一次返回
            existing = self.collection.find_one({"pdf_filename": filename})
            if existing:
                return existing
            raise
        return doc

    # ---- 解析状态更新 ----

    def mark_parse_start(self, filename: str):
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "parsing",
                    "parse_start_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def mark_parse_success(self, filename: str, md_chars: int,
                           start_at: datetime | None = None):
        end_at = datetime.now(timezone.utc)
        start_at = start_at or end_at
        duration_ms = int((end_at - start_at).total_seconds() * 1000)
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "parsed",
                    "parse_end_at": end_at,
                    "parse_duration_ms": duration_ms,
                    "md_chars": md_chars,
                    "parse_error": None,
                    "parse_error_type": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def mark_parse_failed(
        self, filename: str, error: str, error_type: str,
        start_at: datetime | None = None
    ):
        end_at = datetime.now(timezone.utc)
        start_at = start_at or end_at
        duration_ms = int((end_at - start_at).total_seconds() * 1000)
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "parsing_failed",
                    "parse_end_at": end_at,
                    "parse_duration_ms": duration_ms,
                    "parse_error": error,
                    "parse_error_type": error_type,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    # ---- 向量化状态更新 ----

    def mark_vectorize_start(self, filename: str):
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "vectorizing",
                    "vectorize_start_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def mark_vectorize_success(
        self,
        filename: str,
        chunk_count: int,
        milvus_collection: str,
        start_at: datetime | None = None,
    ):
        end_at = datetime.now(timezone.utc)
        start_at = start_at or end_at
        duration_ms = int((end_at - start_at).total_seconds() * 1000)
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "vectorized",
                    "vectorize_end_at": end_at,
                    "vectorize_duration_ms": duration_ms,
                    "chunk_count": chunk_count,
                    "milvus_collection": milvus_collection,
                    "vectorize_error": None,
                    "md_deleted": False,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {"doi": "", "title": ""},
            },
        )

    def mark_vectorize_failed(
        self, filename: str, error: str, start_at: datetime | None = None
    ):
        end_at = datetime.now(timezone.utc)
        start_at = start_at or end_at
        duration_ms = int((end_at - start_at).total_seconds() * 1000)
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "vectorize_failed",
                    "vectorize_end_at": end_at,
                    "vectorize_duration_ms": duration_ms,
                    "vectorize_error": error,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    # ---- MD 删除标记 ----

    def mark_md_deleted(self, filename: str):
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "md_deleted": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    # ---- 批量查询 ----

    def get_filenames_by_status(
        self, status: str, milvus_collection: str | None = None,
    ) -> set[str]:
        """获取指定状态的全部文件名。"""
        query: dict = {"status": status}
        if milvus_collection is not None:
            query["milvus_collection"] = milvus_collection
        cursor = self.collection.find(
            query, {"pdf_filename": 1, "_id": 0}
        )
        return {doc["pdf_filename"] for doc in cursor}

    def get_record_by_md_path(self, md_rel_path: str) -> dict | None:
        return self.collection.find_one({"md_path": md_rel_path})

    def count_by_status(self, status: str) -> int:
        return self.collection.count_documents({"status": status})

    # ---- 统计摘要 ----

    def summary(self) -> dict:
        """返回各状态计数和耗时统计。"""
        pipeline = [
            {
                "$group": {
                    "_id": "$status",
                    "count": {"$sum": 1},
                    "avg_parse_ms": {"$avg": "$parse_duration_ms"},
                    "avg_vectorize_ms": {"$avg": "$vectorize_duration_ms"},
                }
            }
        ]
        result = {}
        for doc in self.collection.aggregate(pipeline):
            result[doc["_id"]] = {
                "count": doc["count"],
                "avg_parse_ms": round(doc["avg_parse_ms"] or 0, 0),
                "avg_vectorize_ms": round(doc["avg_vectorize_ms"] or 0, 0),
            }
        return result

    # ---- 失败重试 ----

    def get_failed_records(self, fail_type: str | None = None) -> list[dict]:
        """获取失败记录。fail_type: 'parsing' | 'vectorize' | None=全部。"""
        if fail_type == "parsing":
            status = "parsing_failed"
        elif fail_type == "vectorize":
            status = "vectorize_failed"
        else:
            status = {"$in": ["parsing_failed", "vectorize_failed"]}
        cursor = self.collection.find({"status": status})
        return list(cursor)

    def reset_to_pending(self, filename: str):
        """重置记录为 pending 状态，清除错误信息。"""
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "pending",
                    "parse_error": None,
                    "parse_error_type": None,
                    "vectorize_error": None,
                    "md_deleted": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def reset_vectorize_only(self, filename: str):
        """仅重置向量化状态（保留已解析的 MD），用于只重做 Phase 2。"""
        self.collection.update_one(
            {"pdf_filename": filename},
            {
                "$set": {
                    "status": "parsed",
                    "vectorize_error": None,
                    "vectorize_start_at": None,
                    "vectorize_end_at": None,
                    "vectorize_duration_ms": 0,
                    "chunk_count": 0,
                    "md_deleted": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    # ---- 清理 ----

    def close(self):
        self.client.close()
