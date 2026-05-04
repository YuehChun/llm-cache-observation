# CLAUDE.md — Claude Code 工作上下文

> 這個檔案會在 Claude Code 開啟此專案時自動載入。
> 它定義了專案的目的、約束、文件結構，以及維護規則。

---

## 1. 專案身份

| 項目 | 內容 |
|---|---|
| **名稱** | LLM KV Cache 觀測實驗 (llm-cache-observation) |
| **路徑** | `/Users/birdtasi/Documents/Projects/llm-cache-observation` |
| **目的** | 以 Wren AI 為真實 workload，量測 LLM 推論的 KV cache hit rate、TTFT、prefill 等指標 |
| **目標機器** | MacBook，Apple Silicon M-series，**16 GB unified memory**（硬約束） |
| **狀態** | 見下方 §1.1 Phase 進度 |

### 1.1 Phase 進度

當完成一個 Phase 後，把 `[ ]` 改成 `[x]`，並在後面附上完成日期。

- [x] **Phase 0**：基礎環境完成 (2026-05-03)
  - Docker Desktop v29.4.1 (arm64)、minikube v1.38.1（4 CPU / 8GB / 40GB）、kubectl v1.36.0、helm、kompose、httpie 全裝好
  - minikube cluster up，addons：ingress、metrics-server
  - 3 個 namespace：`wren-ai`、`monitoring`、`data`
  - vllm-metal 0.2.0 + vllm 0.20.0+cpu + mlx 0.31.2 + mlx-lm 0.31.3 安裝在 `~/.venv-vllm-metal`（Python 3.12.13）
  - 安裝方式：`curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh | bash`（**不是** `pip install vllm-metal`，後者在 PyPI 不存在）
- [x] **Phase 1**：vllm-metal serve Qwen2.5-1.5B 完成 (2026-05-03)
  - 模型：`~/models/Qwen2.5-1.5B-Instruct-mlx-q4` (847 MB, 4.5 bits/weight, dtype bf16)
  - server 啟動 flag：`--enable-prefix-caching --kv-cache-metrics --max-model-len 4096 --no-enable-log-requests`
  - **環境變數必須設定 `VLLM_HOST_IP=127.0.0.1`**：否則 vllm 會選到 utun4（VPN）介面 10.5.0.2，gloo distributed init 會 timeout 卡住
  - **Metrics gate ALL PASS**：`prefix_cache_hits_total`、`prefix_cache_queries_total`、`time_to_first_token_seconds`、`request_prefill_time_seconds`、`generation_tokens_total`、`kv_block_reuse_gap_seconds`、`kv_block_lifetime_seconds`、`kv_block_idle_before_evict_seconds` 全在
  - 驗證實際命中：3 次同 prompt 後 hit rate 44% (112/255 query)
- [x] **Phase 2**：Wren AI in minikube + observability 完成 (2026-05-03)
  - **Embedder 走 Plan B'**：vllm-metal serve LLM (Qwen2.5-1.5B at :8000)；Ollama serve embedding (nomic-embed-text at :11434, 768-dim)。原計畫的 `text-embedding-3-large` (3072-dim) 需 OpenAI key 故捨棄
  - Wren AI 6 個 pod 全 Running（bootstrap Completed）：bootstrap, qdrant, wren-engine, ibis-server, wren-ai-service, wren-ui，都跑 `linux/arm64` native（image 都有 arm64 build）
  - manifest 由 kompose 從 `~/wren-k8s-input/resolved.yaml` 產出到 `~/wren-k8s/`（手改 wren-ai-service Service → NodePort 30090，wren-ui Service → NodePort 30000）
  - **重要發現 1**：wren-ai-service v0.29.0 **不暴露 /metrics**（只有 `/health` + `/v1/...`）。原 PROJECT_PLAN.md §2.1 的 NodePort 30090 metrics scrape 是誤判，已從 prometheus.yaml 移除
  - **重要發現 2**：Docker Desktop v29.4.1 + minikube v1.38.1 已修了 PROJECT_PLAN.md §2.2 擔心的 `host.minikube.internal` 網路陷阱——pod 內解析為 192.168.65.254（真 host gateway），可直連 host 上的 vllm-metal :8000 與 Ollama :11434
  - **重要發現 3**：minikube NodePort `192.168.49.2:30090` **不可從 host 直達**（docker driver 限制）。要從 host 連 minikube service 用 `kubectl port-forward` 或 `minikube tunnel`
  - Prometheus targets：`prometheus` UP、`vllm-metal-host` UP（2/2）。cache 觀察在 LLM 層（vllm-metal），這是三大目標真正關心的地方
