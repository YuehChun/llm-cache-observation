# LLM KV Cache 觀測實驗專案規劃

> **目標環境**：MacBook (Apple Silicon M-series, 16GB unified memory)
> **核心目標**：以 Wren AI 為真實 workload，量測 LLM 推論的 KV cache hit rate、TTFT、prefill 時間等指標
> **撰寫日期**：2026-05-03

---

## 1. 專案總覽

### 1.1 目標
1. **基線比較**：在相同 workload 下，比較「啟用 / 關閉 prefix cache」對 TTFT、吞吐量的影響。
2. **真實場景觀察**：以 Wren AI（Text-to-SQL）的真實 prompt 模式為 workload，觀察 cache 命中規律。
3. **K8s 端到端部署**：把整個 stack（Wren AI + LLM serving + observability）放到 minikube 上，研究分散式 serving 架構。

### 1.2 你原始指定的元件 vs 可行性結論

| 元件 | 原始計畫 | 可行性結論 | 處置 |
|---|---|---|---|
| **minikube** | ✅ 安裝 | ✅ 完全可行（建議 8GB / 4 CPU） | 採用 |
| **Wren AI** | ✅ 作為 workload | ✅ 可行（4 個 container, 約 8-10GB RAM） | 採用 |
| **Qwen2.5-1.5B/3B** | ✅ 本地模型 | ✅ 推薦 Qwen2.5-3B-Instruct (Q4 量化) | 採用 |
| **vLLM (CUDA 主分支)** | 隱含使用 | ⚠️ CUDA path 在 Apple Silicon 不支援。**CPU backend 其實有 ARM64 (NEON) 支援**（FP32/FP16/BF16，透過 oneDNN + ACL），但要從 source build、效能遠不及 Metal、且需要在 ARM64 容器內編譯 | **首選 vllm-metal**；ARM64 CPU 路徑作為 Phase 3 備案 |
| **vllm-metal** plugin | 未提及 | ✅ 2026/1 釋出，支援 Metal GPU + 部分 prefix cache metrics；但**只能跑在 macOS host，無法進 Linux container/minikube** | 跑在 host |
| **LMCache for k8s** | ✅ 安裝 | ⚠️ Helm chart 主要針對 NVIDIA GPU；ARM64 image 與純 CPU 路徑未確認 | **延後 / 條件性實驗** |
| **llm-d** | ✅ 安裝 | ❌ 硬性需要 NVIDIA A100+ / AMD MI250 / TPU v5e+，**Apple Silicon 完全無法跑** | **移除** |
| **omlx** (jundot/omlx) | ✅ 安裝 | ✅ 完全可行（macOS 原生 MLX server, menu bar app），但**不是 K8s 元件** | **作為基準對照，不放進 k8s** |
| **Mooncake** (kvcache-ai) | ✅ 安裝 | ❌ 主要針對多節點 GPU 叢集，CPU build 雖可選但 ARM64 未驗證；單機 16GB Mac 無意義 | **移除** |
| **Prometheus + Grafana** | ✅ | ✅ 完全可行 | 採用 |

### 1.3 為什麼移除 llm-d / Mooncake / 部分簡化 LMCache

- **llm-d** 的設計目標是「在資料中心級加速器上做 disaggregated prefill/decode + KV cache 池化」。它最小門檻就是 A100 / MI250 / TPU v5e，這個專案的目標機器（16GB MacBook）完全達不到。硬上等於放一個跑不起來的 CRD 在叢集裡。
- **Mooncake** 的價值在於「跨節點 GPU 共享 KV cache 池」，它解決的是「我有 32 張 H100 但 KV 互不共用」這種問題，單機環境用它沒有觀測意義。
- **LMCache** 的「KV cache 分層 (GPU→CPU→SSD)」是真實有研究價值的；但它的官方 Helm chart 與多數 Docker image 都針對 CUDA。我們會保留它作為 Phase 3 的條件性實驗（在純 CPU 模式下跑 vLLM x86 emulation 來驗證），但 **這部分的優先序最低**。

---

