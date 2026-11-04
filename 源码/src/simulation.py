"""Local end-to-end simulation without external database or embedding services."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pdf2md import convert_all
from vectorize import chunk_to_row, clean_text, split_document


SCHEMA_FIELDS = {
    "id", "pdf_index", "chunk_index", "text_embed",
    "text", "char_start", "char_end", "section_path",
}


def run_simulation(config: dict, run_id: str | None = None, batch_size: int = 12) -> dict:
    """Run PDF conversion and semantic chunking locally, simulating vector writes."""
    paths_cfg = config["paths"]
    chunk_cfg = config["chunking"]
    embedding_cfg = config["embedding"]
    workers = config.get("performance", {}).get("pdf_workers", 0) or 4

    source_dir = Path(paths_cfg["pdf_source_dir"]).resolve()
    output_root = Path(paths_cfg["md_output_dir"]).resolve()
    if batch_size <= 0:
        raise ValueError("simulate_batch_size must be greater than zero")
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / "semantic_simulation" / run_id
    state_path = output_dir / "simulation-state.json"

    if not source_dir.exists():
        raise FileNotFoundError(f"PDF source directory not found: {source_dir}")

    pdf_paths = sorted(source_dir.rglob("*.pdf"))
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found in: {source_dir}")

    state = {"conversion_failures": {}}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    conversion_failures = state.get("conversion_failures", {})

    pending_paths = [
        pdf_path
        for pdf_path in pdf_paths
        if not (output_dir / pdf_path.relative_to(source_dir).with_suffix(".md")).exists()
        and pdf_path.name not in conversion_failures
    ]
    batch_paths = pending_paths[:batch_size]

    print(f"=== Local semantic simulation: {len(pdf_paths)} PDFs ===")
    print(f"PDF source: {source_dir}")
    print(f"Simulation output: {output_dir}")
    print(f"Conversion batch: {len(batch_paths)} pending PDFs")
    conversion_results = convert_all(
        source_dir,
        output_dir,
        workers,
        {path.name for path in batch_paths},
    ) if batch_paths else []
    for result in conversion_results:
        if not result["success"]:
            conversion_failures[result["pdf_filename"]] = result["message"]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"conversion_failures": conversion_failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    placeholder_embedding = [0.0] * int(embedding_cfg["dimension"])
    document_reports = []
    total_clean_chars = 0
    total_chunks = 0
    chunk_lengths: list[int] = []
    same_section_pairs = 0

    for pdf_path in pdf_paths:
        md_path = output_dir / pdf_path.relative_to(source_dir).with_suffix(".md")
        conversion_success = md_path.exists()
        report = {
            "pdf_filename": pdf_path.name,
            "conversion_success": conversion_success,
            "conversion_message": conversion_failures.get(pdf_path.name, "Pending conversion"),
            "md_chars": md_path.stat().st_size if conversion_success else 0,
            "chunk_success": False,
            "clean_chars": 0,
            "chunk_count": 0,
            "same_section_pairs": 0,
            "error": None,
        }
        if not conversion_success:
            document_reports.append(report)
            continue

        try:
            md_text = md_path.read_text(encoding="utf-8")
            clean_md = clean_text(md_text)
            if not clean_md:
                raise ValueError("Markdown is empty after cleaning")

            chunks = split_document(
                clean_md,
                pdf_path.name,
                target_chars=int(chunk_cfg["target_chars"]),
                min_chars=int(chunk_cfg["min_chars"]),
                max_chars=int(chunk_cfg["max_chars"]),
                semantic_window=int(chunk_cfg["semantic_window_chars"]),
                overlap_ratio=float(chunk_cfg["overlap_ratio"]),
                excluded_headers=set(config.get("exclude_headers", [])),
            )
            if not chunks:
                raise ValueError("No semantic chunks generated")

            # Validate every simulated Milvus row without retaining vectors in memory.
            for chunk in chunks:
                if set(chunk_to_row(chunk, placeholder_embedding)) != SCHEMA_FIELDS:
                    raise ValueError("Simulated Milvus row does not match the eight-field schema")

            report["chunk_success"] = True
            report["clean_chars"] = len(clean_md)
            report["chunk_count"] = len(chunks)
            report["same_section_pairs"] = sum(
                1
                for previous, current in zip(chunks, chunks[1:])
                if previous.section_path == current.section_path
            )
            total_clean_chars += report["clean_chars"]
            total_chunks += report["chunk_count"]
            same_section_pairs += report["same_section_pairs"]
            chunk_lengths.extend(len(chunk.text) for chunk in chunks)
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        document_reports.append(report)

    pending_after_batch = [
        pdf_path
        for pdf_path in pdf_paths
        if not (output_dir / pdf_path.relative_to(source_dir).with_suffix(".md")).exists()
        and pdf_path.name not in conversion_failures
    ]
    converted = sum(item["conversion_success"] for item in document_reports)
    chunked = sum(item["chunk_success"] for item in document_reports)
    report = {
        "mode": "local_simulation",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "pdf_source_dir": str(source_dir),
        "markdown_output_dir": str(output_dir),
        "embedding_api_called": False,
        "mongodb_connected": False,
        "milvus_connected": False,
        "milvus_schema_fields": sorted(SCHEMA_FIELDS),
        "embedding_dimension_simulated": len(placeholder_embedding),
        "complete": not pending_after_batch,
        "pending_conversion_count": len(pending_after_batch),
        "pdf_count": len(pdf_paths),
        "converted_count": converted,
        "conversion_failed_count": len(conversion_failures),
        "chunked_count": chunked,
        "chunk_failed_count": converted - chunked,
        "total_clean_chars": total_clean_chars,
        "total_chunks": total_chunks,
        "chunk_length_min": min(chunk_lengths) if chunk_lengths else 0,
        "chunk_length_max": max(chunk_lengths) if chunk_lengths else 0,
        "chunk_length_avg": round(sum(chunk_lengths) / len(chunk_lengths), 2) if chunk_lengths else 0,
        "same_section_adjacent_pairs": same_section_pairs,
        "documents": document_reports,
    }
    report_path = output_dir / "simulation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Simulation summary ===")
    print(f"Converted: {converted}/{len(pdf_paths)}")
    print(f"Pending conversion: {len(pending_after_batch)}")
    print(f"Conversion failures: {len(conversion_failures)}")
    print(f"Semantic chunked: {chunked}/{converted}")
    print(f"Total chunks: {total_chunks}")
    print(f"Chunk length: min={report['chunk_length_min']}, avg={report['chunk_length_avg']}, max={report['chunk_length_max']}")
    print(f"Report: {report_path}")
    return report
