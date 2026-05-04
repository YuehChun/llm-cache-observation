"""Wren AI service reverse proxy with Prometheus metrics.

Listens on :5556 and forwards every request to localhost:5555 (the
wren-ai-service container in the same pod). Records per-route latency,
status, and error counters. Exposes /metrics in Prometheus format.

Discovered automatically by Prometheus via the `kubernetes-pods` job since
the wren-ai-service pod carries prometheus.io/scrape=true annotations
pointing at port 5556.
"""
from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

UPSTREAM = os.environ.get("UPSTREAM_URL", "http://localhost:5555")

REQ_TOTAL = Counter(
    "wren_ai_requests_total",
    "Total HTTP requests through the proxy.",
    ["method", "route", "status"],
)
REQ_LATENCY = Histogram(
    "wren_ai_request_duration_seconds",
    "End-to-end request duration measured at the sidecar.",
    ["method", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
UPSTREAM_ERRORS = Counter(
    "wren_ai_upstream_errors_total",
    "Failures contacting the upstream wren-ai-service.",
    ["reason"],
)
INFLIGHT = Gauge(
    "wren_ai_inflight_requests",
    "Requests currently being proxied.",
)

app = FastAPI(title="wren-ai prom proxy")
client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup() -> None:
    global client
    client = httpx.AsyncClient(base_url=UPSTREAM, timeout=300.0)


@app.on_event("shutdown")
async def shutdown() -> None:
    if client is not None:
        await client.aclose()


@app.get("/metrics")
async def metrics() -> Response:
    return PlainTextResponse(
        generate_latest().decode(), media_type=CONTENT_TYPE_LATEST
    )


def route_label(path: str) -> str:
    """Collapse high-cardinality path params (e.g. /v1/asks/<query_id>)."""
    parts = path.strip("/").split("/")
    out: list[str] = []
    for p in parts:
        if len(p) >= 16 and any(c.isdigit() for c in p):
            out.append(":id")
        elif len(p) > 24:
            out.append(":id")
        else:
            out.append(p)
    return "/" + "/".join(out) if out else "/"


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    if path == "metrics":
        return await metrics()

    assert client is not None
    method = request.method
    route = route_label("/" + path)
    body = await request.body()
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    INFLIGHT.inc()
    t0 = time.perf_counter()
    try:
        upstream_resp = await client.request(
            method,
            "/" + path,
            params=request.query_params,
            content=body,
            headers=fwd_headers,
        )
        dur = time.perf_counter() - t0
        REQ_TOTAL.labels(method, route, str(upstream_resp.status_code)).inc()
        REQ_LATENCY.labels(method, route).observe(dur)
        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        }
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )
    except httpx.TimeoutException:
        UPSTREAM_ERRORS.labels("timeout").inc()
        REQ_TOTAL.labels(method, route, "504").inc()
        return PlainTextResponse("upstream timeout", status_code=504)
    except httpx.RequestError as exc:
        UPSTREAM_ERRORS.labels(type(exc).__name__).inc()
        REQ_TOTAL.labels(method, route, "502").inc()
        return PlainTextResponse(f"upstream error: {exc}", status_code=502)
    finally:
        INFLIGHT.dec()