## 2. 推薦架構

### 2.1 高層架構圖（建議方案：混合架構）

```mermaid
flowchart TB
  subgraph host["macOS Host (Apple Silicon, 16GB)"]
    direction TB
    vllm["vllm-metal server<br/>:8000 OpenAI API + /metrics<br/>Qwen2.5-3B-Instruct (MLX, Q4)"]
    omlx["(對照組) omlx server<br/>:port Y - 同模型不同引擎"]
  end

  subgraph dc["Docker Compose on host"]
    prom[Prometheus :9090]
    graf[Grafana :3001]
  end

  subgraph mini["minikube (8GB / 4 CPU)"]
    direction TB
    subgraph wren["Namespace: wren-ai"]
      ui[wren-ui :3000]
      svc[wren-ai-service<br/>:8000 /metrics NodePort 30090]
      eng[wren-engine]
      ibis[wren-ibis-server]
    end
    subgraph data["Namespace: data"]
      pg[(Postgres demo DB)]
      qd[(Qdrant vector DB)]
    end
  end

  ui --> svc
  svc -->|Text-to-SQL prompts| vllm
  svc --> qd
  svc --> eng
  eng --> ibis
  ibis --> pg
  prom -.host.docker.internal:8000.-> vllm
  prom -.minikube ip :30090.-> svc
  graf --> prom

  classDef host fill:#fef3c7,stroke:#92400e
  classDef mini fill:#dbeafe,stroke:#1e40af
  class host host
  class mini mini
```

### 2.2 為什麼是這個架構

1. **vllm-metal 必須在 host**：Metal API 是 Apple GPU 的私有介面，無法穿透 Linux container，所以 LLM serving 不能放進 minikube。
2. **Wren AI 與 observability 在 minikube**：這部分純 CPU、跨平台，K8s 化沒問題；同時也滿足你「研究 K8s 部署」的目標。
3. **跨界 scrape 的網路陷阱**（重要）：minikube 在 macOS + docker driver 下，pod 內的 `host.minikube.internal` 解析到的是 **Docker Desktop VM**，不是真正的 macOS host！要打到 host 上的 vllm-metal 有兩條路：
   - **(推薦) 把 Prometheus 也放在 macOS host 上**（用 Docker Compose 跑），用 `host.docker.internal:8000` 直連 vllm-metal；要看 Wren AI 的 metric 就走 `minikube ip` 或 NodePort。
   - 在 Docker Desktop VM 內跑一個 socat / nginx 把 `host.docker.internal:8000` 反向代理出來，minikube pod 才能透過 `host.minikube.internal` 經兩跳到達 host。複雜易錯，不建議。
4. **omlx 作為對照組**：用同一個模型在 omlx 上跑，比較 vllm-metal vs omlx 的 cache 行為與 TTFT，附加價值不大但成本很低。

### 2.3 RAM 預算（總 16GB）

| 元件 | 預估佔用 | 說明 |
|---|---|---|
| macOS 系統 + Chrome / IDE | 5 GB | 保留 baseline |
| vllm-metal + Qwen2.5-3B Q4 | 4 GB | 模型 ~2GB + KV cache + 引擎 |
| minikube VM (4 CPU / 8GB) | 8 GB | 內含 Wren AI 4 容器 (~5GB) + Prom/Grafana (~1.5GB) + Postgres/Qdrant (~1.5GB) |
| **小計** | **17 GB** | **超載 1 GB** → 需要關閉部分 GUI / 用 Qwen2.5-1.5B 替代 |

**緩解方案**：
- 預設用 **Qwen2.5-1.5B-Instruct Q4**（~1GB），保留 buffer。
- 把 Postgres demo DB 改用 SQLite + Wren engine 內建支援。
- 量測時關閉 Chrome 與其他 GUI app。

---

## 3. 階段性實作計畫

### Phase 0：基礎環境（預估 30 分鐘）

#### 0.1 安裝工具鏈

