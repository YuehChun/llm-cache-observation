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

## Limitations / Future work

- **TTFT 量測在 streaming SSE 下不對等**：omlx 立刻送 role-only chunk 讓 TTFT 量到 ~7 ms，並非真實 prefill 時間。應改測「first content token」而非「first SSE event」。
- **omlx hit rate 觀察缺口**：omlx paged-SSD/hot cache 沒對外暴露 metric。可寫個 cache disk usage exporter（看 `~/.omlx/paged-ssd-cache/` size 變化）作為近似。
- **KSM 自身重啟 46 次**：Prometheus 看到 K8s API TLS handshake timeout，需 root cause（可能是 minikube docker driver 在重 LLM workload 下 control plane 抖動）。
- **Wren AI 未跑真實 workflow**：本實驗只用「Wren-style prompt 模式」直打 vllm-metal，沒設定 demo Postgres + UI 專案。end-to-end query 需要額外 setup。
