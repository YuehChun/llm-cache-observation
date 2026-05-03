"""Benchmark vllm-metal under Wren AI-style prompts.

Usage:
  python scripts/bench.py --api-base http://localhost:8000/v1--questions scripts/wren_questions.txt \
    --out reports/2026-05-03/run.csv

Measures TTFT (time to first token) per request via streaming. Optionally polls
Prometheus metrics before & after to record cumulative cache stats.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from pathlib import Path

import requests

SCHEMA_DESC = """\
You are an expert SQL analyst. The database has the following PostgreSQL tables:

- customers(id, name, email, country, created_at)
- products(id, name, category, supplier_id, price, reorder_threshold, current_stock)
- suppliers(id, name, country, rating)
- orders(id, customer_id, order_date, ship_date, total_amount, payment_method, status)
- order_items(order_id, product_id, quantity, unit_price, discount_pct)
- sales_reps(id, name, region, hire_date)
- order_assignments(order_id, sales_rep_id)
- campaigns(id, name, start_date, end_date, discount_pct, channel)
- order_campaigns(order_id, campaign_id)
- returns(order_id, product_id, return_date, reason, quantity)

Generate ONLY a single PostgreSQL query that answers the user's question.
Do not include explanations, comments, or markdown fences. Return raw SQL.
"""


def load_questions(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def fetch_metrics(base_url: str) -> dict[str, float]:
    """Return current vllm: cumulative counters as a flat dict."""
    try:
        r = requests.get(f"{base_url.rstrip('/v1').rstrip('/')}/metrics", timeout=5)
    except requests.RequestException:
        return {}
    snapshot: dict[str, float] = {}
    for line in r.text.splitlines():
        if line.startswith("#") or not line.startswith("vllm:"):
            continue
        # parse "name{labels} value"
        try:
            name_part, val = line.rsplit(" ", 1)
            name = name_part.split("{", 1)[0]
            snapshot[name] = snapshot.get(name, 0.0) + float(val)
        except ValueError:
            pass
    return snapshot


def stream_request(api_base: str, model: str, system: str, user: str,
                   max_tokens: int = 256, temperature: float = 0.0) -> tuple[float, float, int]:
    """Send a streaming chat completion. Returns (ttft_s, total_s, output_tokens)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    t0 = time.perf_counter()
    ttft: float | None = None
    output_tokens = 0
    with requests.post(f"{api_base}/chat/completions", json=payload, stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith(b"data: "):
                if ttft is None:
                    ttft = time.perf_counter() - t0
                payload_text = line[6:]
                if payload_text == b"[DONE]":
                    break
                try:
                    chunk = json.loads(payload_text)
                    delta = chunk["choices"][0]["delta"].get("content") or ""
                    if delta:
                        output_tokens += 1  # rough: 1 SSE delta ~ 1 token
                except (json.JSONDecodeError, KeyError):
                    continue
    total = time.perf_counter() - t0
    return ttft or total, total, output_tokens


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api-base", default="http://localhost:8000/v1")
    p.add_argument("--model", default="qwen2.5-1.5b")
    p.add_argument("--questions", default="scripts/wren_questions.txt")
    p.add_argument("--out", required=True)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--repeat", type=int, default=1, help="repeat the question list this many times")
    p.add_argument("--label", default="default", help="run label written to csv")
    p.add_argument("--schema", choices=["short", "long"], default="short",
                   help="short = bench.SCHEMA_DESC; long = wren_long_schema.LONG_SCHEMA_DESC")
    args = p.parse_args()

    if args.schema == "long":
        from wren_long_schema import LONG_SCHEMA_DESC
        system_prompt = LONG_SCHEMA_DESC
    else:
        system_prompt = SCHEMA_DESC

    questions = load_questions(args.questions)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    pre = fetch_metrics(args.api_base)
    rows = []
    iter_idx = 0
    overall_t0 = time.perf_counter()
    for cycle in range(args.repeat):
        for q in questions:
            iter_idx += 1
            ttft, total, out_tokens = stream_request(
                args.api_base, args.model, system_prompt, q, max_tokens=args.max_tokens
            )
            rows.append({
                "label": args.label,
                "cycle": cycle,
                "idx": iter_idx,
                "question": q[:80],
                "ttft_s": round(ttft, 4),
                "total_s": round(total, 4),
                "out_tokens": out_tokens,
            })
            print(f"[{args.label}] {iter_idx:>3}  ttft={ttft:.3f}s  total={total:.3f}s  out={out_tokens}")
    overall_total = time.perf_counter() - overall_t0
    post = fetch_metrics(args.api_base)

    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summary_path = out.with_suffix(".summary.json")
    ttfts = [r["ttft_s"] for r in rows]
    summary = {
        "label": args.label,
        "n": len(rows),
        "overall_seconds": round(overall_total, 3),
        "ttft_p50": round(statistics.median(ttfts), 4),
        "ttft_p95": round(sorted(ttfts)[max(0, int(len(ttfts) * 0.95) - 1)], 4),
        "ttft_mean": round(statistics.mean(ttfts), 4),
        "out_tokens_total": sum(r["out_tokens"] for r in rows),
        "tokens_per_sec_avg": round(sum(r["out_tokens"] for r in rows) / overall_total, 2),
        "metrics_delta": {
            k: round(post.get(k, 0) - pre.get(k, 0), 4)
            for k in (
                "vllm:prefix_cache_hits_total",
                "vllm:prefix_cache_queries_total",
                "vllm:prompt_tokens_total",
                "vllm:prompt_tokens_cached_total",
                "vllm:generation_tokens_total",
            )
            if post.get(k) is not None
        },
    }
    summary["cache_hit_rate"] = (
        round(summary["metrics_delta"].get("vllm:prefix_cache_hits_total", 0)
              / max(summary["metrics_delta"].get("vllm:prefix_cache_queries_total", 1), 1), 4)
    )
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\ncsv     -> {out}")
    print(f"summary -> {summary_path}")


if __name__ == "__main__":
    main()
