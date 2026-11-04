#!/usr/bin/env python3
"""
pipeline.py -- PDF→MD→语义切分向量化完整流水线编排器。

用法:
  python src/pipeline.py                      # 增量处理
  python src/pipeline.py --rebuild            # 删除 Milvus 集合后重建
  python src/pipeline.py --auto-retry         # 完成后自动重试失败项
  python src/pipeline.py --no-server          # 不启动监控页面

环境变量:
  EMBEDDING_API_KEY  — 嵌入 API Bearer token（必填）
  MONGO_URI           — MongoDB 连接串（可选，覆盖 config.yaml）
  MONITOR_PORT        — 监控页面端口（默认 18080）
"""

import argparse
import os
import sys
import time
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
    """Resolve configured input and output paths relative to the config file."""
    config_dir = Path(config_path).expanduser().resolve().parent
    for key in ("pdf_source_dir", "md_output_dir"):
        value = Path(config["paths"][key]).expanduser()
        config["paths"][key] = str(value if value.is_absolute() else config_dir / value)


def _start_monitor(mongo_uri: str, mongo_db: str, port: int):
    """启动监控 HTTP 服务（后台线程）。"""
    from server import start_monitor
    start_monitor(mongo_uri, mongo_db, port)


def run_pipeline(config: dict, api_key: str, rebuild: bool = False,
                 auto_retry: bool = False, no_server: bool = False,
                 config_path: str | Path = DEFAULT_CONFIG_PATH):
    _resolve_config_paths(config, config_path)
    mongo_cfg = config["mongodb"]
    paths_cfg = config["paths"]
    perf_cfg = config.get("performance", {})
    milvus_cfg = config["milvus"]

    mongo_uri = os.environ.get("MONGO_URI", mongo_cfg["uri"])
    mongo_db = mongo_cfg["database"]
    monitor_port = int(os.environ.get("MONITOR_PORT", "18080"))

    pdf_source_dir = Path(paths_cfg["pdf_source_dir"]).resolve()
    md_output_dir = Path(paths_cfg["md_output_dir"]).resolve()
    workers = perf_cfg.get("pdf_workers", 0) or os.cpu_count() or 4

    # 创建输出目录
    md_output_dir.mkdir(parents=True, exist_ok=True)

    # ---- 启动监控服务 ----
    if not no_server:
        _start_monitor(mongo_uri, mongo_db, monitor_port)
        console.print(f"[bold cyan]监控页面:[/bold cyan] http://localhost:{monitor_port}")
        console.print(f"  (服务器 IP: http://0.0.0.0:{monitor_port})")

    # ---- 连接 MongoDB ----
    console.print(f"[bold]MongoDB:[/bold] {mongo_db}")
    tracker = MongoTracker(mongo_uri, mongo_db)

    try:
        # ---- 扫描 PDF ----
        if not pdf_source_dir.exists():
            console.print(f"[red]PDF 源目录不存在:[/red] {pdf_source_dir}")
            sys.exit(1)

        all_pdfs = list(pdf_source_dir.rglob("*.pdf"))
        console.print(f"扫描到 [bold]{len(all_pdfs)}[/bold] 个 PDF 文件")

        # ---- 确定处理策略 ----
        vectorized_set = tracker.get_filenames_by_status(
            "vectorized", milvus_cfg["collection"],
        )
        parsed_set = tracker.get_filenames_by_status("parsed")
        retry_vectorize = tracker.get_filenames_by_status("vectorizing")

        skip_all = vectorized_set
        skip_parse = parsed_set | retry_vectorize

        pdfs_to_parse = [p for p in all_pdfs
                         if p.name not in skip_all and p.name not in skip_parse]
        pdfs_to_vectorize = [p for p in all_pdfs if p.name not in skip_all]

        console.print(f"  已完成（跳过）: [dim]{len(skip_all)}[/dim]")
        console.print(f"  待解析: [bold yellow]{len(pdfs_to_parse)}[/bold yellow]")
        console.print(f"  待向量化: [bold yellow]{len(pdfs_to_vectorize)}[/bold yellow]")

        if not pdfs_to_parse and not pdfs_to_vectorize and parsed_set:
            console.print("[green]所有文件已处理完毕。[/green]")
            _print_summary(tracker)
            return

        # ---- 初始化 MongoDB 记录 ----
        for pdf_path in all_pdfs:
            if pdf_path.name not in skip_all:
                tracker.init_record(pdf_path, pdf_source_dir, str(md_output_dir))

        # ================================================
        # Phase 1: PDF → MD（并行）
        # ================================================
        if pdfs_to_parse:
            console.print(f"\n[bold]=== Phase 1: PDF → MD (workers={workers}) ===[/bold]")
            console.print(f"待转换: {len(pdfs_to_parse)} 文件")

            parse_filenames = {p.name for p in pdfs_to_parse}
            parse_start_map = {}
            for pdf_path in pdfs_to_parse:
                parse_start_map[pdf_path.name] = datetime.now(timezone.utc)
                tracker.mark_parse_start(pdf_path.name)

            phase_start = time.time()
            results = pdf2md_convert_all(
                pdf_source_dir, md_output_dir, workers, parse_filenames
            )
            phase_elapsed = time.time() - phase_start

            parse_ok = 0
            parse_fail = 0
            for r in results:
                if r["success"]:
                    tracker.mark_parse_success(
                        r["pdf_filename"], r["md_chars"],
                        parse_start_map.get(r["pdf_filename"], datetime.now(timezone.utc))
                    )
                    parse_ok += 1
                else:
                    tracker.mark_parse_failed(
                        r["pdf_filename"], r["message"], "ParseError",
                        parse_start_map.get(r["pdf_filename"], datetime.now(timezone.utc))
                    )
                    parse_fail += 1
                    console.print(f"  [red]✗ {r['pdf_filename']}: {r['message']}[/red]")

            console.print(
                f"Phase 1 完成: 成功 [green]{parse_ok}[/green], "
                f"失败 [red]{parse_fail}[/red], "
                f"耗时 {phase_elapsed:.1f}s"
            )

        # ================================================
        # Phase 2: MD → Vector
        # ================================================
        vectorize_pdf_names = {p.name for p in pdfs_to_vectorize}
        vectorize_pdf_names |= parsed_set
        vectorize_pdf_names |= retry_vectorize

        if vectorize_pdf_names:
            console.print(f"\n[bold]=== Phase 2: MD → Semantic Vector ===[/bold]")
            console.print(f"待向量化: {len(vectorize_pdf_names)} 文件")

            vec_start_map = {}
            for pdf_filename in vectorize_pdf_names:
                vec_start_map[pdf_filename] = datetime.now(timezone.utc)
                tracker.mark_vectorize_start(pdf_filename)

            phase_start = time.time()
            try:
                vec_results = vectorize_all(
                    config, api_key, rebuild, vectorize_pdf_names
                )
            except Exception as e:
                console.print(f"  [red]向量化阶段异常: {type(e).__name__}: {e}[/red]")
                vec_results = [
                    {
                        "pdf_filename": pdf_filename,
                        "success": False,
                        "chunk_count": 0,
                        "error": f"{type(e).__name__}: {e}",
                    }
                    for pdf_filename in vectorize_pdf_names
                ]
            phase_elapsed = time.time() - phase_start

            vec_ok = 0
            vec_fail = 0
            vec_skip = 0
            for r in vec_results:
                pdf_filename = r["pdf_filename"]
                if r.get("skipped"):
                    vec_skip += 1
                    continue
                if r["success"]:
                    tracker.mark_vectorize_success(
                        pdf_filename, r["chunk_count"], milvus_cfg["collection"],
                        vec_start_map.get(pdf_filename, datetime.now(timezone.utc)),
                    )
                    vec_ok += 1

                    # 向量化成功后删除 MD 文件
                    for f in md_output_dir.rglob("*.md"):
                        if f.stem + ".pdf" == pdf_filename:
                            try:
                                f.unlink()
                                tracker.mark_md_deleted(pdf_filename)
                            except Exception as e:
                                console.print(f"  [yellow]删除 MD 失败 {f}: {e}[/yellow]")
                            break
                else:
                    tracker.mark_vectorize_failed(
                        pdf_filename,
                        r.get("error", "Unknown"),
                        vec_start_map.get(pdf_filename, datetime.now(timezone.utc)),
                    )
                    vec_fail += 1
                    console.print(f"  [red]✗ {pdf_filename}: {r.get('error', '')}[/red]")

            console.print(
                f"Phase 2 完成: 成功 [green]{vec_ok}[/green], "
                f"失败 [red]{vec_fail}[/red], "
                f"跳过 [dim]{vec_skip}[/dim], "
                f"耗时 {phase_elapsed:.1f}s"
            )

        # ================================================
        # 自动重试
        # ================================================
        if auto_retry:
            console.print(f"\n[bold]=== 自动重试失败项 ===[/bold]")
            failed = tracker.get_failed_records()
            if failed:
                console.print(f"失败记录: {len(failed)} 条")
                from retry_failed import retry as do_retry
                # 递归重试最多 2 轮
                for attempt in range(2):
                    remaining = tracker.get_failed_records()
                    if not remaining:
                        console.print("[green]所有失败项已修复。[/green]")
                        break
                    console.print(f"重试轮次 {attempt + 1}/2: {len(remaining)} 条")
                    do_retry(config_path, None, False, api_key)
            else:
                console.print("[green]无失败项。[/green]")

        # ================================================
        # 汇总
        # ================================================
        _print_summary(tracker)

    finally:
        tracker.close()


