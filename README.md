# 會議記錄整理工具

本專案是一個 Docker 化的本機網頁工具，用於將會議音訊或影片整理成 Markdown 會議報告。流程為：

1. 上傳音訊或影片檔
2. 使用 `faster-whisper` 在本機轉錄逐字稿
3. 在處理狀態區即時預覽逐字稿與報告草稿
4. 使用 Docker 內的 Ollama 地端模型整理成會議報告
5. 下載 Markdown 報告與逐字稿

## 目前硬體建議

在本機偵測到的硬體條件：

| 項目 | 偵測結果 |
| --- | --- |
| CPU | Intel Core i7-11700，16 threads |
| RAM | 約 62 GB |
| GPU | NVIDIA GeForce RTX 4070 Ti，約 12 GB VRAM |

依此配置，預設建議：

| 用途 | 模型 / 設定 | 原因 |
| --- | --- | --- |
| 語音轉文字 | `faster-whisper medium`、`cuda`、`float16` | 12GB VRAM 可支撐較好的轉錄品質，同時保留 LLM 推論空間 |
| 會議整理 | `qwen2.5:7b` | 對中文整理能力與本機推論成本較平衡 |

若需要更快速度，可改用 `WHISPER_MODEL=small` 或 `OLLAMA_MODEL=qwen2.5:3b`。若需要更高品質且可接受更長處理時間，可嘗試 `WHISPER_MODEL=large-v3`。

## 系統需求

- Docker 與 Docker Compose
- NVIDIA GPU Driver
- NVIDIA Container Toolkit
- 可連線下載 Docker image、Whisper 模型與 Ollama 模型

Ollama 官方 Docker 文件建議 NVIDIA GPU 使用 `--gpus=all`，並需先安裝與設定 NVIDIA Container Toolkit。

## 快速啟動

```bash
docker compose up --build
```

開啟：

```text
http://localhost:8001
```

若要改成其他對外 port，可在 `.env` 設定：

```bash
APP_PORT=8002
```

第一次使用時會下載：

- Docker base images
- `faster-whisper` 模型
- Ollama 模型，例如 `qwen2.5:7b`

第一次處理會議可能需要較久，後續會使用 Docker volume 快取。

## 設定模型

可建立 `.env` 或直接在 `docker-compose.yml` 的 `environment` 覆寫：

```bash
APP_PORT=8001
WHISPER_MODEL=medium
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
OLLAMA_MODEL=qwen2.5:7b
PREVIEW_LIMIT=12000
```

自動選擇規則位於 `app/model_selector.py`：

| 條件 | Whisper | Ollama |
| --- | --- | --- |
| GPU VRAM >= 20GB | `large-v3` | `qwen2.5:14b` |
| GPU VRAM >= 10GB | `medium` | `qwen2.5:7b` |
| GPU VRAM >= 6GB | `small` | `qwen2.5:7b` |
| 無 GPU，RAM >= 24GB | `small` | `qwen2.5:3b` |
| 其他 | `base` | `qwen2.5:1.5b` |

`PREVIEW_LIMIT` 控制 `/api/jobs/{job_id}` 回傳的預覽文字長度，預設保留最新 12000 個字元，避免長會議在前端輪詢時傳輸過大。

## API

### 健康狀態

```http
GET /api/health
```

回傳硬體偵測、模型建議與 Ollama 連線狀態。

### 建立整理工作

```http
POST /api/jobs
Content-Type: multipart/form-data
```

欄位：

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `file` | 是 | 音訊或影片檔 |
| `meeting_title` | 否 | 會議名稱 |
| `language` | 否 | `zh`、`en` 或空白自動偵測 |
| `output_language` | 否 | 預設 `繁體中文` |
| `report_mode` | 否 | `standard`、`executive`、`action` |

### 查詢工作

```http
GET /api/jobs/{job_id}
```

處理中會回傳：

- `status`：`queued`、`running`、`done` 或 `failed`
- `stage`：目前處理階段
- `progress`：整體進度百分比
- `transcript_preview`：逐字稿預覽；轉錄期間會隨段落產生更新
- `report_preview`：報告預覽；分段摘要或最終報告完成後更新
- `preview_updated_at`：最近一次預覽更新時間

完成後會額外回傳：

- `report_url`
- `transcript_url`

## 資料保存位置

容器內資料目錄為 `/data`，本機掛載為：

```text
./data
```

主要輸出：

- `./data/uploads/`：上傳原始檔
- `./data/reports/`：Markdown 報告與逐字稿

## 已知限制

- 工作狀態目前保存在記憶體，容器重啟後仍保留報告檔，但前端工作狀態不會恢復。
- 即時預覽採前端輪詢 job 狀態；逐字稿會逐段更新，報告預覽會在 Ollama 完成分段或最終生成後更新，尚未做到 token-by-token streaming。
- 報告由 AI 自動整理，講者、期限、專有名詞與決議仍需人工覆核。
- 長會議會先分段摘要再合併，可能失去部分細節。
- 第一次下載模型會花較久時間，取決於網路與磁碟速度。

## 驗證方式

建置與啟動：

```bash
docker compose up --build
```

檢查 API：

```bash
curl http://localhost:8001/api/health
```

使用指定音檔建立工作並檢查預覽欄位：

```bash
curl -F "file=@/home/robertlo/Downloads/voice_207078.aac" \
  -F "meeting_title=即時預覽最終驗證" \
  -F "language=zh" \
  -F "output_language=繁體中文" \
  -F "report_mode=standard" \
  http://localhost:8001/api/jobs

curl http://localhost:8001/api/jobs/{job_id}
```

已驗證結果：

- `status=done`
- `transcript_preview` 有內容
- `report_preview` 有內容
- `report_url` 與 `transcript_url` 可下載 Markdown 檔案

檢查 GPU 是否進入容器：

```bash
docker compose exec meeting-app nvidia-smi
docker compose exec ollama nvidia-smi
```

瀏覽器驗證：

- Desktop 與 mobile viewport 均需確認沒有水平溢出
- 上傳表單、狀態輪詢、報告下載連結需可操作
- Ollama 尚未就緒或處理失敗時需顯示錯誤訊息
- 截圖輸出位置：`/home/robertlo/Desktop/personal/Tool/meeting/validation_screenshots/desktop-preview.png`、`/home/robertlo/Desktop/personal/Tool/meeting/validation_screenshots/mobile-preview.png`

## 資訊來源 / 參考資料

- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/model_selector.py`，用途：硬體偵測與模型選擇規則
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/meeting_service.py`，用途：轉錄、Ollama 呼叫、報告格式
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/main.py`，用途：job API、預覽欄位與下載 endpoint
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/static/index.html`，用途：即時預覽界面結構
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/static/app.js`，用途：前端輪詢與預覽切換邏輯
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/static/styles.css`，用途：預覽區與 responsive dark UI 樣式
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/docker-compose.yml`，用途：Docker 與 GPU 部署設定
- 指令：`docker compose build && docker compose up -d`，用途：重建並啟動最終驗證容器
- 指令：`curl -F "file=@/home/robertlo/Downloads/voice_207078.aac" ... http://localhost:8001/api/jobs`，用途：以指定 AAC 音檔驗證上傳、轉錄、摘要與預覽欄位
- 指令：`npx playwright screenshot ...`，用途：以實際 Chromium 渲染驗證 desktop/mobile UI
- 網路：[Ollama Docker 官方文件](https://docs.ollama.com/docker)，用途：NVIDIA GPU Docker 執行方式與 NVIDIA Container Toolkit 前置條件，存取日期：2026-07-02