- [ ] **Phase 3 (選做)**：LMCache x86 emulation 對照實驗
- [x] **2026-05-04 架構調整**：Prometheus 進 minikube；Grafana 留 host；omlx 加入對照組
  - **背景**：原規劃把 Prometheus 也放 host Docker Compose，理由是擔心 macOS docker driver 下 `host.minikube.internal` 解析錯誤——但 Phase 2 重要發現 2 已確認該陷阱在 Docker Desktop v29.4.1 + minikube v1.38.1 修掉了。Prometheus 進 minikube 反而能多 scrape cAdvisor/kubelet/pod annotations
  - **Prometheus → minikube**：`manifests/monitoring/{00-namespace,05-prometheus-rbac,10-prometheus-configmap,20-prometheus-deployment}.yaml`，NodePort 30091（30090 被既有 wren-ai-service 佔用）。4 個 scrape jobs 全 UP：`vllm-metal-host`、`kubernetes-cadvisor`、`kubernetes-kubelet`、`prometheus`
  - **Grafana 留 host**：minikube NodePort 從 macOS host 不可直連（docker driver 限制），所以 Grafana 用 docker-compose 留 host，datasource 改指 `host.docker.internal:9090`，由 `kubectl port-forward svc/prometheus -n monitoring 9090:9090` 提供。對應腳本 `scripts/start-prometheus-tunnel.sh`
  - **omlx 對照組**：`brew install` jundot/omlx tap，最新 0.3.8 / 0.3.5+ 都鎖定一個已從 dflash-mlx repo 消失的 commit `814c4a1`，所以 fall back 到 v0.3.4（最後一個沒 dflash-mlx 依賴的版本），手動修 formula 把 tokenizers 從 `--no-binary` 移除避開 macOS 15+ PyO3 linker 錯
  - **omlx 用法**：手動 `omlx serve --host 0.0.0.0 --port 8001`（不用 brew services 自動啟動，量測時才開；避免閒置吃 12.8 GB 記憶體 quota 與 Metal GPU 算力）。**沒有 Prometheus `/metrics`**，對照組數據走 `scripts/bench.py` 在 client 端採樣
- [x] **W3-W4**：A/B 實驗 + 報告完成 (2026-05-03)
  - **Exp 1 Cold/Warm**：cold TTFT 9.88s, warm best 0.33s, **speedup 29.7x**, hit rate 80.7%
  - **Exp 2 APC On vs Off (公平 warm 對照)**：TTFT mean 改善 21% (2.87s→2.27s)、p95 改善 19%。**整體 throughput 只改善 2%**——1.5B 短 generation 是 generation-bound，不是 prefill-bound
  - **Exp 3 3-cycle retention (90 題)**：TTFT mean 從 cycle 0 的 1.88s 降到 cycle 2 的 1.04s（−45%），總時間從 10.93s 降到 6.65s（−39%），**hit rate 達 97.7%**
  - 報告：[`reports/2026-05-03/REPORT.md`](./reports/2026-05-03/REPORT.md)
  - 三大目標都有具體數據答案；**重要副作用發現**：實驗時須 `minikube stop`，否則 minikube 吃 5+ 核心搶佔 vllm-metal 帶寬
  - **Bonus Exp：7B + 1917 tok system prompt**：APC On vs Off 跑出 **3.2x throughput**（2.2 → 7.0 tok/s）、TTFT mean −86%（20.4s → 2.85s）、cache hit rate 98.8%。**完全驗證 PROJECT_PLAN.md 1.5-3x 預期**——它只在 prefill-bound workload (`prefill / total > 50%`) 浮現，1.5B + 短 prompt 看不到是 normal
