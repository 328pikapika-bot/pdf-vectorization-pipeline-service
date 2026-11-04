#!/usr/bin/env python3
"""
server.py -- 轻量级进度监控 HTTP 服务。

启动: python server.py [--port 18080]
访问: http://localhost:18080
"""

import argparse
from html import escape
import json
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import yaml
from pymongo import MongoClient


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF Pipeline 进度监控</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}}
  h1{{font-size:22px;margin-bottom:8px;color:#58a6ff}}
  .subtitle{{color:#8b949e;font-size:13px;margin-bottom:20px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center}}
  .card .num{{font-size:32px;font-weight:700}}
  .card .label{{font-size:12px;color:#8b949e;margin-top:4px}}
  .progress-bar{{background:#21262d;border-radius:6px;height:24px;overflow:hidden;margin-bottom:20px}}
  .progress-fill{{background:linear-gradient(90deg,#238636,#3fb950);height:100%;transition:width .5s;display:flex;align-items:center;justify-content:flex-end;padding-right:8px;font-size:12px;font-weight:600;color:#fff}}
  .fail-box{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px}}
  .fail-box h2{{font-size:16px;color:#f85149;margin-bottom:10px}}
  .fail-row{{font-size:12px;padding:4px 0;border-bottom:1px solid #21262d;display:flex;gap:12px}}
  .fail-row .fname{{color:#58a6ff;flex:1}}
  .fail-row .ferr{{color:#f0883e}}
  .done-banner{{background:#238636;color:#fff;text-align:center;padding:16px;border-radius:8px;font-size:18px;font-weight:700;margin-bottom:20px}}
  .footer{{text-align:center;color:#484f58;font-size:11px;margin-top:20px}}
</style>
</head>
<body>
<h1>PDF Pipeline — 进度监控</h1>
<div class="subtitle">{timestamp} | 每 10 秒自动刷新</div>

{done_banner}

<div class="cards">
  <div class="card"><div class="num" style="color:#58a6ff">{total}</div><div class="label">总计 PDF</div></div>
  <div class="card"><div class="num" style="color:#d2a8ff">{pending}</div><div class="label">待处理</div></div>
  <div class="card"><div class="num" style="color:#d29922">{parsing}</div><div class="label">解析中</div></div>
  <div class="card"><div class="num" style="color:#79c0ff">{parsed}</div><div class="label">已解析</div></div>
  <div class="card"><div class="num" style="color:#a5d6ff">{vecing}</div><div class="label">向量化中</div></div>
  <div class="card"><div class="num" style="color:#3fb950">{done}</div><div class="label">已完成</div></div>
  <div class="card"><div class="num" style="color:#f85149">{failed}</div><div class="label">失败</div></div>
</div>

<div class="progress-bar">
  <div class="progress-fill" style="width:{pct}%">{pct}%</div>
</div>

{failures_html}

<div class="footer">PDF Pipeline Monitor v2.0 | Port {port}</div>
</body>
</html>"""


def load_config(path: str | None = None):
    config_path = path or os.environ.get("CONFIG_FILE", str(DEFAULT_CONFIG_PATH))
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {"mongodb": {"uri": "mongodb://localhost:27017", "database": "pdf_pipeline"}}


def get_stats(mongo_uri: str, mongo_db: str) -> dict:
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[mongo_db]
        col = db["pdf_records"]

        statuses = ["pending", "parsing", "parsed", "vectorizing",
                     "vectorized", "parsing_failed", "vectorize_failed"]
        counts = {}
        for s in statuses:
            counts[s] = col.count_documents({"status": s})

        # 失败记录
        failures = list(col.find(
            {"status": {"$in": ["parsing_failed", "vectorize_failed"]}},
            {"pdf_filename": 1, "status": 1, "parse_error": 1,
             "vectorize_error": 1, "_id": 0}
        ))

        # 平均耗时
        pipeline = [
            {"$match": {"status": "vectorized"}},
            {"$group": {
                "_id": None,
                "avg_parse_ms": {"$avg": "$parse_duration_ms"},
                "avg_vectorize_ms": {"$avg": "$vectorize_duration_ms"},
            }}
        ]
        agg = list(col.aggregate(pipeline))
        avg = agg[0] if agg else {"avg_parse_ms": 0, "avg_vectorize_ms": 0}

        client.close()
        return {
            "counts": counts,
            "failures": failures,
            "avg_parse_ms": round(avg.get("avg_parse_ms", 0) or 0, 0),
            "avg_vectorize_ms": round(avg.get("avg_vectorize_ms", 0) or 0, 0),
        }
    except Exception as e:
        return {
            "counts": {},
            "failures": [],
            "avg_parse_ms": 0,
            "avg_vectorize_ms": 0,
            "error": str(e),
        }


class MonitorHandler(BaseHTTPRequestHandler):
    mongo_uri = "mongodb://localhost:27017"
    mongo_db = "pdf_pipeline"
    port = 18080

    def log_message(self, format, *args):
        pass  # 安静模式

    def _serve_html(self):
        stats = get_stats(self.mongo_uri, self.mongo_db)
        c = stats["counts"]
        total = sum(c.values())
        done = c.get("vectorized", 0)
        failed = c.get("parsing_failed", 0) + c.get("vectorize_failed", 0)
        pct = round(done / total * 100, 1) if total > 0 else 0

        done_banner = ""
        if total > 0 and done + failed >= total and failed == 0:
            done_banner = '<div class="done-banner">全部处理完成！共 {total} 个 PDF，全部成功</div>'.format(total=total)
        elif total > 0 and done + failed >= total and failed > 0:
            done_banner = f'<div class="done-banner" style="background:#d29922">处理完成，但有 {failed} 个失败</div>'

        failures_html = ""
        if stats["failures"]:
            rows = ""
            for f in stats["failures"][:50]:
                err = f.get("parse_error") or f.get("vectorize_error") or ""
                filename = escape(str(f.get("pdf_filename", "")))
                err_text = escape(str(err)[:100])
                rows += (
                    f'<div class="fail-row"><span class="fname">{filename}</span>'
                    f'<span class="ferr">{err_text}</span></div>'
                )
            more_html = ""
            if len(stats["failures"]) > 50:
                more_html = '<div style="color:#8b949e;margin-top:8px">... 只显示前50条</div>'
            failures_html = (
                f'<div class="fail-box"><h2>失败文件 ({len(stats["failures"])})</h2>'
                f'{rows}{more_html}</div>'
            )

        html = HTML_TEMPLATE.format(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total=total, pending=c.get("pending", 0), parsing=c.get("parsing", 0),
            parsed=c.get("parsed", 0), vecing=c.get("vectorizing", 0),
            done=done, failed=failed, pct=pct,
            done_banner=done_banner, failures_html=failures_html, port=self.port,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_json(self):
        stats = get_stats(self.mongo_uri, self.mongo_db)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(stats, ensure_ascii=False, default=str).encode("utf-8"))

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/status":
            self._serve_json()
        else:
            self.send_response(404)
            self.end_headers()


def start_monitor(mongo_uri: str, mongo_db: str, port: int = 18080) -> HTTPServer:
    MonitorHandler.mongo_uri = mongo_uri
    MonitorHandler.mongo_db = mongo_db
    MonitorHandler.port = port
    server = HTTPServer(("0.0.0.0", port), MonitorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Monitor] 监控页面已启动: http://0.0.0.0:{port}")
    return server


def main():
    parser = argparse.ArgumentParser(description="启动进度监控 HTTP 服务")
    parser.add_argument("--port", "-p", type=int, default=18080)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    config = load_config(args.config)
    mongo_uri = os.environ.get("MONGO_URI", config["mongodb"]["uri"])
    mongo_db = config["mongodb"]["database"]

    MonitorHandler.mongo_uri = mongo_uri
    MonitorHandler.mongo_db = mongo_db
    MonitorHandler.port = args.port

    print(f"MongoDB: {mongo_db}")
    print(f"启动监控服务: http://0.0.0.0:{args.port}")
    server = HTTPServer(("0.0.0.0", args.port), MonitorHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