```bash
# Homebrew 工具
brew install --cask docker          # Docker Desktop（minikube 的 driver）
brew install minikube kubectl helm
brew install jq watch httpie         # 後續測試與觀測會用到

# Python 環境（給 vllm-metal）
brew install python@3.11
python3.11 -m venv ~/.venvs/vllm-metal
source ~/.venvs/vllm-metal/bin/activate
pip install vllm-metal               # 從 PyPI 安裝 plugin
```

#### 0.2 啟動 minikube

```bash
minikube start \
  --driver=docker \
  --cpus=4 \
  --memory=8192 \
  --disk-size=40g \
  --kubernetes-version=v1.31.0

# 開啟需要的 addons
minikube addons enable ingress
minikube addons enable metrics-server

# 驗證
kubectl get nodes
kubectl get pods -A
```

#### 0.3 建立 namespace

```bash
kubectl create namespace wren-ai
kubectl create namespace monitoring
kubectl create namespace data
```

---

### Phase 1：在 host 上跑 vllm-metal（預估 45 分鐘）

#### 1.1 下載 Qwen2.5 模型（MLX 量化版）

```bash
pip install mlx-lm
python -m mlx_lm.convert --hf-path Qwen/Qwen2.5-1.5B-Instruct --quantize -q-bits 4 \
  --mlx-path ~/models/Qwen2.5-1.5B-Instruct-mlx-q4
```

#### 1.2 啟動 vllm-metal server（基線：啟用 prefix cache）

```bash
source ~/.venvs/vllm-metal/bin/activate
python -m vllm.entrypoints.openai.api_server \
  --model ~/models/Qwen2.5-1.5B-Instruct-mlx-q4 \
  --served-model-name qwen2.5-1.5b \
  --host 0.0.0.0 --port 8000 \
  --enable-prefix-caching \
  --max-model-len 4096 \
  --disable-log-requests
# /metrics 端點會自動暴露在 :8000/metrics
```

#### 1.3 煙霧測試

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-1.5b","messages":[{"role":"user","content":"Hello"}],"max_tokens":32}'

# 確認 metrics
curl -s http://localhost:8000/metrics | grep -E "vllm:(prefix_cache|gpu_cache_usage|time_to_first)"
```

> ⚠️ **驗證點**：vllm-metal 對 `vllm:prefix_cache_hits/_queries` 與 `vllm:time_to_first_token_seconds` 的支援還在演進中。如果這些 metric 缺漏，回退到 Phase 2 的 Plan B：Ollama + 自寫 metrics exporter。

---

### Phase 2：在 minikube 部署 Wren AI + Prometheus + Grafana（預估 90 分鐘）

#### 2.1 Wren AI 部署

Wren AI 官方有 Helm chart（在 `Canner/WrenAI` repo 的 `deploy/helm`），但更穩定的做法是用 docker-compose 文件改寫成 K8s manifest。建議用 [kompose](https://github.com/kubernetes/kompose)：

```bash
git clone https://github.com/Canner/WrenAI.git ~/code/WrenAI
cd ~/code/WrenAI/docker

# 改 .env：把 LLM_PROVIDER 指向 host 上的 vllm-metal
cp .env.example .env
cat >> .env <<EOF
LLM_PROVIDER=openai_compatible
OPENAI_BASE_URL=http://host.minikube.internal:8000/v1
OPENAI_API_KEY=dummy
LLM_MODEL=qwen2.5-1.5b
EOF

# 用 kompose 轉 manifest
brew install kompose
kompose convert -f docker-compose.yaml -o ~/wren-k8s/

# 套用
kubectl -n wren-ai apply -f ~/wren-k8s/
kubectl -n wren-ai get pods -w
```

> 註：minikube 在 Mac 用 docker driver 時，host 對 pod 的 hostname 是 `host.minikube.internal`（不是 `host.docker.internal`，這點容易踩雷）。

#### 2.2 Prometheus + Grafana 部署（**改放在 macOS host，用 Docker Compose**）

由於 minikube pod 無法直接打到 host 上的 vllm-metal（見 2.2 網路陷阱），把 observability stack 改放 host 上、scrape 兩邊：

```yaml
# ~/Documents/Projects/llm-cache-observation/observability/docker-compose.yaml
services:
  prometheus:
    image: prom/prometheus:v2.55.0
    ports: ["9090:9090"]
    volumes:
      - ./prometheus.yaml:/etc/prometheus/prometheus.yml:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
  grafana:
    image: grafana/grafana:11.3.0
    ports: ["3001:3000"]
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    depends_on: [prometheus]
```

```yaml
# prometheus.yaml
global:
  scrape_interval: 5s