def _print_summary(tracker: MongoTracker):
    console.print(f"\n[bold]=== MongoDB 状态汇总 ===[/bold]")
    summary = tracker.summary()

    if not summary:
        console.print("[dim]暂无记录[/dim]")
        return

    table = Table(title="处理状态分布")
    table.add_column("状态", style="cyan")
    table.add_column("数量", style="bold")
    table.add_column("平均解析耗时", style="green")
    table.add_column("平均向量化耗时", style="green")

    for status, info in sorted(summary.items()):
        parse_ms = info["avg_parse_ms"]
        vec_ms = info["avg_vectorize_ms"]
        table.add_row(
            status, str(info["count"]),
            f"{parse_ms / 1000:.1f}s" if parse_ms else "-",
            f"{vec_ms / 1000:.1f}s" if vec_ms else "-",
        )

    console.print(table)

    # 失败明细
    failed_records = list(tracker.collection.find(
        {"status": {"$in": ["parsing_failed", "vectorize_failed"]}},
        {"pdf_filename": 1, "status": 1, "parse_error": 1,
         "vectorize_error": 1, "_id": 0}
    ))
    if failed_records:
        console.print(f"\n[bold red]失败文件 ({len(failed_records)}):[/bold red]")
        for r in failed_records:
            err = r.get("parse_error") or r.get("vectorize_error") or ""
            console.print(f"  [{r['status']}] {r['pdf_filename']}: {err[:120]}")
    else:
        console.print("[bold green]全部成功！[/bold green]")


