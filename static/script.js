'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
let uploadedFiles    = [];
let convertedResults = [];

// ── DOM refs ───────────────────────────────────────────────────────────────────
const dropZone          = document.getElementById('drop-zone');
const fileInput         = document.getElementById('file-input');
const qualitySlider     = document.getElementById('quality-slider');
const qualityValue      = document.getElementById('quality-value');
const convertBtn        = document.getElementById('convert-btn');
const progressWrap      = document.getElementById('progress-wrap');
const progressBar       = document.getElementById('progress-bar');
const progressText      = document.getElementById('progress-text');
const resultsContainer  = document.getElementById('results-container');
const statsContainer    = document.getElementById('stats-container');
const statOriginal      = document.getElementById('stat-original');
const statConverted     = document.getElementById('stat-converted');
const statGain          = document.getElementById('stat-gain');
const statPct           = document.getElementById('stat-pct');
const zipWrap           = document.getElementById('zip-wrap');
const zipBtn            = document.getElementById('zip-btn');
const fileListContainer = document.getElementById('file-list');

// ── Init ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  qualitySlider.addEventListener('input', () => {
    qualityValue.textContent = qualitySlider.value;
  });

  fileInput.addEventListener('change', (e) =>
    handleFiles(Array.from(e.target.files))
  );

  dropZone.addEventListener('click', () => fileInput.click());

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', () =>
    dropZone.classList.remove('drag-over')
  );
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    handleFiles(Array.from(e.dataTransfer.files).filter(isValidImage));
  });

  convertBtn.addEventListener('click', convertAll);
  zipBtn.addEventListener('click', downloadZip);
});

// ── File handling ──────────────────────────────────────────────────────────────
function isValidImage(file) {
  return ['image/jpeg', 'image/jpg', 'image/png'].includes(file.type);
}

function handleFiles(files) {
  const valid = files.filter(isValidImage);
  if (!valid.length) return;
  uploadedFiles = [...uploadedFiles, ...valid];
  renderFileList();
  convertBtn.disabled = false;
  convertBtn.classList.remove('hidden');
}

function renderFileList() {
  fileListContainer.innerHTML = '';
  uploadedFiles.forEach((file, i) => {
    const url = URL.createObjectURL(file);
    const item = document.createElement('div');
    item.className = 'file-item';
    item.innerHTML = `
      <img src="${url}" alt="${escHtml(file.name)}" class="file-thumb">
      <div class="file-info">
        <span class="file-name">${escHtml(file.name)}</span>
        <span class="file-size">${fmtSize(file.size)}</span>
      </div>
      <button class="file-remove" data-i="${i}" aria-label="Supprimer">×</button>`;
    fileListContainer.appendChild(item);
  });

  fileListContainer.querySelectorAll('.file-remove').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      uploadedFiles.splice(+e.currentTarget.dataset.i, 1);
      renderFileList();
      if (!uploadedFiles.length) convertBtn.classList.add('hidden');
    });
  });

  fileListContainer.classList.toggle('hidden', !uploadedFiles.length);
}

// ── Conversion ─────────────────────────────────────────────────────────────────
async function convertAll() {
  if (!uploadedFiles.length) return;
  const quality = +qualitySlider.value;
  convertedResults = [];

  resultsContainer.innerHTML = '';
  statsContainer.classList.add('hidden');
  zipWrap.classList.add('hidden');
  convertBtn.disabled = true;
  progressWrap.classList.remove('hidden');
  setProgress(0, uploadedFiles.length);

  for (let i = 0; i < uploadedFiles.length; i++) {
    try {
      const result = await convertSingle(uploadedFiles[i], quality);
      convertedResults.push(result);
      renderResult(result);
      setProgress(i + 1, uploadedFiles.length);
    } catch (err) {
      console.error('Erreur pour', uploadedFiles[i].name, err);
    }
  }

  progressWrap.classList.add('hidden');
  convertBtn.disabled = false;

  if (convertedResults.length) {
    renderStats();
    statsContainer.classList.remove('hidden');
    zipWrap.classList.remove('hidden');
    statsContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

async function convertSingle(file, quality) {
  const fd = new FormData();
  fd.append('files', file);
  fd.append('quality', quality);
  const res = await fetch('/api/convert', { method: 'POST', body: fd });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json())[0];
}

// ── Progress ───────────────────────────────────────────────────────────────────
function setProgress(done, total) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  progressBar.style.width = pct + '%';
  progressText.textContent = `Conversion en cours… ${done} / ${total}`;
}