scrape_configs:
  - job_name: 'vllm-metal-host'
    static_configs:
      - targets: ['host.docker.internal:8000']  # 直接打 host 上的 vllm-metal
  - job_name: 'wren-ai-via-minikube'
    static_configs:
      # 用 `minikube ip` 抓出來的 IP + NodePort，替換下面的 192.168.49.2
      - targets: ['192.168.49.2:30090']
```

```bash
cd ~/Documents/Projects/llm-cache-observation/observability
docker compose up -d
open http://localhost:9090/targets   # 應看到 vllm-metal-host UP
open http://localhost:3001           # Grafana
```

> 替代方案：如果你真的要把 Prometheus 放在 minikube 內（為了「100% K8s 化」的研究目標），可以用 [`socat` sidecar pattern](https://github.com/kubernetes/minikube/issues/16989) 在 Docker Desktop 內反代 `host.docker.internal:8000`，但維護成本明顯較高。建議第一輪先用上面的 Docker Compose 方案。

#### 2.3 Grafana 連入

```bash
minikube service -n monitoring kps-grafana --url
# 用 admin / admin 登入，匯入 vLLM 官方 dashboard
```

---

### Phase 3（選做）：LMCache offload 對照實驗

在 Apple Silicon 上跑 LMCache 有兩條路，**都不理想**，列出供你決定：

| 方案 | 做法 | 缺點 |
|---|---|---|
| A. x86 emulation | 在 minikube 用 `--platform=linux/amd64` 跑 vLLM CPU image + LMCache | 慢 5-10 倍，KV cache 行為仍可觀測，TTFT 數據沒有絕對意義 |
| B. 雲端 spot GPU | 同樣的 manifest 拿到 GKE/EKS 一台 T4 spot 跑 30 分鐘 | 成本 ~$0.5；環境真實但偏離「本地」目標 |

**建議**：Phase 3 預設不做。先把 Phase 0-2 做出可重現的數據，再決定是否進 Phase 3。

---

## 4. Prometheus 指標與 Grafana 儀表板設計

> 以下 metric 名稱以 vLLM 主分支為準。若 vllm-metal 未實作某個，會在「驗證」步驟發現並調整。

### 4.1 核心指標分類（對應你的需求）

#### A. 命中率（Cache Hit Rate）
```promql
# 5 分鐘滾動命中率
sum(rate(vllm:prefix_cache_hits_total[5m]))
  / sum(rate(vllm:prefix_cache_queries_total[5m]))
```

#### B. KV 區塊重複利用
```promql
# 區塊兩次存取平均間隔（秒）
histogram_quantile(0.50, sum by (le) (rate(vllm:kv_block_reuse_gap_seconds_bucket[5m])))

# 區塊存活時間 p95
histogram_quantile(0.95, sum by (le) (rate(vllm:kv_block_lifetime_seconds_bucket[5m])))

# 被驅逐前的閒置時間（揪出 stranded cache）
histogram_quantile(0.90, sum by (le) (rate(vllm:kv_block_idle_before_evict_seconds_bucket[5m])))
```

#### C. 容量
```promql
vllm:gpu_cache_usage_perc        # 0~1，逼近 1 代表記憶體飽和
```

#### D. 延遲
```promql
# TTFT p50 / p95
histogram_quantile(0.50, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))

# Prefill 時間
histogram_quantile(0.95, sum by (le) (rate(vllm:request_prefill_time_seconds_bucket[5m])))

