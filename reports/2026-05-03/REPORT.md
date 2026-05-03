# LLM KV Cache 觀測實驗報告

**日期**：2026-05-03
**環境**：MacBook Pro Mac15,3 (Apple M3)，16 GB unified memory，macOS 26.3.1 (arm64)
**模型**：Qwen2.5-1.5B-Instruct，MLX 4-bit 量化（4.5 bits/weight，~847 MB）
**Engine**：vllm-metal 0.2.0 + vLLM 0.20.0+cpu，MLX 0.31.2 後端，Metal GPU
**命令範本**（baseline）：

```bash
VLLM_HOST_IP=127.0.0.1 python -m vllm.entrypoints.openai.api_server \
  --model ~/models/Qwen2.5-1.5B-Instruct-mlx-q4 \
  --served-model-name qwen2.5-1.5b \
  --host 0.0.0.0 --port 8000 \
  --enable-prefix-caching \      # OFF 改成 --no-enable-prefix-caching
  --kv-cache-metrics \
  --max-model-len 4096 \
  --no-enable-log-requests
```

**Workload**：30 個 Wren AI 風格的 Text-to-SQL 問題，共用一個 ~190-token 的 schema description system prompt。腳本 `scripts/bench.py` 透過 streaming chat completion 量測 TTFT。

**重要前置條件**：所有實驗開始前都先 `minikube stop`，避免 minikube control plane 吃 5+ CPU 核心搶佔 vllm-metal 的 Metal GPU/CPU 帶寬（首輪測試發現此 contention 把 prompt throughput 從 20+ tok/s 拉到 1.7 tok/s）。

---

## Experiment 1：Cold vs Warm（APC On）

**目的**：證明 prefix cache 能降低 TTFT。

**步驟**：重啟 vllm-metal（清空 KV cache）→ 連續送 10 個共用 system prompt 但不同 user question 的 request，記錄每筆 TTFT。第 1 筆視為 cold，第 2-10 筆為 warm。

**結果**：

| | TTFT |
|---|---|
| Cold (req 1)        | **9.876s** |
| Warm mean (req 2-10) | **2.013s** |
| Warm best (min)     | **0.332s** |
| Speedup vs cold     | **29.7x** |
| Prefix cache hit rate | 80.7% (1728 / 2140 prompt tokens cached) |

**結論**：第一次冷啟動須付 Metal kernel JIT compile + KV cache 從零建立的成本（~9.9s），之後重複請求的 system prompt 部分能完全命中 cache，TTFT 最佳可降至 0.33s。**對 Wren AI 這種 schema 共用的場景，prefix cache 對 TTFT 的改善可達 30 倍**。

完整 raw data：[`exp1-cold-warm.json`](./exp1-cold-warm.json)

---

## Experiment 2：APC On vs Off（公平對照）

**目的**：量化 prefix cache 對整體 throughput 的貢獻。

**步驟**：兩次 30-question run，引擎都先暖機。第一次 `--no-enable-prefix-caching`，第二次 `--enable-prefix-caching`。**公平條件**：兩次都用同樣的 question list、同樣的 max_tokens=128、同樣的 temperature=0、引擎都已過 cold-start。

**結果**：

| 指標                 | APC OFF | APC ON warm | Δ |
|---|---|---|---|
| Total time           | 289.5s  | 283.7s      | **−2.0%** |
| TTFT p50             | 1.758s  | 1.459s      | **−17.0%** |
| TTFT p95             | 7.448s  | 6.030s      | **−19.0%** |
| TTFT mean            | 2.875s  | 2.274s      | **−20.9%** |
| Tokens per sec (avg) | 6.89    | 6.74        | −2.2% |
| Cache hit rate       | 0%      | **92.7%**   | — |
| `prompt_tokens_cached_total` | 0    | 5920 / 6385 | — |

**結論**：
- **TTFT 改善穩定 ~20%**，p95 從 7.45s 降到 6.03s。
- **整體 throughput 改善很小（~2%）**，**遠不及 PROJECT_PLAN.md 預期的 1.5-3x**。
- 原因：在 1.5B Q4 模型 + 短 prompt（~213 tokens）+ 短 generation（~80 tokens）的 workload 下，**整體時間是 generation-bound 而非 prefill-bound**。Prefix cache 只省 prefill 計算，不省 decode 計算。
- 92.7% prompt token 從 cache 取，prefill 計算量幾乎全省，但因 generation 才是時間瓶頸，整體時間只少 6 秒。
- **若換用 7B+ 模型或 prompt 變長（>1000 tokens）**，prefill 會回到瓶頸位置，APC 對 throughput 的改善會放大到接近原計畫預期。