- [ ] **Phase 3 (選做)**：LMCache x86 emulation 對照實驗

---

## 2. 三個核心目標（任何架構決策都要回扣這三個）

1. **基線比較**：啟用 / 關閉 prefix cache 對 TTFT 與吞吐量的影響有多大？
2. **真實場景觀察**：Wren AI（Text-to-SQL）這種「同 schema、不同問題」的 workload，cache 命中率隨時間如何變化？
3. **K8s 部署研究**：把 Wren AI + observability 放到 minikube 上，實際運作所需的最小可行架構長什麼樣？

---

## 3. 核心架構

**一句話版**：vllm-metal + omlx 跑在 macOS host（Metal GPU）；Wren AI + Prometheus 跑在 minikube；Grafana 用 Docker Compose 跑 host，透過 `kubectl port-forward` 連到 minikube Prometheus。

### 3.1 兩個獨立執行平面（**2026-05-04 後**）

1. **macOS host**：
   - **vllm-metal** server（`:8000`，OpenAI API + `/metrics`，Metal GPU，主力 LLM）
   - **omlx** server（`:8001`，OpenAI API，無 `/metrics`，量測時手動啟動的對照組）
   - **Grafana**（`:3001`，Docker Compose）— datasource 指 `host.docker.internal:9090`
   - **kubectl port-forward**（`:9090` ← minikube `svc/prometheus`）— 必須開著
   - LLM serving 必須在 host：Metal API 私有，不能進 container
2. **minikube (docker driver, 8GB / 4 CPU)**：
   - **monitoring ns**：Prometheus（`:9090`，NodePort `:30091`）+ ServiceAccount RBAC
   - **wren-ai ns**：wren-ui / wren-ai-service / wren-engine / wren-ibis-server / qdrant
   - **data ns**：Postgres demo DB

### 3.2 關鍵資料流

- **Prompt 路徑**：使用者 → `wren-ui:3000`（minikube）→ `wren-ai-service:8000`（minikube，組合 schema description + 使用者問題）→ `host.minikube.internal:8000/v1/chat/completions`（從 minikube 打到 host vllm-metal）→ 回傳 SQL。
- **Metrics 路徑**：
  - minikube Prometheus → `host.minikube.internal:8000/metrics`（vllm-metal）
  - minikube Prometheus → kubernetes API server proxy → cAdvisor / kubelet（cluster 內）
  - minikube Prometheus → 任何 pod 上 `prometheus.io/scrape=true` annotation
  - host Grafana → `host.docker.internal:9090` → `kubectl port-forward` → `svc/prometheus.monitoring`
- **跨界網路陷阱**（2026-05-04 已驗證）：
  - `host.minikube.internal`（minikube pod → host）：在 Docker Desktop v29.4.1 + minikube v1.38.1 解析為 192.168.65.254（真 host gateway），可用
  - `host.docker.internal`（host Docker container → host）：用於 host 上 Docker Compose 的 Grafana 連 port-forward 的 9090
  - minikube NodePort `192.168.49.2:NNNN` **從 macOS host 不可達**（docker driver 限制）→ 必須用 `kubectl port-forward` 或 `minikube tunnel`

完整的視覺化架構圖見 [`ARCHITECTURE.html`](./ARCHITECTURE.html)（在瀏覽器打開）；逐步部署步驟見 [`PROJECT_PLAN.md`](./PROJECT_PLAN.md)。

---

## 4. 硬體限制與排除元件（重要：別再嘗試重新引入）

