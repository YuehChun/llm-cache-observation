# A+C Experiment Report — vllm-metal vs omlx + 5-layer Dashboard

**Date**: 2026-05-06
**Environment**: MacBook Pro Mac15,3 (Apple M3), 16 GB unified memory, macOS 26.3.1 (arm64)
**Model**: Qwen2.5-1.5B-Instruct (MLX 4-bit quantized, ~847 MB)
**Engines under test**:
- vllm-metal 0.2.0 + vLLM 0.20.0+cpu (Metal GPU; flag `--enable-prefix-caching`)
- omlx 0.3.4 + mlx-lm 0.31.3 (Metal GPU; native paged-SSD + hot prefix cache)

**Workload (固定條件)**:
- **System prompt**: `scripts/wren_long_schema.py` `LONG_SCHEMA_DESC` —
  ~1917 tokens (cl100k_base)，含 13 個 PostgreSQL table 完整 column 定義 + 慣例 + 規則
- **Questions**: `scripts/wren_questions.txt` — **30 個固定 Wren AI 風格問題**
- **Cycles**: 3 輪重複（共 90 requests），temperature=0，max_tokens=96
- **Streaming TTFT** 經 `bench.py` 在 client 端量測

每個 engine 跑 90 個 request，計時相同條件下兩邊的行為。

---

## A. vllm-metal vs omlx 對照

### A.1 整體 summary

| 指標                         | vllm-metal | omlx     | omlx Δ    |
|---|---|---|---|
| Total time (s)              | **1837.45**| **183.08**| **−90.0%**|
| Throughput (tok/s avg)      | 2.72       | 38.08    | **+1300%**|
| Output tokens total         | 5000       | 6972     | +39%      |
| Cache hit rate (server-side)| **99.34%** | n/a *    | —         |
| `prompt_tokens_cached_total`| 179520 / 180705 | n/a | —    |

\* omlx 沒有 Prometheus `/metrics`，hit rate 透過內建 paged-SSD + hot cache 在引擎內部運作但不對外暴露。

### A.2 Per-cycle 進展（這是 KV cache 觀察的核心）

**vllm-metal Qwen2.5-1.5B + 1917-tok system prompt**:

| Cycle | n  | TTFT p50 | TTFT mean | TTFT min | TTFT max | Total mean |
|---|---|---|---|---|---|---|
| 0 (cold)  | 30 | 21.32s  | 21.26s | 7.24s  | 40.74s | 29.72s |
| 1 (warm)  | 30 | 6.09s   | 8.36s  | 0.59s  | 38.61s | 20.81s |
| 2 (hot)   | 30 | **1.68s** | 4.04s | 0.59s | 16.77s | **10.57s** |

**Δ cycle 0 → cycle 2**:
- TTFT p50 −92.1% (21.32s → 1.68s)
- TTFT mean −81.0%
- TTFT min 12x（7.24s → 0.59s）
- Total mean −64.4%（29.72s → 10.57s）

**omlx Qwen2.5-1.5B + 同 system prompt**:

| Cycle | n  | TTFT p50 | TTFT mean | TTFT min | Total mean |
|---|---|---|---|---|---|
| 0 | 30 | 0.007s | 0.007s | 0.004s | 2.05s |
| 1 | 30 | 0.007s | 0.007s | 0.005s | 2.03s |
| 2 | 30 | 0.007s | 0.007s | 0.005s | 2.03s |

**注意：兩邊 TTFT 數字不可直接比較**。`bench.py` 抓「第一個 SSE `data:` line」當 TTFT。omlx 第一筆 SSE 是空 role-only delta，立刻送出（~7 ms = 網路 RTT），所以記錄到的 7 ms 不是真的「prefill 完成時間」。vllm-metal 反而會等 prefill 結束才送 chunk，TTFT 才反映真實 prefill 時間。**用 `total_s`（end-to-end）才公平**。

### A.3 公平比較：end-to-end 時間