def main():
    parser = argparse.ArgumentParser(description="PDF → MD → 语义切分向量化流水线")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--rebuild", action="store_true", help="删除 Milvus 集合后重建")
    parser.add_argument("--auto-retry", action="store_true", help="完成后自动重试失败项")
    parser.add_argument("--no-server", action="store_true", help="不启动监控页面")
    parser.add_argument(
        "--simulate", action="store_true",
        help="本地执行 PDF 转换、语义切分和 8 字段写入模拟，不连接外部服务",
    )
    parser.add_argument("--simulate-run-id", help="继续指定的本地模拟批次")
    parser.add_argument(
        "--simulate-batch-size", type=int, default=12,
        help="单次模拟转换的 PDF 数量，默认 12",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.simulate:
        if args.rebuild or args.auto_retry:
            parser.error("--simulate cannot be used with --rebuild or --auto-retry")
        _resolve_config_paths(config, args.config)
        from simulation import run_simulation
        run_simulation(config, args.simulate_run_id, args.simulate_batch_size)
        return

    api_key = os.environ.get("EMBEDDING_API_KEY")
    if not api_key:
        console.print("[red]EMBEDDING_API_KEY 环境变量未设置。[/red]")
        console.print("用法: set EMBEDDING_API_KEY=sk-xxx && python src/pipeline.py")
        sys.exit(1)

    run_pipeline(config, api_key, rebuild=args.rebuild,
                 auto_retry=args.auto_retry, no_server=args.no_server,
                 config_path=args.config)


if __name__ == "__main__":
    main()
