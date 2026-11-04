#!/usr/bin/env python3
"""
retry_failed.py -- 重试 MongoDB 中的失败记录。

用法:
  python retry_failed.py                  # 重试所有失败记录
  python retry_failed.py --type parsing   # 仅重试解析失败的
  python retry_failed.py --type vectorize # 仅重试向量化失败的
  python retry_failed.py --dry-run        # 仅查看，不实际重试
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from mongodb_client import MongoTracker
from pdf2md import convert_all as pdf2md_convert_all
from vectorize import vectorize_all

console = Console()
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_config_paths(config: dict, config_path: str | Path) -> None:
    config_dir = Path(config_path).expanduser().resolve().parent
    for key in ("pdf_source_dir", "md_output_dir"):
        value = Path(config["paths"][key]).expanduser()
        config["paths"][key] = str(value if value.is_absolute() else config_dir / value)


def retry(config_path: str, fail_type: str | None, dry_run: bool, api_key: str):
    config = load_config(config_path)
    _resolve_config_paths(config, config_path)
    mongo_cfg = config["mongodb"]
    paths_cfg = config["paths"]
    perf_cfg = config.get("performance", {})
    mongo_uri = os.environ.get("MONGO_URI", mongo_cfg["uri"])
    mongo_db = mongo_cfg["database"]

    tracker = MongoTracker(mongo_uri, mongo_db)

    try:
        # 查询失败记录
        failed = tracker.get_failed_records(fail_type)
        if not failed:
            console.print("[green]没有失败记录。[/green]")
            return

        console.print(f"找到 [bold red]{len(failed)}[/bold red] 条失败记录")

        # 展示
        table = Table(title="失败记录")
        table.add_column("文件名", style="cyan", max_width=50)
        table.add_column("状态", style="red")
        table.add_column("错误信息", max_width=60)

        for r in failed:
            err = r.get("parse_error") or r.get("vectorize_error") or ""
            table.add_row(r["pdf_filename"], r["status"], err[:120])

        console.print(table)

        if dry_run:
            console.print("[yellow]Dry-run 模式，不执行重试。[/yellow]")
            return

        # 分类处理
        parse_failed = [r for r in failed if r["status"] == "parsing_failed"]
        vec_failed = [r for r in failed if r["status"] == "vectorize_failed"]

        # 解析失败的：完全重置
        for r in parse_failed:
            tracker.reset_to_pending(r["pdf_filename"])
            console.print(f"  [yellow]↻[/yellow] 重置: {r['pdf_filename']}")

        # 向量化失败的：仅重置向量化状态（保留 MD）
        for r in vec_failed:
            tracker.reset_vectorize_only(r["pdf_filename"])
            console.print(f"  [yellow]↻[/yellow] 重置向量化: {r['pdf_filename']}")

        if parse_failed:
            console.print(f"\n重新解析 {len(parse_failed)} 个文件...")
            parse_filenames = {r["pdf_filename"] for r in parse_failed}
            pdf_source_dir = paths_cfg["pdf_source_dir"]
            md_output_dir = paths_cfg["md_output_dir"]
            workers = perf_cfg.get("pdf_workers", 0) or os.cpu_count() or 4
            parse_start_map = {}

            for filename in parse_filenames:
                parse_start_map[filename] = datetime.now(timezone.utc)
                tracker.mark_parse_start(filename)

            results = pdf2md_convert_all(
                Path(pdf_source_dir), Path(md_output_dir), workers, parse_filenames
            )
            for r in results:
                if r["success"]:
                    tracker.mark_parse_success(
                        r["pdf_filename"], r["md_chars"],
                        parse_start_map.get(r["pdf_filename"]),
                    )
                    console.print(f"  [green]✓[/green] {r['pdf_filename']}")
                else:
                    tracker.mark_parse_failed(
                        r["pdf_filename"], r["message"], "ParseError",
                        parse_start_map.get(r["pdf_filename"]),
                    )
                    console.print(f"  [red]✗[/red] {r['pdf_filename']}: {r['message']}")

        if vec_failed or parse_failed:
            vec_names = {r["pdf_filename"] for r in vec_failed}
            if parse_failed:
                vec_names |= {r["pdf_filename"] for r in parse_failed}

            console.print(f"\n重新向量化 {len(vec_names)} 个文件...")
            vec_start_map = {}
            for filename in vec_names:
                vec_start_map[filename] = datetime.now(timezone.utc)
                tracker.mark_vectorize_start(filename)

            vec_results = vectorize_all(config, api_key, rebuild=False, filenames=vec_names)

            for r in vec_results:
                if r.get("skipped"):
                    continue
                if r["success"]:
                    tracker.mark_vectorize_success(
                        r["pdf_filename"], r["chunk_count"], config["milvus"]["collection"],
                        vec_start_map.get(r["pdf_filename"]),
                    )
                    console.print(f"  [green]✓[/green] {r['pdf_filename']} ({r['chunk_count']} chunks)")
                else:
                    tracker.mark_vectorize_failed(
                        r["pdf_filename"], r.get("error", "Unknown"),
                        vec_start_map.get(r["pdf_filename"]),
                    )
                    console.print(f"  [red]✗[/red] {r['pdf_filename']}: {r.get('error', '')}")

    finally:
        tracker.close()


def main():
    parser = argparse.ArgumentParser(description="重试失败记录")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--type", choices=["parsing", "vectorize"],
                        help="仅重试指定类型")
    parser.add_argument("--dry-run", action="store_true", help="仅查看，不执行")
    args = parser.parse_args()

    api_key = os.environ.get("EMBEDDING_API_KEY")
    if not api_key and not args.dry_run:
        console.print("[red]EMBEDDING_API_KEY 环境变量未设置。[/red]")
        sys.exit(1)

    retry(args.config, args.type, args.dry_run, api_key)


if __name__ == "__main__":
    main()