完整 raw data：[`exp2-apc-off.csv`](./exp2-apc-off.csv) / [`exp2-apc-on-warm.csv`](./exp2-apc-on-warm.csv)

---

## Experiment 3：Wren AI 真實 workload（3-cycle retention）

**目的**：在「使用者對同 schema 持續提問」的場景下觀察 cache 隨時間累積的效益。

**步驟**：APC On，連續跑 3 個完整 cycle（每 cycle 同樣 30 題），共 90 個 request。每 cycle 內每題都不同，但 cycle 之間題目重複。

**結果（per-cycle）**：

| Cycle | n  | TTFT p50 | TTFT mean | TTFT min | Total time mean | Output tokens mean |
|---|---|---|---|---|---|---|
| 0     | 30 | 0.655s   | 1.875s    | 0.277s   | 10.93s          | 64 |
| 1     | 30 | 0.686s   | 1.314s    | 0.056s   | 7.78s           | 56 |
| 2     | 30 | 0.588s   | 1.040s    | 0.090s   | 6.65s           | 65 |

**整體**：n=90, total 762.2s, TTFT p50 0.638s, mean 1.410s, **cache hit rate 97.7%** (18720/19155 prompt tokens cached)

**Cycle 之間的變化**：

- TTFT mean: 1.88s → 1.31s → 1.04s（**3 cycle 後降 45%**）
- 每題總時間: 10.93s → 7.78s → 6.65s（**降 39%**）
- TTFT min: 0.28s → 0.06s → 0.09s（最佳情況下 cache 命中後 prefill 幾乎為零）
- Cache hit rate 從 cycle 0 的 ~92% 拉高到 cycle 2 接近上界（受 EOS / 各題尾段差異限制無法 100%）

**結論**：**這是 cache 對真實使用者場景最有說服力的驗證**：
- 第一輪每筆 ~11 秒，使用者體感「LLM 還在思考」
- 第三輪每筆 ~6.6 秒，**使用者體感變得明顯更流暢，且最佳請求 TTFT 接近瞬時 (90 ms)**
- 對 Wren AI 這類「同 schema 多問題」應用，**啟用 prefix cache 是免費的午餐**，只需開一個 flag

完整 raw data：[`exp3-apc-on-3cycle.csv`](./exp3-apc-on-3cycle.csv) / [`exp3-apc-on-3cycle.summary.json`](./exp3-apc-on-3cycle.summary.json)

---

## 三個核心目標的回答

| Goal | 結論 |
|---|---|
| **G1：基線比較** | APC On 對 TTFT 有 17-21% 改善（warm 路徑），對 cold-vs-warm 有 30 倍改善。整體 throughput 改善小，因為 1.5B 短 generation 是 generation-bound |
| **G2：Wren AI 真實場景觀察** | 「同 schema 不同問題」workload 下 cache hit rate 在第一輪即達 92%，3 輪後達 97.7%，per-request 時間下降 39% |
| **G3：K8s 部署研究** | 確認可行架構：vllm-metal 在 host (Metal GPU)，Wren AI + Qdrant 在 minikube，observability 在 host Docker Compose。**重要發現**：(a) Wren AI v0.29.0 wren-ai-service 不暴露 `/metrics`；(b) 同時跑 minikube + vllm-metal 在 16 GB Mac 上 CPU 競爭嚴重（5+ 核心被 minikube 吃掉），實驗階段須 `minikube stop`；(c) Docker Desktop v29.4.1 + minikube v1.38.1 已修了舊版 `host.minikube.internal` 解析錯的問題 |

---

## 觀察到的限制

1. **vllm-metal 0.2.0 在 16GB M3 上 prompt throughput 僅 ~6.7 tok/s（generation）和 ~30 tok/s prefill**——遠低於同硬體上 Ollama 跑同模型的數字。Metal kernel optimization 仍在改善中。
2. **Wren AI v0.29.0 的 wren-ai-service 沒有 Prometheus metrics endpoint**——無法直接觀察 ai-service 內部的 LLM 呼叫頻率、retry 次數、token 用量。需要從 vllm-metal 自身的 metrics 反推。
3. **本實驗未跑「Wren AI 端到端」的真實 query**——只用 schema-shared prompt 模擬其 prompt pattern。end-to-end 測試需要設定 Wren UI 專案、灌 demo schema，未列入 W3-W4 範圍。
4. **Apple Silicon 的 16 GB unified memory 同時跑 vllm-metal 1.5B + minikube + Wren AI 4 容器 + Ollama embedding 會接近 OOM**——量測時必須選擇性停掉 minikube 或 Wren AI pods 釋放資源。
5. **APC On 的 cache memory overhead**：vllm-metal 報告 KV cache usage 約 0.1%（17.2GB Metal memory 中），所以 cache 容量沒問題；瓶頸不在記憶體而在 generation 速度。

