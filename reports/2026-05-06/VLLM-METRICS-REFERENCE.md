# vllm-metal `/metrics` Reference

**Source**: live capture from `curl http://localhost:8000/metrics` after the
A+C 90-request bench (`exp5-vllm-metal-3cycle.csv`).

**Engine**: vllm-metal 0.2.0 + vLLM 0.20.0+cpu, Qwen2.5-1.5B-Instruct (MLX 4-bit).

**Server flags**: `--enable-prefix-caching --kv-cache-metrics --max-model-len 4096`.

**Total metric families**: 86 (72 `vllm:*` + 14 `http_*`/`process_*`/`python_*`).

每個 series 都帶兩個 label：`engine="0"` 與 `model_name="qwen2.5-1.5b"`。下面省略這兩個 label，只列其他 label。

---

## A. Cache 行為（第一觀察重點）

### A.1 Local prefix cache（KV cache 命中率）

| Metric | Type | 用途 | Snapshot 值 |
|---|---|---|---|
| `vllm:prefix_cache_queries_total` | counter | 累計查詢的 prompt token 數（**分母**） | 186,704 |
| `vllm:prefix_cache_hits_total` | counter | 累計命中 cache 的 token 數（**分子**） | 183,488 |
| `vllm:prefix_cache_queries_created` | gauge | counter 建立時間（Unix epoch） | — |
| `vllm:prefix_cache_hits_created` | gauge | 同上 | — |

**核心 PromQL**：
```promql
sum(rate(vllm:prefix_cache_hits_total[5m]))
  / clamp_min(sum(rate(vllm:prefix_cache_queries_total[5m])), 1)
```
**snapshot ratio**: 183488 / 186704 = **98.28%**

### A.2 External prefix cache（KV connector 跨節點共享，本機未啟用）

| Metric | Type | 用途 | Snapshot |
|---|---|---|---|
| `vllm:external_prefix_cache_queries_total` | counter | 跨節點 KV connector 查詢 token 數 | 0 |
| `vllm:external_prefix_cache_hits_total` | counter | 跨節點命中 token 數 | 0 |

> 本機單節點不啟用 KV connector（vllm-metal 的 Metal worker 也不支援，見 `CLAUDE.md` §4.4）。

### A.3 Multi-modal cache（本實驗無 image 輸入）

| Metric | Type | 用途 | Snapshot |
|---|---|---|---|
| `vllm:mm_cache_queries_total` | counter | MM 輸入 cache 查詢項目數 | 0 |
| `vllm:mm_cache_hits_total` | counter | 命中數 | 0 |

### A.4 Prompt token 來源拆分（**重要**）

`vllm:prompt_tokens_by_source_total` (counter, label `source`)：

| source | tokens | 占比 |
|---|---|---|
| `local_compute`        | 3,216   | **1.72%**  |
| `local_cache_hit`      | 183,488 | **98.28%** |
| `external_kv_transfer` | 0       | 0%         |

**實驗結論**：90 個 request 的總 prompt token 中只有 1.72% 真的進 prefill 計算，其餘從 cache 取——這個拆分比 hit_rate 更直接告訴你 cache 在做多少工。

### A.5 KV-block-level sampled metrics（`--kv-cache-metrics` 必須開）

| Metric | Type | 用途 | 本實驗 count |
|---|---|---|---|
| `vllm:kv_block_lifetime_seconds` | histogram | block 從 allocate 到 evict 的存活時間 | 0 |
| `vllm:kv_block_idle_before_evict_seconds` | histogram | block 被 evict 前的閒置時間（揪 stranded cache） | 0 |
| `vllm:kv_block_reuse_gap_seconds` | histogram | 同一 block 被重複存取的時間間隔 | 0 |

> 這 3 個是**取樣型** metric，預設 `--kv-cache-metrics-sample=0.01`。本實驗 90 個 request 取樣機率太低，count=0 表示沒抽到事件。如要分析 block 行為要把 sample 拉到 1.0（會增加 overhead）。

---

## B. 時間分布（latency histograms）

每個 histogram 都有 `_bucket{le="..."}` + `_count` + `_sum`。9 個 default bucket 邊界涵蓋從亞秒到分鐘。

