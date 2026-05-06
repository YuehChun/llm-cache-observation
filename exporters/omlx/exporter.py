"""Filesystem-based Prometheus exporter for omlx 3-tier cache.

omlx has no /metrics endpoint of its own; its admin API requires a session.
This exporter observes the cache externally:

  Layer       Source                                        Metric
  -----       ------                                        ------
  Hot cache   omlx process RSS                              omlx_process_rss_bytes
  Paged SSD   ~/.omlx/paged-ssd-cache/ disk usage           omlx_paged_ssd_cache_bytes
              file count (each file = 1 cache block)        omlx_paged_ssd_cache_blocks
              boundary snapshot count                       omlx_paged_ssd_boundary_snapshots
  Process     general health                                omlx_process_cpu_percent
                                                            omlx_process_open_files
                                                            omlx_up

Run with:
    python exporters/omlx/exporter.py --port 9105 --cache-dir ~/.omlx/paged-ssd-cache
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import psutil
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
    start_http_server,
)


UP = Gauge("omlx_up", "1 if the omlx process is running")
PROC_RSS = Gauge("omlx_process_rss_bytes", "Resident set size of omlx process (proxy for hot cache occupancy)")
PROC_VMS = Gauge("omlx_process_vms_bytes", "Virtual memory size of omlx process")
PROC_CPU = Gauge("omlx_process_cpu_percent", "CPU usage percent of omlx process (last interval)")
PROC_FDS = Gauge("omlx_process_open_files", "Number of open file descriptors held by omlx")
PROC_THREADS = Gauge("omlx_process_threads", "Thread count")
PROC_UPTIME = Gauge("omlx_process_uptime_seconds", "Seconds since omlx process started")

SSD_BYTES = Gauge("omlx_paged_ssd_cache_bytes", "Disk usage of the paged SSD cache directory")
SSD_BLOCKS = Gauge("omlx_paged_ssd_cache_blocks", "Number of files in the paged SSD cache (= number of cached blocks)")
SSD_SHARDS = Gauge("omlx_paged_ssd_cache_shards_used", "Number of cache shard subdirectories that contain at least one block")
SSD_BOUNDARY = Gauge("omlx_paged_ssd_boundary_snapshots", "Number of files in the _boundary_snapshots subdirectory")
SSD_OLDEST_AGE = Gauge("omlx_paged_ssd_oldest_block_age_seconds", "Age (now - mtime) of the oldest block file")
SSD_NEWEST_AGE = Gauge("omlx_paged_ssd_newest_block_age_seconds", "Age (now - mtime) of the newest block file")
COLLECT_ERRORS = Counter("omlx_exporter_collect_errors_total", "Number of collection failures", ["reason"])


def find_omlx_process() -> psutil.Process | None:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except psutil.NoSuchProcess:
            continue
        if any("omlx" in str(arg) for arg in cmdline) and any("serve" in str(arg) for arg in cmdline):
            return proc
    return None


def collect_process_metrics() -> None:
    proc = find_omlx_process()
    if proc is None:
        UP.set(0)
        return
    try:
        with proc.oneshot():
            mem = proc.memory_info()
            PROC_RSS.set(mem.rss)
            PROC_VMS.set(mem.vms)
            PROC_CPU.set(proc.cpu_percent(interval=0))  # cumulative since last call
            try:
                PROC_FDS.set(proc.num_fds())
            except (psutil.AccessDenied, AttributeError):
                pass
            PROC_THREADS.set(proc.num_threads())
            PROC_UPTIME.set(time.time() - proc.create_time())
        UP.set(1)
    except psutil.NoSuchProcess:
        UP.set(0)
        COLLECT_ERRORS.labels("process_disappeared").inc()


def collect_cache_metrics(cache_dir: Path) -> None:
    if not cache_dir.exists():
        SSD_BYTES.set(0)
        SSD_BLOCKS.set(0)
        SSD_SHARDS.set(0)
        SSD_BOUNDARY.set(0)
        return

    total_bytes = 0
    total_blocks = 0
    shards_used = 0
    boundary_count = 0
    oldest_mtime: float | None = None
    newest_mtime: float | None = None

    try:
        for shard in cache_dir.iterdir():
            if not shard.is_dir():
                continue
            shard_has_files = False
            for f in shard.rglob("*"):
                try:
                    if not f.is_file():
                        continue
                    st = f.stat()
                except (OSError, FileNotFoundError):
                    continue
                if shard.name == "_boundary_snapshots":
                    boundary_count += 1
                else:
                    total_blocks += 1
                    shard_has_files = True
                total_bytes += st.st_size
                if oldest_mtime is None or st.st_mtime < oldest_mtime:
                    oldest_mtime = st.st_mtime
                if newest_mtime is None or st.st_mtime > newest_mtime:
                    newest_mtime = st.st_mtime
            if shard_has_files and shard.name != "_boundary_snapshots":
                shards_used += 1
    except OSError:
        COLLECT_ERRORS.labels("cache_dir_read").inc()

    SSD_BYTES.set(total_bytes)
    SSD_BLOCKS.set(total_blocks)
    SSD_SHARDS.set(shards_used)
    SSD_BOUNDARY.set(boundary_count)
    now = time.time()
    if oldest_mtime is not None:
        SSD_OLDEST_AGE.set(max(0.0, now - oldest_mtime))
    else:
        SSD_OLDEST_AGE.set(0)
    if newest_mtime is not None:
        SSD_NEWEST_AGE.set(max(0.0, now - newest_mtime))
    else:
        SSD_NEWEST_AGE.set(0)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9105)
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.path.expanduser("~/.omlx/paged-ssd-cache")),
    )
    p.add_argument("--interval", type=float, default=10.0, help="seconds between collections")
    args = p.parse_args()

    start_http_server(args.port)
    print(f"omlx exporter listening on :{args.port}, watching {args.cache_dir}")
    while True:
        try:
            collect_process_metrics()
            collect_cache_metrics(args.cache_dir)
        except Exception as exc:  # pragma: no cover
            COLLECT_ERRORS.labels(type(exc).__name__).inc()
            print(f"collect error: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