以下元件在原始計畫中被列出，但因為 Apple Silicon 16 GB MacBook 的硬體限制**無法使用**，已從架構移除。除非未來換機，否則不要重新引入：

### 4.1 ❌ 排除：`llm-d` （硬性無法執行）
- Repo：https://github.com/llm-d/llm-d
- 原因：官方明確要求 **NVIDIA A100+ / AMD MI250 / TPU v5e+ / Intel GPU Max**，並依賴 GPUDirect RDMA。Apple Silicon 連最低門檻都達不到。
- 替代：用單一 vllm-metal 實例。如果未來真的要研究 disaggregated serving，租雲端 GPU spot instance（GKE/EKS T4 約 $0.5/小時）。

### 4.2 ❌ 排除：`Mooncake` (kvcache-ai) （單機無觀測意義）
- Repo：https://github.com/kvcache-ai/Mooncake
- 原因：解決的是「跨節點 GPU cluster KV 互不共用」的問題。單機 16 GB MacBook 沒有這個問題可解。雖然 CPU build 可選，但 ARM64 路徑未驗證，且運作起來跟本機 cache 等價。
- 替代：vLLM 內建 Automatic Prefix Caching (APC) 已足以做本機 KV cache 觀測。

### 4.3 ⚠️ 部分排除：`vLLM 主分支 (CUDA)` 
- Repo：https://github.com/vllm-project/vllm
- 原因：CUDA 路徑在 Apple Silicon 完全不可用；CPU backend 雖支援 ARM64 (NEON)，但需 source build、效能遠低於 Metal。
- 替代：**`vllm-metal` plugin**（vllm-project + Docker 2026/1 釋出）。限制：只能跑在 macOS host，無法進 Linux container/minikube。

### 4.4 ⏳ 延後：`LMCache` 到 Phase 3 條件性實驗
- Repo：https://github.com/LMCache/LMCache | Helm: https://lmcache.github.io/helm/
- 原因：官方文件指明「Linux NVIDIA GPU platform」，ARM64 / 純 CPU 路徑未確認。
- 處置：Phase 1-2 不導入。Phase 3 嘗試在 minikube 內 `--platform=linux/amd64` emulation 跑 vLLM CPU image + LMCache 做對照，但 emulation 慢 5-10x，TTFT 數據沒有絕對意義。

### 4.5 ✅ 保留但角色調整：`omlx`
- Repo：https://github.com/jundot/omlx
- 角色：作為對照組（同模型不同引擎），**不放進 K8s**（它本身就是 macOS menu-bar app）。

---

## 5. 文件結構

```
llm-cache-observation/
├── CLAUDE.md                       ← 本檔，Claude 工作上下文（你正在看）
├── PROJECT_PLAN.md                 ← 完整書面規劃（含 Phase 0-3 步驟、A/B 實驗設計）
├── ARCHITECTURE.html               ← 視覺化架構圖（在瀏覽器打開）
├── observability/                  ← Grafana 端 docker-compose（host）
│   ├── docker-compose.yaml         ← 2026-05-04 後只剩 grafana
│   ├── prometheus.yaml             ← 已停用；canonical 在 manifests/monitoring/
│   └── grafana/provisioning/       ← Grafana datasource → host.docker.internal:9090
├── manifests/                      ← K8s manifest
│   ├── monitoring/                 ← 2026-05-04 新增：Prometheus 進 minikube
│   │   ├── 00-namespace.yaml
│   │   ├── 05-prometheus-rbac.yaml
│   │   ├── 10-prometheus-configmap.yaml
│   │   └── 20-prometheus-deployment.yaml  (NodePort 30091)
│   └── (Wren AI manifests 在 ~/wren-k8s/，由 kompose 產出)
├── scripts/
│   ├── start-prometheus-tunnel.sh  ← kubectl port-forward Prometheus → host:9090
│   ├── bench.py                    ← 量測腳本（W3-W4 已用）
│   └── wren_questions.txt
└── reports/                        ← 實驗結果、圖表、報告
    └── 2026-05-03/REPORT.md        ← W3-W4 完成
```