# 吞吐量
sum(rate(vllm:generation_tokens_total[5m]))
```

### 4.2 Grafana Dashboard 結構

| Row | Panels |
|---|---|
| **Overview** | Hit rate %、KV usage %、TTFT p95、Throughput tokens/s |
| **Cache Behavior** | Reuse gap histogram、Lifetime histogram、Eviction reason 計數 |
| **Latency Distribution** | TTFT heatmap、Prefill time heatmap |
| **System** | CPU / RAM (kube-state-metrics)、Pod restarts |

> 匯入順序：先用官方 [vllm-project Grafana JSON](https://docs.vllm.ai/en/v0.7.2/getting_started/examples/prometheus_grafana.html)，再加上自訂的 cache behavior 那一 row。

---

## 5. A/B 實驗設計

要證明「KV cache 真的有用」，必須做受控比較。建議三組實驗：

### 5.1 實驗 1：Cold vs Warm
**目的**：證明 prefix cache 能降低 TTFT。

1. **Cold**：重啟 vllm-metal，立刻發送一個長 prompt（>1000 tokens 的 system prompt + Wren AI 的 schema description），記 TTFT。
2. **Warm**：保持同 system prompt，連續發 10 個只改尾段（user question）的 request，記每次 TTFT。
3. **預期**：Warm 的 TTFT 應比 Cold 低 50-90%，`vllm:prefix_cache_hits` 對應上升。

### 5.2 實驗 2：APC On vs Off
**目的**：量化 prefix cache 對整體 throughput 的貢獻。

1. 用同一份 50 個 Wren AI prompts 的腳本（共享 schema prefix）。
2. 跑兩次：`--enable-prefix-caching` vs `--no-enable-prefix-caching`。
3. 量：總時間、平均 TTFT、p95 TTFT、tokens/s。
4. **預期**：APC On 的吞吐量應提升 1.5-3x。

### 5.3 實驗 3：Wren AI 真實 workload
**目的**：在 Wren AI 真實使用模式下觀察 cache 行為。

1. 建立一個 demo Postgres，灌 Wren AI tutorial 用的 e-commerce schema。
2. 寫一個 query 腳本，模擬「使用者連續問 30 個關於同一個 schema 的問題」。
3. 觀察：hit rate 是否隨時間單調上升？哪些 prompt 結構命中率高？
4. 產出：一份「Wren AI workload 下 cache 行為觀察報告」。

### 5.4 量測腳本範例

`bench.py`（簡化版）：

```python
import time, requests, json, statistics

PROMPTS = [
    {"system": SCHEMA_DESC, "user": q}
    for q in load_questions("wren_questions.txt")
]

def run(api_base):
    ttfts = []
    for p in PROMPTS:
        t0 = time.time()
        r = requests.post(f"{api_base}/chat/completions",
            json={"model":"qwen2.5-1.5b",
                  "messages":[{"role":"system","content":p["system"]},
                              {"role":"user","content":p["user"]}],
                  "stream": True, "max_tokens": 256},
            stream=True)
        for line in r.iter_lines():
            if line and b"data:" in line:
                ttfts.append(time.time() - t0)
                break
    return ttfts

