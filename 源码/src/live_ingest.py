"""Resumable real embedding and Milvus ingest for locally converted PDFs."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import yaml

from pipeline import DEFAULT_CONFIG_PATH, _resolve_config_paths
from vectorize import vectorize_all


def _load_state(path: Path, collection: str, reset: bool) -> dict:
    if reset or not path.exists():
        return {
            "collection": collection,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "completed": [],
            "failures": {},
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("collection") != collection:
        raise ValueError("Ingest state belongs to a different Milvus collection")
    return state


def run_batch(config: dict, run_id: str, batch_size: int, reset: bool) -> dict:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    api_key = os.environ.get("EMBEDDING_API_KEY")
    if not api_key:
        raise ValueError("EMBEDDING_API_KEY is not configured")

    source_dir = Path(config["paths"]["pdf_source_dir"]).resolve()
    output_dir = Path(config["paths"]["md_output_dir"]).resolve()
    collection = config["milvus"]["collection"]
    state_path = output_dir / "live_ingest" / run_id / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(state_path, collection, reset)

    filenames = [path.name for path in sorted(source_dir.rglob("*.pdf"))]
    completed = set(state["completed"])
    failures = state["failures"]
    pending = [name for name in filenames if name not in completed and name not in failures]
    batch = pending[:batch_size]
    if not batch:
        state["complete"] = len(completed) == len(filenames)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "run_id": run_id,
            "collection": collection,
            "processed": 0,
            "completed": len(completed),
            "total": len(filenames),
            "failures": len(failures),
            "complete": state["complete"],
            "state_path": str(state_path),
        }

    print(f"=== Live ingest {run_id}: {len(batch)} PDFs ===")
    results = vectorize_all(config, api_key, rebuild=reset, filenames=set(batch))
    for result in results:
        name = result["pdf_filename"]
        if result.get("success"):
            completed.add(name)
            failures.pop(name, None)
        else:
            failures[name] = result.get("error", "Unknown error")

    state["completed"] = sorted(completed)
    state["failures"] = failures
    state["last_batch_at"] = datetime.now().isoformat(timespec="seconds")
    state["complete"] = len(completed) == len(filenames)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "run_id": run_id,
        "collection": collection,
        "processed": len(batch),
        "completed": len(completed),
        "total": len(filenames),
        "failures": len(failures),
        "complete": state["complete"],
        "state_path": str(state_path),
    }
    print("Summary:", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="分批真实写入本地 PDF 向量")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--run-id", required=True, help="可续跑的批次标识")
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--reset", action="store_true", help="重建目标集合并重置本批次状态")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _resolve_config_paths(config, config_path)
    run_batch(config, args.run_id, args.batch_size, args.reset)


if __name__ == "__main__":
    main()
