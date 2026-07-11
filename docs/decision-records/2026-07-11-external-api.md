# Decision Record：移除本地模型並採用外部 API

- 日期：2026-07-11
- 狀態：採用

## 背景與問題

既有服務在 Docker 內載入 faster-whisper，並依賴另一個 Ollama container。這會占用本機 GPU、需要模型下載與維護，也與「不要再用本地推論」的需求衝突。

## 已評估選項

1. 保留 faster-whisper，只將 Ollama 改成外部 API。
2. 保留 Ollama container，將其改連遠端模型服務。
3. 轉錄與會議整理都改成外部 OpenAI-compatible API。

## 採用原因

採用方案 3。服務以 `POST /audio/transcriptions` 取得逐字稿，再以 `POST /chat/completions` 產生報告；endpoint、API key 環境變數與兩個 model 都可設定。容器不再配置 GPU、Ollama service 或 Whisper cache。

## 放棄原因

- 方案 1 仍有本地語音模型與 GPU 佔用，不符合需求。
- 方案 2 仍需維護本地 Ollama runtime，且無法保證實際推論不在本機。

## 取捨與風險

- 音訊、影片與逐字稿會離開本機，必須確認供應商的資料政策。
- API 可用性、rate limit、媒體大小與費用由外部供應商決定。
- 並非所有 OpenAI-compatible 供應商都提供 transcription endpoint 或 `verbose_json` segments；設定可覆寫 response format，缺少 segments 時仍保存文字稿。

## 驗證方式

- Docker build 不包含 `faster-whisper`、CUDA base image 或 Ollama service。
- 單元測試 mock multipart transcription、Chat Completions、模型健康檢查與缺少 API key 的錯誤處理。
- 以 `/api/health` 檢查外部 endpoint 與兩個設定模型。

## Rollback 條件

若外部 API 的隱私、成本或功能不符合需求，可回到 commit 前版本；不要同時啟用舊 Ollama 與新 external adapter，以避免無意間重新使用本地推論。

## 資訊來源 / 參考資料

- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/meeting_service.py`，既有 Ollama 與 Whisper 呼叫點。
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/docker-compose.yml`，既有 Ollama service 與 GPU 設定。
- OpenAI 官方：[Speech to text](https://developers.openai.com/api/docs/guides/speech-to-text)，存取日期：2026-07-11。
- OpenAI 官方：[GPT-4o mini Transcribe](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe)，存取日期：2026-07-11。