---

## 6. ⭐ 維護規則（重要）

### 6.1 ARCHITECTURE.html 必須隨架構異動同步更新

只要發生以下任一情況，**Claude 必須立即更新 `ARCHITECTURE.html`**，並在更新後告知使用者：

- ➕ **新增元件**：例如 Phase 3 真的引入了 LMCache → 在元件清單、架構圖、資料流加上
- ➖ **移除元件**：例如不跑 omlx 對照組了 → 從架構圖移除，並補在「排除元件」章節
- 🔄 **替換元件**：例如把 vllm-metal 換成 Ollama（Plan B）→ 改架構圖、改 metric 說明、改連線方式
- 🔌 **port / endpoint 變更**：例如 Grafana 改用 :3000 而非 :3001 → 改元件清單與資料流
- 💾 **RAM 預算變化**：例如改用 Qwen2.5-3B → 更新 RAM bar
- 📊 **觀測指標增減**：例如新增了一個 metric → 加到「觀測點」章節
- 🎯 **目標調整**：例如使用者改變了三個核心目標的某一條 → 改最上面的目標說明

更新後的 commit message 建議：`docs(arch): <一行說明改了什麼>`。

### 6.2 PROJECT_PLAN.md 也要保持與 ARCHITECTURE.html 一致

兩份文件的「架構決策」章節（Section 2 of PROJECT_PLAN.md）與 ARCHITECTURE.html 的架構圖必須一致。修改一邊就要修另一邊。

### 6.3 CLAUDE.md（本檔）要記錄重大決策

當發生以下情況，更新本檔的對應章節：
- 新增/移除/延後元件 → 更新 §4
- 文件結構變化 → 更新 §5
- 三個核心目標調整 → 更新 §2

---

## 7. 工作流提示

### 7.1 接到「執行下一步」這類請求時的順序
1. 先讀 `PROJECT_PLAN.md` 確認目前處於哪個 Phase
2. 確認對應 Phase 的前置條件已滿足
3. 執行對應命令前，先用 AskUserQuestion 確認工具安裝/啟動的權限
4. 完成後更新 `PROJECT_PLAN.md` 的進度標記

### 7.2 接到「改架構」這類請求時的順序
1. 先評估改動是否會影響「三個核心目標」 → 如會，先跟使用者討論
2. 改 `ARCHITECTURE.html`（架構圖、元件清單、資料流）
3. 改 `PROJECT_PLAN.md`（對應的 Phase 步驟、RAM 預算、風險表）
4. 改 `CLAUDE.md`（§4 排除/延後元件清單；§5 文件結構）
5. 用 `mcp__cowork__present_files` 把更新過的檔案 link 給使用者

### 7.3 接到「跑量測」這類請求時的順序
1. 確認 vllm-metal、Prometheus、Grafana、Wren AI 都 up（用 `curl /metrics` 與 `kubectl get pods`）
2. 跑 `scripts/bench.py` 對應的實驗
3. 把結果 csv / 圖表存到 `reports/<date>/`
4. 更新 `PROJECT_PLAN.md` 的「實驗結果」段落（如果還沒有，建立一個）

---

## 8. 命名 / 風格慣例

- 程式碼註解、commit message：英文
- Markdown 文件、ARCHITECTURE.html、與使用者對話：**繁體中文**
- 變數命名：snake_case
- K8s namespace：`wren-ai`、`monitoring`、`data`
- Prometheus job_name：`vllm-metal-host`、`wren-ai-via-minikube`

---

## 9. 已知陷阱（踩過一次就記下來）