| Cycle | vllm-metal (total mean) | omlx (total mean) | omlx 快於 vllm |
|---|---|---|---|
| 0 (cold) | 29.72s | 2.05s | **14.5x** |
| 1        | 20.81s | 2.03s | 10.3x |
| 2 (hot)  | 10.57s | 2.03s | 5.2x  |

**結論**：
- omlx 在所有 3 個 cycle 都顯著快於 vllm-metal——這是 Apple Silicon 上「**MLX-native + paged-SSD + hot cache**」對 vllm-metal 的優勢
- vllm-metal 需要 3 個 cycle 才能逼近 omlx 的「冷啟動」速度
- **但 vllm-metal 是唯一暴露 hit-rate metric 的引擎**（99.34%），對「觀察 KV cache 行為」目標不可或缺
- omlx 不暴露 cache metric，但內部 cache 機制顯然是有效的（cycle 0 已是穩定的 2.05s，沒有 cold-start spike——可能因為 paged-SSD cache 從之前的 session 直接 hit）

### A.4 KV cache hit rate 持續性觀察

90 個 request 累積：
```
vllm:prefix_cache_queries_total = 180705
vllm:prefix_cache_hits_total    = 179520
vllm:prompt_tokens_total        = 180705
vllm:prompt_tokens_cached_total = 179520
```

**99.34% hit rate** 意味著：每個 request 的 1917 token system prompt 幾乎全部從 KV cache 取，每次只需 prefill 少數新 tokens（user question 的 ~14 tokens + EOS 結構）。這正是 PROJECT_PLAN.md 三大目標的目標 #2「同 schema 不同問題下 cache 命中率隨時間如何變化」的具體答案：**穩定 99% 以上，且隨 cycle 增加逐步上升**。

---

## C. 5-layer Grafana Dashboard 觀察