// ── Render results ─────────────────────────────────────────────────────────────
function renderResult(r) {
  const ratio   = ((1 - r.converted_size / r.original_size) * 100).toFixed(1);
  const saving  = r.original_size - r.converted_size;
  const positive = parseFloat(ratio) > 0;

  const card = document.createElement('div');
  card.className = 'result-card';
  card.innerHTML = `
    <div class="result-header">
      <span class="result-filename">${escHtml(r.webp_name)}</span>
      <span class="result-badge ${positive ? 'badge-success' : 'badge-neutral'}">
        ${positive ? '−' + ratio + '%' : '+' + Math.abs(ratio) + '%'}
      </span>
    </div>
    <div class="result-images">
      <div class="result-image-wrap">
        <div class="result-image-label">Avant</div>
        <img src="data:${r.original_mime};base64,${r.original_b64}"
             alt="Avant" class="result-image" loading="lazy">
        <div class="result-image-size">${fmtSize(r.original_size)}</div>
      </div>
      <div class="result-arrow">→</div>
      <div class="result-image-wrap">
        <div class="result-image-label">Après (WebP)</div>
        <img src="data:image/webp;base64,${r.webp_b64}"
             alt="Après" class="result-image" loading="lazy">
        <div class="result-image-size result-size-new">${fmtSize(r.converted_size)}</div>
      </div>
    </div>
    <div class="result-footer">
      <span class="result-saving">Gain : ${fmtSize(saving)}</span>
      <button class="btn btn-sm btn-outline"
              data-b64="${r.webp_b64}" data-name="${escHtml(r.webp_name)}">
        ⬇ Télécharger
      </button>
    </div>`;

  card.querySelector('[data-b64]').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    downloadWebp(btn.dataset.b64, btn.dataset.name);
  });

  resultsContainer.appendChild(card);
}

function renderStats() {
  const tot = convertedResults.reduce((s, r) => s + r.original_size, 0);
  const conv = convertedResults.reduce((s, r) => s + r.converted_size, 0);
  const gain = tot - conv;
  const pct  = tot ? ((gain / tot) * 100).toFixed(1) : '0';
  statOriginal.textContent  = fmtSize(tot);
  statConverted.textContent = fmtSize(conv);
  statGain.textContent      = fmtSize(gain);
  statPct.textContent       = pct + '%';
}

// ── Downloads ──────────────────────────────────────────────────────────────────
function downloadWebp(b64, filename) {
  const bytes = atob(b64);
  const arr   = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  triggerDownload(new Blob([arr], { type: 'image/webp' }), filename);
}

async function downloadZip() {
  if (!uploadedFiles.length) return;
  zipBtn.disabled = true;
  zipBtn.textContent = '⏳ Préparation du ZIP…';
  try {
    const fd = new FormData();
    uploadedFiles.forEach((f) => fd.append('files', f));
    fd.append('quality', qualitySlider.value);
    const res  = await fetch('/api/convert-zip', { method: 'POST', body: fd });
    const blob = await res.blob();
    triggerDownload(blob, 'images_webp.zip');
  } finally {
    zipBtn.disabled = false;
    zipBtn.textContent = '⬇ Télécharger toutes les images en ZIP';
  }
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = Object.assign(document.createElement('a'), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function fmtSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  return (bytes / 1024).toFixed(1) + ' KB';
}
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