| 陷阱 | 解決方案 |
|---|---|
| `host.minikube.internal` 在 macOS docker driver 下**舊版**指向 Docker Desktop VM 而非真 host（在 Docker Desktop v29.4.1 + minikube v1.38.1 已修，pod 內可正確解析為真 host gateway 192.168.65.254）| Prometheus 進 minikube 後直接用 `host.minikube.internal:8000` scrape vllm-metal |
| minikube NodePort IP `192.168.49.2:NNNN` 從 macOS host 不可達（docker driver 限制） | host Grafana 用 `kubectl port-forward svc/prometheus -n monitoring 9090:9090`（`scripts/start-prometheus-tunnel.sh`）；不要試 `minikube ip:30091` |
| omlx Homebrew formula 0.3.5+ 都釘 `dflash-mlx@814c4a1`，但該 commit 已從 GitHub 消失 | 改裝 v0.3.4（最後一個沒 dflash-mlx 依賴的版本），`git checkout 3c0345f -- Formula/omlx.rb` 後本地 install |
| omlx 預設 `--host 127.0.0.1` 從 minikube pod 不可達 | `omlx serve --host 0.0.0.0 --port 8001`（即使如此 omlx 也無 Prometheus `/metrics`，不能 scrape，只能 client bench） |
| 同時跑 vllm-metal + omlx 會搶 Metal GPU + memory bandwidth | omlx 不放 brew services 自動啟動；A/B 對照組量測「**串行**」執行 — 跑完 vllm-metal 那組再跑 omlx 那組 |
| Wren AI v0.29.0 wren-ai-service 沒有 `/metrics` endpoint | 不要 scrape；cache 觀察靠 vllm-metal 自身的 metrics |
| Wren AI bootstrap 直接呼叫 wren-ui 的 deployment GraphQL 會回 "No project found"（第一次安裝沒 project 是正常） | 只是初始化噪音，不影響後續使用 |
| Wren AI v0.29.0 的 config.yaml 用 `litellm_llm` + `litellm_embedder`，把 LLM 與 Embedder 拆開設定 | LLM 指 vllm-metal `:8000/v1`，Embedder 指 Ollama `:11434/v1`；`embedding_model_dim` 必須對應 embedder 維度（nomic-embed-text=768） |
| vllm-metal 啟動時 distributed init 選到 VPN/utun 介面（如 10.5.0.x），gloo backend timeout 卡住 | 啟動前 export `VLLM_HOST_IP=127.0.0.1` 強制走 loopback |
| vllm 0.20 移除 `--disable-log-requests` flag | 改用 `--no-enable-log-requests` |
| PROJECT_PLAN.md §4 / §10.1 寫的 `vllm:gpu_cache_usage_perc` 在 vllm 0.20 重命名 | 用 `vllm:kv_cache_usage_perc`（語義相同） |
| 計畫的 `pip install vllm-metal` 在 PyPI 找不到 | 走官方 install.sh：`curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh \| bash`，會建 `~/.venv-vllm-metal` |
| `python -m mlx_lm.convert` 已改入口 | 用 `python -m mlx_lm convert`（中間沒有點），且 `--q-bits 4` 不能寫成 `-q-bits 4` |
| vllm-metal 不能跑在 Linux container（Metal API 限制） | LLM serving 永遠在 host，K8s 只放純 CPU 元件 |
| vLLM CPU backend 在 Apple Silicon 需 source build 且效能差 | 不走這條路，用 vllm-metal |
| Wren AI Helm chart 不太成熟 | 用 `kompose convert` 從 docker-compose.yaml 產生 manifest |
| 16 GB RAM 同時跑 vllm-metal + minikube + GUI app 容易 OOM | 預設 1.5B 模型 + 量測時關 GUI |

---

## 10. 常用命令

> 完整 phase-by-phase 步驟在 `PROJECT_PLAN.md`。本節彙整最常用的「啟動 / 驗證 / 量測」指令，方便快速操作而不用翻 plan。

### 10.1 啟動順序（從零開始，2026-05-04 後）