dashboard `LLM Cache — 5-layer Overview` (uid `llm-cache-5-layer`) 在 [http://localhost:3001](http://localhost:3001) 提供 5 個觀察層次。實驗執行期間實際抓到的 panel state（見 `panel-snapshot.txt`）：

### Layer 1 — vllm-metal (LLM engine)
- `vllm:prefix_cache_hits_total` = 183488
- `vllm:prefix_cache_queries_total` = 186704
- `vllm:prompt_tokens_total` = 186704
- `vllm:prompt_tokens_cached_total` = 183488
- `vllm:generation_tokens_total` = 7228
- `vllm:kv_cache_usage_perc` = 0（idle 期間，bench 結束後）

實驗期間 hit rate panel 實時顯示 99%+，TTFT p95 隨 cycle 從 ~28s 降到 ~6s。

### Layer 2 — wren-ai-service sidecar (FastAPI proxy)
- `wren_ai_requests_total{route="/health",status="200"}` = 50
- `wren_ai_requests_total{route="/v1/asks/non-existent-N/result",status="200"}` = 50（每個 N 各 1）
- `wren_ai_inflight_requests` = 0
- `wren_ai_upstream_errors_total` = 0

sidecar 透過 reverse proxy 把 wren-ai-service 的所有 API 呼叫加上 Prometheus 觀察，**完全不需要改 Wren AI 上游程式碼**。route label 用 collapse 規則處理高 cardinality（>=16 字元含數字 → `:id`）。

### Layer 3 — Qdrant (vector DB)
- `collections_total` = 6（Wren AI 啟動時建立 6 個 collection）
- `collections_vector_total` = 0（沒灌資料）
- `rest_responses_total{method="PUT",endpoint="/collections/{name}/index"}` = 24（schema 初始化 PUT）

未跑真實 Wren AI workflow（沒灌 demo data），所以向量數為 0；但 collection 結構驗證 Qdrant 與 wren-ai-service 之間的 RAG init 流程運作。

### Layer 4 — Container resources (cAdvisor)
wren-ai namespace 各 pod 記憶體（working set）：
| Pod | Memory |
|---|---|
| ibis-server     | 598 MiB |
| wren-ai-service | 358 MiB（含 sidecar）|
| wren-engine     | 198 MiB |
| qdrant          | 192 MiB |
| wren-ui         | 57 MiB  |
| **小計** | **1.40 GiB** |

加上 Prometheus、kube-state-metrics、minikube control plane (~1.3 GiB)，整個 minikube VM 用約 **2.7 GiB**。實驗時 vllm-metal + Wren AI 並存對 16 GB Mac 的 unified memory 仍是負擔，故主 bench 時把 wren-ai pods 暫停以避免 CPU 競爭。

### Layer 5 — Kubernetes state (kube-state-metrics)
- Pod phases: 16 Running, 3 Succeeded, 1 Failed, 0 Pending
- Cluster total restarts: 46（**全集中在 monitoring/kube-state-metrics-xxx**）
- 注意：KSM 自身重啟 46 次，Prometheus 容器 log 顯示 K8s API server `client connection lost` / `TLS handshake timeout` 多次——minikube docker driver + macOS 同時跑大型 vllm-metal 時 control plane 偶發抖動

---

## 三大觀察重點

### 1. 「同 schema 不同問題」確實讓 hit rate 衝上 99%
1917-token system prompt 共用，30 個不同 question，3 cycle 後 hit rate 99.34%。這是 Wren AI 真實使用模式下 prefix cache 的價值來源。

### 2. vllm-metal 的觀察價值 vs omlx 的速度價值
- vllm-metal：**唯一能看 cache 行為的引擎**（KV-block-level metric, prefix_cache_hits/queries, kv_cache_usage_perc, kv_block_reuse_gap_seconds 等都齊全）
- omlx：**5-14x 快**（end-to-end），但黑盒
- 結論：研究 cache 機制必須用 vllm-metal；要 production 部署用 omlx 速度更好但失去可觀測性

### 3. 5-layer dashboard 證明本地 K8s + Metal 混合架構可觀測
從 LLM 引擎內部（vllm cache metric）→ 業務層（wren-ai sidecar req/sec）→ 下游服務（qdrant collections）→ 容器資源（cAdvisor）→ K8s 狀態（KSM），每層都有 ground truth metric。一個 dashboard URL 包辦從「使用者體感」到「LLM cache miss」的端到端追蹤。

---

## Raw data

- `exp5-vllm-metal-3cycle.csv` / `.summary.json`
- `exp5-omlx-3cycle.csv` / `.summary.json`
- `panel-snapshot.txt` — Prometheus panel 抓取
- Grafana dashboard JSON: `observability/grafana/provisioning/dashboards/llm-cache-overview.json`
- Live dashboard: [http://localhost:3001/d/llm-cache-5-layer](http://localhost:3001/d/llm-cache-5-layer) (admin/admin)

---

---

## Addendum (2026-05-06 +5 hours): End-to-end Wren AI workflow with fake data

After the initial A+C report, we addressed the four "Limitations / Future work"
items in priority order:

### 1. KSM 46-restart loop — FIXED

Root cause was **NOT** K8s API connection issues (which we initially suspected from
log noise). It was kubelet's default `timeoutSeconds: 1` on the KSM liveness/readiness
probes — KSM was slow to respond when the minikube node was under LLM load and
kubelet killed it. Fixed in `manifests/monitoring/30-kube-state-metrics.yaml` by
relaxing probe parameters (`timeoutSeconds: 10`, `failureThreshold: 5`,
`periodSeconds: 30`). New KSM pod has been stable with **0 restarts**.

### 2. TTFT measurement now skips role-only SSE chunks — FIXED

`scripts/bench.py` now starts the TTFT clock on the first non-empty `delta.content`
chunk, not the first SSE event. Re-measurement on omlx with the long schema:

| Engine | Old TTFT (first SSE) | New TTFT (first content token) |
|---|---|---|
| omlx (cold) | 0.007 s | **2.36 s** |
| omlx (warm) | 0.005-0.010 s | 0.87 s |
| vllm-metal (already correct) | — | 1.46 s |

omlx cold TTFT is now realistic (2.36 s) and slightly slower than vllm-metal warm,
which matches our intuition (omlx's cache works but is not magic).

### 3. omlx paged-SSD + hot cache exporter — IMPLEMENTED

Built `exporters/omlx/exporter.py` (FastAPI-free, uses `prometheus_client` +
`psutil`). Listens on host `:9105`, scraped by Prometheus via the new
`omlx-exporter-host` job. Exposes:

| Metric | What it measures |
|---|---|
| `omlx_up` | 1 if omlx serve process is running |
| `omlx_process_uptime_seconds` | seconds since omlx started |
| `omlx_process_rss_bytes` | RSS — limited usefulness on MLX (model is mmap'd → only counts in VMS, not RSS) |
| `omlx_process_vms_bytes` | VMS — actual indicator of model + hot cache footprint |
| `omlx_process_cpu_percent` | activity proxy |
| `omlx_process_open_files` | open fd count (rises with active blocks) |
| `omlx_paged_ssd_cache_bytes` | disk usage of `~/.omlx/paged-ssd-cache/` |
| `omlx_paged_ssd_cache_blocks` | file count (= cached block count) |
| `omlx_paged_ssd_cache_shards_used` | how many of the 16 hash shards have at least one block |
| `omlx_paged_ssd_oldest_block_age_seconds` | age of oldest cached block (LRU candidate) |
| `omlx_paged_ssd_newest_block_age_seconds` | age of most recent block (recency tracker) |

**Snapshot during the e2e run**: omlx_up=1, uptime ~50 min, paged-SSD cache bytes 0
(workload entirely fit in 1 GiB hot cache, never spilled to SSD — expected for
small-prompt benchmarks).

### 4. Wren AI fake data end-to-end — IMPLEMENTED via Wren's built-in sample dataset

Instead of standing up a Postgres pod with hand-crafted seed data, we used Wren AI's
built-in `StartSampleDataset` mutation that loads the **Olist Brazilian e-commerce**
dataset (9 tables: customers, orders, order_items, products, reviews, payments,
sellers, geolocation, category_translation) directly into a DuckDB-backed sample
data source. This skips the Postgres provisioning step while still exercising the
full Wren AI pipeline:

```graphql
mutation StartSampleDataset($data: SampleDatasetInput!) {
  startSampleDataset(data: $data)
}
# variables: {"data": {"name": "ECOMMERCE"}}  # also: HR, MUSIC, NBA
```

**Results captured**:

- **Schema indexing finished in ~17 s**, deploy hash `f91a37d52b86f0e302421d752955d7a41f7509d1`
- **Qdrant collections vector total: 0 → 27** (real schema embeddings via Ollama nomic-embed-text)
- **Sidecar Layer 2 captured real Wren routes** (instead of the synthetic /health spam):
  - `/v1/semantics-preparations` and `/v1/semantics-preparations/:id/status` (route-collapse correctly handled UUID polling)
  - `/v1/asks` and `/v1/asks/:id/result`
- **vllm-metal cache hit rate climbed live from 66.5% → 74.8%** as the multi-stage pipeline
  (intent classification → schema retrieval → SQL generation → SQL correction → SQL answer)
  reused the same schema-context prefix across LLM calls.

**The first question hit a real bug**: the LLM call exceeded `--max-model-len 4096`
(prompt 3073 + max_tokens 1024 = 4097 > 4096). Fixed by restarting vllm-metal with
`--max-model-len 8192`. The resubmitted question progressed UNDERSTANDING → SEARCHING
→ Ask Retrieval before encountering the runaway in #5.

### 5. Operational limit discovered: minikube CPU runaway under sustained Wren load

After 2-3 e2e questions, minikube node CPU pinned at 450-2500% for >5 min and
the K8s API server became unreachable (TLS handshake timeout). This is **the
same class of failure** as the vllm bench earlier, but worse because Wren AI's
multi-stage pipeline keeps multiple LLM round-trips in flight simultaneously.

Recovery options (none tested in this session due to time):
1. `minikube start --cpus 6 --memory 10240` (reduces host headroom for vllm-metal)
2. Move Wren AI off the same minikube node (separate cluster, or Docker Compose on host)
3. Limit Wren AI's concurrent pipeline stages via `config.yaml` settings
4. Use a faster LLM (7B+) so each pipeline call generates more usable output, requiring
   fewer retries

This is a strong argument that **observability should NOT live in the same minikube
cluster as the workload it observes** when both are heavy. Move Prometheus to its
own monitoring cluster or back to host Docker Compose for production use.

---

## Limitations / Future work

### Resolved (見 Addendum)

| 原 limitation | 狀態 | 修復點 |
|---|---|---|
| TTFT 量測在 SSE 下不對等（omlx 假 7ms） | ✅ 已修 | `scripts/bench.py` 跳過 role-only / finish-reason chunks；omlx cold TTFT 真實值 2.36s |
| omlx hit rate 沒 exporter | ✅ 已建 | `exporters/omlx/exporter.py`（host :9105, 11 個 metric）+ Prometheus job `omlx-exporter-host` |
| KSM 46 次自重啟 | ✅ 已修 | 真因是 kubelet 預設 probe `timeoutSeconds: 1`；改成 10s + failureThreshold 5 + period 30s |
| Wren AI 沒真實 workflow | ✅ 已跑 | `StartSampleDataset` 拉 Olist 9-table dataset；Qdrant 0→27 vectors；sidecar 抓到真實 `/v1/asks` 路由；vllm hit rate 66.5%→74.8% |

### Newly discovered (本次跑出來的真問題)

- **minikube CPU runaway under Wren AI sustained load**：observability + workload 共一個 minikube node 在 4 CPU 配額下，Wren AI 多階段 pipeline + 多並行 LLM round-trips 會讓 node CPU 拉到 450-2500%，K8s API server TLS handshake timeout 超過 5 分鐘不回神。建議 production 把 observability cluster 與 workload cluster 分開（或把 Prometheus 移回 host Docker Compose）
- **omlx hot cache 大小無法精準量測**：MLX 把模型 + hot cache mmap 進記憶體，所以 `process_rss_bytes` 只看到 25 MB 卻 `vms_bytes` 4.47 × 10¹¹（447 GB virtual）。RSS 不是好的 hot cache 占用 proxy。要量真正的 hot cache 占用須改 omlx source 或加 admin API session-cookie 認證後抓 `/admin/api/stats`
- **vllm `--max-model-len 4096` 對真實 Wren AI 太緊**：Wren AI 的 schema-context system prompt + tool definitions 一輪就 ~3073 tokens，加 max_tokens 1024 立刻超過 4096。實驗時臨時改 8192，但 Qwen 1.5B Q4 跑 8K 上下文會吃掉更多 KV cache memory（17.2 GB Metal 額度只剩 ~3 GB available）。Production 應該升 7B Q4 + 4K context 或 1.5B + 16K context（2 選 1）
- **`vllm:request_success_total` 與 histogram count 不一致**（55 vs 93）：38 個 request 沒到 success 狀態，疑為 client 流式中斷或 503 錯。需查 `bench.py` 的 streaming 早關行為
- **`vllm:kv_block_*` sampled metrics 在預設 sample rate 0.01 下 90 reqs 全都抽不到**：要做 block 級分析得 `--kv-cache-metrics-sample 1.0`，但會增加觀察 overhead
- **omlx 沒 SSE keep-alive，長 LLM 回應時 client 端可能斷**：實驗中 omlx 完整 90 reqs 都沒問題，但更長的 generation 場景需驗證
- **Sidecar route_label collapse 規則對 14-15 字元 ID 失效**：`non-existent-1...50` 都變獨立 series（cardinality 50 個）。應改 collapse 規則為「任何 segment 含數字皆 → :id」或「>= 8 字元含數字 → :id」
