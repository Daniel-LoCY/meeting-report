const form = document.querySelector("#uploadForm");
const submitButton = form.querySelector("button[type='submit']");
const refreshHealthButton = document.querySelector("#refreshHealth");
const hardwareFacts = document.querySelector("#hardwareFacts");
const modelReason = document.querySelector("#modelReason");
const ollamaStatus = document.querySelector("#ollamaStatus");
const jobIdLabel = document.querySelector("#jobId");
const progressBar = document.querySelector("#progressBar");
const progressText = document.querySelector("#progressText");
const jobMessage = document.querySelector("#jobMessage");
const downloads = document.querySelector("#downloads");
const reportLink = document.querySelector("#reportLink");
const transcriptLink = document.querySelector("#transcriptLink");
const errorBox = document.querySelector("#errorBox");
const previewBox = document.querySelector("#previewBox");
const previewMeta = document.querySelector("#previewMeta");
const previewTabs = Array.from(document.querySelectorAll("[data-preview-tab]"));

let pollTimer = null;
let activePreviewTab = "transcript";
let latestJob = null;

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderHealth(data);
  } catch (error) {
    ollamaStatus.textContent = "API 離線";
    ollamaStatus.className = "status-pill bad";
    modelReason.textContent = `無法取得健康狀態：${error.message}`;
  }
}

function renderHealth(data) {
  const hardware = data.hardware;
  const gpu = hardware.gpu ? `${hardware.gpu.name} (${hardware.gpu.memory_gb} GB)` : "未偵測到";
  const rec = data.recommendation;
  hardwareFacts.innerHTML = `
    <div><dt>CPU</dt><dd>${hardware.cpu_count} threads</dd></div>
    <div><dt>RAM</dt><dd>${hardware.memory_gb} GB</dd></div>
    <div><dt>GPU</dt><dd>${escapeHtml(gpu)}</dd></div>
    <div><dt>Whisper</dt><dd>${escapeHtml(rec.whisper_model)} / ${escapeHtml(rec.whisper_device)} / ${escapeHtml(rec.whisper_compute_type)}</dd></div>
    <div><dt>Ollama</dt><dd>${escapeHtml(rec.ollama_model)}</dd></div>
  `;
  modelReason.textContent = rec.reason;

  if (data.ollama.available) {
    ollamaStatus.textContent = "Ollama 已連線";
    ollamaStatus.className = "status-pill ok";
  } else {
    ollamaStatus.textContent = "Ollama 未就緒";
    ollamaStatus.className = "status-pill bad";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  downloads.hidden = true;
  latestJob = null;
  renderPreview();
  setProgress(0);
  jobMessage.textContent = "上傳中。";
  submitButton.disabled = true;

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: new FormData(form),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    jobIdLabel.textContent = data.job_id;
    pollJob(data.job_id);
  } catch (error) {
    showError(error.message);
    submitButton.disabled = false;
  }
});

refreshHealthButton.addEventListener("click", loadHealth);

previewTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    activePreviewTab = tab.dataset.previewTab;
    previewTabs.forEach((item) => {
      const isActive = item.dataset.previewTab === activePreviewTab;
      item.classList.toggle("active", isActive);
      item.setAttribute("aria-selected", String(isActive));
    });
    renderPreview();
  });
});

async function pollJob(jobId) {
  clearTimeout(pollTimer);
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderJob(data);

    if (data.status === "done" || data.status === "failed") {
      submitButton.disabled = false;
      return;
    }
    pollTimer = setTimeout(() => pollJob(jobId), 1800);
  } catch (error) {
    showError(error.message);
    submitButton.disabled = false;
  }
}

function renderJob(data) {
  latestJob = data;
  setProgress(data.progress || 0);
  jobMessage.textContent = `${data.message || "處理中"} (${data.stage || "unknown"})`;
  renderPreview();

  if (data.status === "done") {
    reportLink.href = data.report_url;
    transcriptLink.href = data.transcript_url;
    downloads.hidden = false;
  }

  if (data.status === "failed") {
    showError(data.error || "未知錯誤");
  }
}

function renderPreview() {
  const job = latestJob || {};
  const content =
    activePreviewTab === "report" ? job.report_preview || "" : job.transcript_preview || "";
  const emptyText = activePreviewTab === "report" ? "報告尚未產生。" : "逐字稿尚未產生。";

  previewBox.textContent = content || emptyText;
  previewBox.classList.toggle("is-empty", !content);

  if (job.preview_updated_at && content) {
    previewMeta.textContent = `更新時間：${job.preview_updated_at}`;
  } else {
    previewMeta.textContent = "等待處理開始。";
  }
}

function setProgress(value) {
  const normalized = Math.max(0, Math.min(100, Number(value) || 0));
  progressBar.style.width = `${normalized}%`;
  progressText.textContent = `${normalized}%`;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  jobMessage.textContent = "處理失敗。";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadHealth();
