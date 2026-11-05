# Decision Record: Google Gemini 全 API 會議處理流程

- 背景與問題：
  - 使用者要求先串 Google API，暫時不要使用本地算力。
  - 既有最新版本使用本機 `faster-whisper` 轉錄，報告整理走外部 LLM，仍會依賴 CUDA / GPU / Whisper model cache。

- 已評估選項：
  - 保留本機 `faster-whisper`，只把報告整理接 Google Gemini。
  - 使用 OpenAI-compatible `/audio/transcriptions` 形式接 Google。
  - 使用 Google Gemini native audio understanding，透過 Files API 上傳檔案，再用 `models.generate_content` 做轉錄與報告整理。

- 採用原因：
  - Google 官方文件提供 Gemini audio input、Files API upload、structured output 與 transcript prompt 範例。
  - 目前容器內的 `google-genai==1.53.0` 有 `files` 與 `models.generate_content` 介面，沒有 `interactions` 介面；採用可在現有套件版本中驗證的 SDK surface。
  - Google AI Studio / Gemini API 沒有等同 OpenAI Whisper 的 `/audio/transcriptions` contract；使用 native API 較符合官方支援路徑。
  - 移除本機 Whisper / CUDA 後，Docker image 較小，部署不需要 GPU runtime。

- 放棄原因：
  - 保留本機 Whisper 不符合「先不要用到本地算力」。
  - `/audio/transcriptions` 是 OpenAI 風格假設，無法作為 Google Gemini 的穩定 contract。

- 取捨與風險：
  - 優點：本機不需 GPU/CPU 推論、模型下載或 Ollama；設定集中在 Gemini API key 與模型名稱。
  - 風險：Gemini 轉錄屬於 multimodal audio understanding，不是專用 Speech-to-Text API；逐字稿 timestamp、speaker diarization 與長音訊品質需用實際會議檔驗證。
  - 隱私：音訊/影片與逐字稿會送到 Google Gemini API，正式使用前需確認資料政策與會議內容敏感性。

- 驗證方式：
  - Docker build。
  - Docker 內 unit tests，mock Google SDK 與 `/models` HTTP response。
  - `docker compose config --quiet` 驗證 compose 設定。
  - 實際 API 驗證可用 `/api/health` 與一個非敏感短音訊檔測試。

- rollback 條件：
  - 若 Gemini 轉錄品質或成本不符合需求，可回退至 `dfd3e6d` 的本機 Whisper + 外部報告流程，或再切到 Google Cloud Speech-to-Text。

- 資訊來源 / 參考資料：
  - `/home/robertlo/Desktop/personal/Tool/meeting/app/meeting_service.py`
  - `/home/robertlo/Desktop/personal/Tool/meeting/app/model_selector.py`
  - `/home/robertlo/Desktop/personal/Tool/meeting/docker-compose.yml`
  - [Google Gemini Audio understanding](https://ai.google.dev/gemini-api/docs/audio)
  - [Google Gemini Text generation](https://ai.google.dev/gemini-api/docs/text-generation)