---

## Bonus Experiment：7B + 長 Prompt（驗證 prefill-bound 假設）

**動機**：Exp 2 在 Qwen 1.5B + ~190-token system prompt 下只看到 2% 的 throughput 改善，與 PROJECT_PLAN.md 預期的 1.5-3x 相距甚遠。我們推測這是因為 1.5B + 短 prompt 是 generation-bound，prefix cache 只省 prefill 不省 decode。本實驗用 **Qwen2.5-7B-Instruct (MLX 4-bit, ~4 GB)** + **~1917-token 真實 schema description**（含 13 個 table 完整 column 定義 + 慣例 + 規則）來把 workload 推進 prefill-bound 區，驗證假設。

**實驗條件**：10 個問題 × 1 cycle，max_tokens=64，兩次都先暖機。`scripts/wren_long_schema.py` 提供長 system prompt（cl100k_base 估 1917 tokens）。

**結果**：

| 指標                 | 7B APC OFF | 7B APC ON   | Δ |
|---|---|---|---|
| Total time           | 283.5s     | 88.7s       | **−68.7%** |
| TTFT p50             | 16.93s     | 1.46s       | **−91.4%** |
| TTFT p95             | 27.91s     | 4.05s       | **−85.5%** |
| TTFT mean            | 20.42s     | 2.85s       | **−86.0%** |
| Throughput (tok/s)   | 2.2        | 7.0         | **+218.2% (3.2x)** |
| Cache hit rate       | 0%         | **98.8%**   | — |
| `prompt_tokens_cached_total` | 0  | 19840 / 20090 | — |

**對比 1.5B 短 prompt 的結果**：

| Workload | Throughput Δ | TTFT mean Δ |
|---|---|---|
| 1.5B + 短 prompt (~190 tok) | **+2%** | −21% |
| 7B + 長 prompt (~1917 tok)  | **+218% (3.2x)** | **−86%** |

**結論**：**PROJECT_PLAN.md 的「1.5-3x throughput 改善」預測完全正確**——它只在 prefill-bound workload 下浮現。決定改善幅度的核心比例是 `prefill_compute / total_compute`：
- 1.5B + 190 tok prompt + 80 tok generation → prefill 占整體時間 ~10% → cache 上限改善 ~10%
- 7B + 1917 tok prompt + 60 tok generation → prefill 占整體時間 ~85% → cache 把這 85% 幾乎全省，吞吐量直接 3.2 倍

**對 Wren AI 真實場景的 implication**：Wren AI 實際上 inject 的 MDL schema description 經常 1500-3000 tokens。所以 **如果換 7B+ 模型 + 真實 MDL，prefix cache 的價值會非常顯著（3-5x throughput）**。1.5B 模型本身太小，不足以體現 cache 的真實價值。

完整 raw data：[`exp4-7b-long-apc-off.csv`](./exp4-7b-long-apc-off.csv) / [`exp4-7b-long-apc-on.csv`](./exp4-7b-long-apc-on.csv)

---

## 建議的後續研究

- **加長 prompt 並換大模型**：✅ 已完成（見 Bonus Experiment）。用 Qwen2.5-7B Q4 + 1917 token system prompt 跑出 3.2x throughput 改善，完全驗證 PROJECT_PLAN.md 預期。
- **Wren AI 端到端**：手動設置 demo Postgres + Wren UI 專案，跑「使用者連續問 30 個關於 e-commerce schema 的問題」整個 pipeline，量測 user-perceived latency。
- **比較 omlx 對照組**：用同模型在 omlx 上跑 Exp 2，驗證 vllm-metal 的 cache 是否真的比 omlx 內建的 cache 更有效。
- **Phase 3 LMCache offload**：若需要分層 cache（GPU→CPU→SSD），需要租雲端 GPU spot 環境跑，本機 emulation 無意義。