ttfts = run("http://localhost:8000/v1")
print(f"p50={statistics.median(ttfts):.3f}s  p95={sorted(ttfts)[int(len(ttfts)*.95)]:.3f}s")
```

---

## 6. 風險與替代方案

| 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|
| vllm-metal 的 prefix cache metrics 不完整 | 中 | 高 | 退回 Plan B：用 Ollama + 自寫 exporter；或追蹤上游 issue 等修復 |
| 16GB RAM 同時跑 minikube + vllm-metal 會 OOM | 中 | 中 | 改用 1.5B 模型；關閉 GUI app；把 Postgres 抽出來用 SQLite |
| Wren AI 對自訂 OpenAI-compatible endpoint 的支援有 quirk | 中 | 中 | 改用 LiteLLM proxy 在中間做 schema 適配 |
| `host.minikube.internal` 解析到 Docker Desktop VM 而非真 host（已知設計） | 確定 | 高 | 已採用「Prometheus 放 host」方案規避；如要強制全 K8s 化用 socat sidecar |
| LMCache helm chart 在 ARM64 minikube 上 image pull 失敗 | 高 | 低 | Phase 3 本來就標 optional，可以放棄 |

### Plan B 全圖
如果 vllm-metal 整體不穩定：
- 把 vllm-metal 換成 **Ollama**（成熟、穩定、ARM 原生），但 Ollama 沒原生 cache metrics。
- 寫一個小 FastAPI proxy，在 Ollama 前面攔截 request：
  - 自己記 `requests_total`、`time_to_first_token_seconds`
  - 用「prompt prefix hash」自己算「prefix collision rate」當作 cache hit 的近似
- 這個 proxy 暴露 `/metrics`，Prometheus 直接 scrape 它。
- 缺點：少了 vLLM 真實的 KV-block-level metrics（reuse gap / lifetime），但 hit rate 與 TTFT 的觀察仍可成立。

---

## 7. Timeline

| 週次 | 工作 | 產出 |
|---|---|---|
| W1 | Phase 0 + Phase 1 | minikube 跑起來；vllm-metal serve Qwen 通過 smoke test；確認哪些 metric 真的有 |
| W2 | Phase 2 | Wren AI 在 minikube 跑起來能對 host vllm-metal 出問題；Grafana dashboard 看到 hit rate 與 TTFT |
| W3 | 實驗 1 + 2 | Cold/Warm 與 APC On/Off 數據 + 圖表 |
| W4 | 實驗 3 + 報告 | Wren AI 真實 workload 觀察報告，含結論與建議 |

---

## 8. 交付物

1. **本規劃文件**（PROJECT_PLAN.md）
2. **腳本與 manifest**：放在 `~/Documents/Projects/llm-cache-observation/{scripts,manifests,grafana}/`
3. **實驗報告**（待 W4）：含 raw numbers + 圖表 + 結論
4. **重現步驟**：從乾淨 Mac 開始能跑起來的 README

---

## 9. 主要參考來源

- [Wren AI GitHub - Canner/WrenAI](https://github.com/Canner/WrenAI)
- [Wren AI 在 Kubernetes 部署文章](https://medium.com/wrenai/wren-ai-in-kubernetes-text-to-sql-39b82bda3d34)
- [vLLM Prometheus + Grafana 範例](https://docs.vllm.ai/en/v0.7.2/getting_started/examples/prometheus_grafana.html)
- [vLLM Production Stack (含 LMCache)](https://github.com/vllm-project/production-stack)
- [vllm-metal plugin](https://github.com/vllm-project/vllm-metal)
- [LMCache Helm Chart 安裝](https://blog.lmcache.ai/en/2025/01/21/high-performance-and-easy-deployment-of-vllm-in-k8s-with-vllm-production-stack/)
- [llm-d 架構文件 (確認 GPU 需求)](https://llm-d.ai/docs/architecture)
- [Mooncake KVCache-centric Architecture](https://github.com/kvcache-ai/Mooncake)
- [omlx (jundot/omlx) - Apple Silicon 原生 MLX server](https://github.com/jundot/omlx)
- [Minikube 官方安裝](https://minikube.sigs.k8s.io/docs/start/)

---

## 10. 下一步建議

如果你同意這份規劃，建議我們從以下任一個切入點開始實際執行：

- **A. 從 Phase 0 開始**：我來實際跑 `brew install` 與 `minikube start`，把基礎建設跑起來。
- **B. 先驗證 vllm-metal**：我先在 host 上把 vllm-metal + Qwen2.5-1.5B 跑起來，確認 metrics 完整度（這是整個專案最大不確定性）。
- **C. 直接寫 Phase 2 的 Wren AI manifest**：先把 K8s 部分寫清楚，等驗證 LLM serving 後再串起來。

我推薦 **B**，因為 vllm-metal 的 metric 完整度是這個專案的單點故障；如果它不行，我們要早點轉到 Plan B（Ollama + 自寫 exporter），不要先把 minikube 都建好才發現要重來。