```bash
# 1. minikube
minikube start --driver=docker --cpus=4 --memory=8192 --disk-size=40g --kubernetes-version=v1.31.0
minikube addons enable ingress metrics-server

# 2. Wren AI + Prometheus（兩個 namespace）
kubectl -n wren-ai apply -f ~/wren-k8s/                        # Wren AI（manifest 已在 disk）
kubectl apply -f ~/Documents/Projects/llm-cache-observation/manifests/monitoring/

# 3. vllm-metal（在 host 上，需先 source venv）
source ~/.venv-vllm-metal/bin/activate
VLLM_HOST_IP=127.0.0.1 python -m vllm.entrypoints.openai.api_server \
  --model ~/models/Qwen2.5-1.5B-Instruct-mlx-q4 \
  --served-model-name qwen2.5-1.5b \
  --host 0.0.0.0 --port 8000 \
  --enable-prefix-caching --kv-cache-metrics --max-model-len 4096 --no-enable-log-requests

# 4. kubectl port-forward Prometheus（量測前必開，獨立 terminal）
~/Documents/Projects/llm-cache-observation/scripts/start-prometheus-tunnel.sh

# 5. Grafana（host docker-compose）
cd ~/Documents/Projects/llm-cache-observation/observability
docker compose up -d

# 6. (對照組量測時才開) omlx
omlx serve --host 0.0.0.0 --port 8001 &
```

關閉順序：`pkill -f "omlx serve"` → `docker compose down` → 結束 port-forward (Ctrl-C) → `pkill -f vllm.entrypoints` → `minikube stop`。

### 10.2 健康檢查（量測前必跑）

```bash
# vllm-metal up & metrics 完整
curl -fsS http://localhost:8000/v1/models | jq .
curl -fsS http://localhost:8000/metrics | grep -E "vllm:(prefix_cache|gpu_cache_usage|time_to_first)" | head

# (對照組量測時) omlx up
curl -fsS http://localhost:8001/health | jq .

# minikube + Wren AI + monitoring 全部 Running
kubectl get pods -A | grep -v Running | grep -v Completed   # 應只剩 header

# Prometheus 4 個 target 都 UP（透過 port-forward）
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job:.labels.job, health}'

# Grafana
open http://localhost:3001    # admin / admin
```

### 10.3 切換 prefix cache On/Off（A/B 實驗 2）

重啟 vllm-metal 並切換 flag（同一個 venv、同一個 model）：

```bash
# Off
python -m vllm.entrypoints.openai.api_server \
  --model ~/models/Qwen2.5-1.5B-Instruct-mlx-q4 \
  --served-model-name qwen2.5-1.5b --host 0.0.0.0 --port 8000 \
  --no-enable-prefix-caching --kv-cache-metrics --max-model-len 4096 --no-enable-log-requests

# On
# 把上面那行的 --no-enable-prefix-caching 換成 --enable-prefix-caching
```

### 10.4 跑 benchmark

```bash
cd ~/Documents/Projects/llm-cache-observation
python scripts/bench.py --api-base http://localhost:8000/v1 \
  --questions scripts/wren_questions.txt \
  --out reports/$(date +%Y-%m-%d)/run.csv
```

### 10.5 取 minikube IP（用於 Prometheus target 設定）

```bash
minikube ip                           # 例如 192.168.49.2
kubectl -n wren-ai get svc            # 找出有 NodePort 的 service
```

如 `minikube ip` 變動，要同步更新 `observability/prometheus.yaml` 並 `docker compose restart prometheus`。

---

## 11. 參考資料（單一事實來源）

當需要查證技術細節時，優先看：
- vLLM Prometheus 指標：https://docs.vllm.ai/en/v0.7.2/getting_started/examples/prometheus_grafana.html
- Wren AI Kubernetes 部署：https://medium.com/wrenai/wren-ai-in-kubernetes-text-to-sql-39b82bda3d34
- vllm-metal plugin：https://github.com/vllm-project/vllm-metal
- Wren AI 官方文件：https://docs.getwren.ai/oss/installation
- Minikube docker driver：https://minikube.sigs.k8s.io/docs/drivers/docker/