| Metric | 涵義 | count | sum (s) | mean (s) |
|---|---|---|---|---|
| `vllm:e2e_request_latency_seconds`     | 端到端請求耗時 | 93 | 1816.07 | 19.53 |
| `vllm:request_queue_time_seconds`      | WAITING 階段時間 | 93 | 0.08    | 0.001 |
| `vllm:request_inference_time_seconds`  | RUNNING 階段（prefill+decode） | 93 | 1709.30 | 18.38 |
| `vllm:request_prefill_time_seconds`    | PREFILL 階段 | 93 | 660.12  | 7.10 |
| `vllm:request_decode_time_seconds`     | DECODE 階段 | 93 | 1049.18 | 11.28 |
| `vllm:time_to_first_token_seconds`     | TTFT（streaming 第一個 token） | 93 | 834.01  | 8.97 |
| `vllm:inter_token_latency_seconds`     | token 間延遲（每個 generated token 一筆） | 7135 | 1049.18 | 0.147 |
| `vllm:request_time_per_output_token_seconds` | 每 output token 平均（per-request） | 93 | — | — |

**實驗解讀**：
- 平均 prefill = 7.10 s，平均 decode = 11.28 s → **decode 占比 60%**（即使 1917-token system prompt）
- queue time 幾乎 0（單 client，無排隊）
- inter-token latency 0.147 s = **6.8 tok/s decode rate**

**核心 PromQL**：
```promql
# TTFT p50 / p95
histogram_quantile(0.50, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))

# Prefill 占 e2e 比例
sum(rate(vllm:request_prefill_time_seconds_sum[5m]))
  / sum(rate(vllm:e2e_request_latency_seconds_sum[5m]))
```

---

## C. Token / request 分布（histograms）

| Metric | 涵義 | count | sum |
|---|---|---|---|
| `vllm:request_prompt_tokens`             | 每 request 的 prompt token 數 | 93 | 186,704 |
| `vllm:request_generation_tokens`         | 每 request 生成 token 數 | 93 | 7,228 |
| `vllm:request_prefill_kv_computed_tokens`| 真的進 prefill 的新 token（**=request_prompt − cached**） | 93 | 3,216 |
| `vllm:request_max_num_generation_tokens` | 設定的 max_tokens histogram | 93 | — |
| `vllm:request_params_n`                  | n 參數分布（並行 sample 數） | 93 | — |
| `vllm:request_params_max_tokens`         | max_tokens 分布 | 93 | — |
| `vllm:iteration_tokens_total`            | 每 engine_step 處理 token 數（內部 batching） | 7,228 | 193,932 |

**重要關係驗證**：
```
prefill_kv_computed (3216) + cache_hits (183488) = 186704 = request_prompt total ✓
```

`vllm:request_prefill_kv_computed_tokens` 平均 = 3216 / 93 = **34.6 token** per request 真的進 prefill 計算（而 prompt 平均長度 1917 token），剩下都從 cache 取。

---

## D. Throughput（counters）

| Metric | Type | 涵義 | Snapshot |
|---|---|---|---|
| `vllm:prompt_tokens_total`     | counter | 累計 prompt token | 186,704 |
| `vllm:prompt_tokens_cached_total` | counter | 累計命中 cache 的 prompt token（= `prefix_cache_hits_total`） | 183,488 |
| `vllm:generation_tokens_total` | counter | 累計 generation token | 7,228 |
| `vllm:request_success_total`   | counter (label `finished_reason`) | 成功完成的 request 數 | 55 |

**核心 PromQL**：
```promql
# Generation tokens/s（rolling）
sum(rate(vllm:generation_tokens_total[1m]))

# Prefill tokens/s
sum(rate(vllm:prompt_tokens_total[1m])) - sum(rate(vllm:prompt_tokens_cached_total[1m]))
```

> ⚠️ `request_success_total=55` 但 histogram count=93——其中 38 個 request 沒到 success 狀態（可能 cancel / 失敗 / streaming 中斷）。這個落差需要追：可能是 client `bench.py` 在某些 request 提早 close connection 導致 vllm 沒記為 success。

---

## E. Capacity 與 system state（gauges）

