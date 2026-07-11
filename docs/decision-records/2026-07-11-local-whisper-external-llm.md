# Decision Record：本機 Whisper 與外部會議報告 LLM

- 日期：2026-07-11
- 狀態：採用；取代同日「全部外部 API」的轉錄決定

## 背景與問題

使用者指定轉錄應維持原本本機 Whisper，而會議報告 LLM 改用 Google AI Studio 或其他外部 API。原本將轉錄也改為外部 API 的方案不符合最新需求。

## 已評估選項

1. Whisper 與 LLM 都使用外部 API。
2. Whisper 與 LLM 都使用本機模型。
3. 本機 GPU Whisper 轉錄，外部 OpenAI-compatible API 產生會議報告。

## 採用原因

採用方案 3。Whisper 可保留原有逐段預覽、timestamp 與 GPU 轉錄能力；外部 LLM 避免本機 Ollama 佔用 VRAM。預設報告 endpoint 指向 Google AI Studio Gemini 的 OpenAI-compatible API，並可經三個環境變數切換其他供應商。

## 放棄原因

- 方案 1 與使用者要求保留本機 Whisper 衝突。
- 方案 2 會重新引入本機 LLM，與外部 API 要求衝突。

## 取捨與風險

- 逐字稿會送往外部 LLM 作報告整理。
- 首次使用 Whisper model 仍需下載模型，且 GPU 顯存不足時可能需要縮小模型或改用 CPU。
- 外部 LLM 的可用性、模型與費用由供應商決定。

## 驗證方式

- Docker image 使用 NVIDIA CUDA runtime 並安裝 faster-whisper。
- CTranslate2 runtime 顯示可用 CUDA device。
- 單元測試 mock 本機 Whisper segments 與外部 Chat Completions request。

## Rollback 條件

若 GPU Whisper 不穩定，可透過 `WHISPER_DEVICE=cpu` 及 `WHISPER_COMPUTE_TYPE=int8` 暫時退回 CPU；不得因此將會議報告改回本機 Ollama。

## 資訊來源 / 參考資料

- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/meeting_service.py`，Whisper 與外部 Chat Completions 流程。
- 本地：`/home/robertlo/Desktop/personal/Tool/meeting/app/model_selector.py`，GPU/Whisper 與 Gemini 設定。
- Google 官方：[OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)，存取日期：2026-07-11。