| Metric | Type | 涵義 | Snapshot |
|---|---|---|---|
| `vllm:num_requests_running` | gauge | 當前正在執行的 request | 0 |
| `vllm:num_requests_waiting` | gauge | 等待中的 request | 0 |
| `vllm:num_requests_waiting_by_reason` | gauge (label `reason`) | 等待原因拆分（capacity / deferred） | 0 |
| `vllm:kv_cache_usage_perc` | gauge | KV cache 占用比例 (0~1) | 0 |
| `vllm:engine_sleep_state` | gauge (labels `awake`/`weights_offloaded`/`discard_all`) | engine 睡眠狀態（0=睡眠 1=醒著） | 1 |
| `vllm:num_preemptions_total` | counter | 因記憶體不足搶占的累計次數 | 0 |

> 注意：PROJECT_PLAN.md 寫的 `vllm:gpu_cache_usage_perc` **在 vllm 0.20 重新命名為 `vllm:kv_cache_usage_perc`**（語義相同）。

---

## F. Hardware FLOPs 估算（Apple Silicon 上**全為 0**）

| Metric | Type | 涵義 |
|---|---|---|
| `vllm:estimated_flops_per_gpu_total` | counter | 估算每 GPU FLOPs |
| `vllm:estimated_read_bytes_per_gpu_total` | counter | 估算每 GPU 記憶體讀 bytes |
| `vllm:estimated_write_bytes_per_gpu_total` | counter | 估算每 GPU 記憶體寫 bytes |

> 這 3 個用於 **MFU (Model Flops Utilization)** 計算，但 vllm-metal 的 Metal worker **沒有對應的 hardware counter integration**——全為 0。在 NVIDIA CUDA 上才有用。

---

## G. Engine config snapshot（info gauge）

`vllm:cache_config_info`（恆 1，所有設定都在 label 中）：

| Label | Value |
|---|---|
| `block_size` | 16 |
| `cache_dtype` | auto |
| `enable_prefix_caching` | True |
| `gpu_memory_utilization` | 0.92 |
| `num_gpu_blocks` | **20090** |
| `prefix_caching_hash_algo` | sha256 |
| `kv_offloading_backend` | native |
| `is_attention_free` | False |
| `sliding_window` | None |

**計算 KV cache 容量**：
```
20090 blocks × 16 tokens/block = 321,440 tokens 容量
```
與 `--max-model-len 4096` 對應 **~78 個 4096-token request 的 KV cache 同時存在**。

---

## H. HTTP 層（FastAPI 觀察，FYI）

| Metric | 涵義 |
|---|---|
| `http_requests_total{handler,method,status}` | 各 endpoint 的請求總數 |
| `http_request_duration_highr_seconds` | 高 resolution latency（精細 percentile） |
| `http_request_duration_seconds` | 低 resolution latency by handler |
| `http_request_size_bytes` | 請求 body 大小 |
| `http_response_size_bytes` | 回應 body 大小 |

> 這些是 FastAPI 的 prometheus_fastapi_instrumentator 自動生成的，跟 LLM 行為無關但能看 endpoint usage pattern。

---

## I. 推薦的 4 個 Grafana panel（最關鍵）

```promql
# 1. Cache hit rate (應穩定 > 90%)
sum(rate(vllm:prefix_cache_hits_total[5m]))
  / clamp_min(sum(rate(vllm:prefix_cache_queries_total[5m])), 1)

# 2. TTFT p95（user-facing latency）
histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))

# 3. Generation throughput
sum(rate(vllm:generation_tokens_total[5m]))

# 4. KV cache 占用（接近 1 表示記憶體飽和、會發生 preemption）
vllm:kv_cache_usage_perc
```

---

## J. 我們實驗中**沒能用到**的 metric

由於 vllm-metal 0.2.0 的 Metal worker 限制：

- `vllm:kv_block_lifetime_seconds`、`kv_block_idle_before_evict_seconds`、`kv_block_reuse_gap_seconds`
  — 預設 sample 0.01 太低，本實驗 90 req 取樣 count = 0
- `vllm:estimated_flops_per_gpu_total` 系列 — Metal 沒對應 counter
- `vllm:external_prefix_cache_*` — 沒啟用 KV connector
- `vllm:mm_cache_*` — 純文字模型
- `vllm:num_preemptions_total` — KV cache 沒爆，沒 preempt

如果未來想觀察 block 級別的 cache 行為，要 `--kv-cache-metrics-sample 1.0`（會增加觀察 overhead）。
