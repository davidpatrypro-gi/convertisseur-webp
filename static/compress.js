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

// ── Pré-chauffage serveur ──────────────────────────────────────────────────────
const _pingStart = Date.now();
fetch('/api/ping')
  .then((r) => r.json())
  .then(() => {
    const ms = Date.now() - _pingStart;
    if (ms > 3000) {
      const banner = document.createElement('p');
      banner.style.cssText =
        'font-size:.8rem;color:#a16207;background:#fef9c3;border-radius:6px;' +
        'padding:.4rem .75rem;margin-top:.5rem;text-align:center;';
      banner.textContent = '⚠️ Serveur en cours de démarrage, première compression peut prendre quelques secondes.';
      if (dropZone) dropZone.after(banner);
      setTimeout(() => banner.remove(), 15000);
    }
  })
  .catch(() => {});

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
    handleFiles(Array.from(e.dataTransfer.files));
  });

  convertBtn.addEventListener('click', compressAll);
  zipBtn.addEventListener('click', downloadZip);
});

// ── File handling ──────────────────────────────────────────────────────────────
function isImage(file) {
  // Double vérification : MIME type OU extension (Windows ne mappe pas toujours WebP)
  if (file.type.startsWith('image/')) return true;
  const ext = file.name.split('.').pop().toLowerCase();
  return ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tiff', 'avif'].includes(ext);
}

function handleFiles(files) {
  const valid = files.filter(isImage);
  if (!valid.length) return;
  uploadedFiles = [...uploadedFiles, ...valid];
  renderFileList();
  convertBtn.disabled = false;
  convertBtn.classList.remove('hidden');
}

function renderFileList() {
  fileListContainer.innerHTML = '';
  uploadedFiles.forEach((file, i) => {
    const url  = URL.createObjectURL(file);
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

// ── Compression ────────────────────────────────────────────────────────────────
async function compressAll() {
  if (!uploadedFiles.length) return;
  const quality = +qualitySlider.value;
  convertedResults = [];

  resultsContainer.innerHTML = '';
  statsContainer.classList.add('hidden');
  zipWrap.classList.add('hidden');
  convertBtn.disabled = true;
  progressWrap.classList.remove('hidden');
  progressBar.style.width = '0%';

  const total = uploadedFiles.length;
  let done = 0;
  progressText.textContent = `0 / ${total} image${total > 1 ? 's' : ''} compressée…`;

  const wakeTimer = setTimeout(() => {
    if (done < total) {
      progressText.textContent = 'Traitement en cours, image volumineuse… ⏳';
    }
  }, 10000);

  const tasks = uploadedFiles.map((file) => {
    const fd = new FormData();
    fd.append('files', file);
    fd.append('quality', quality);

    return fetch('/api/compress', { method: 'POST', body: fd })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(([result]) => {
        convertedResults.push(result);
        renderResult(result, file);
      })
      .catch((err) => console.error('Erreur compression', file.name, err))
      .finally(() => {
        done++;
        progressBar.style.width = Math.round((done / total) * 100) + '%';
        progressText.textContent =
          `${done} / ${total} image${total > 1 ? 's' : ''} compressée${done > 1 ? 's' : ''}…`;
      });
  });

  await Promise.all(tasks);
  clearTimeout(wakeTimer);

  progressBar.style.width = '100%';
  setTimeout(() => progressWrap.classList.add('hidden'), 300);
  convertBtn.disabled = false;

  if (convertedResults.length) {
    renderStats();
    statsContainer.classList.remove('hidden');
    if (convertedResults.length > 1) zipWrap.classList.remove('hidden');
    statsContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    // Popup cross-tool uniquement si au moins un fichier compressé n'est pas déjà WebP
    if (convertedResults.some(r => r.mime !== 'image/webp')) {
      scheduleCrossPopup();
    }
  }
}

// ── Render results ─────────────────────────────────────────────────────────────
function renderResult(r, originalFile) {
  const ratio    = ((1 - r.compressed_size / r.original_size) * 100).toFixed(1);
  const saving   = r.original_size - r.compressed_size;
  const positive = parseFloat(ratio) > 0;

  // Aperçu "avant" : fichier local, pas de transfert réseau
  const originalSrc = URL.createObjectURL(originalFile);

  // Aperçu "après" : blob décodé depuis le base64
  const compBytes = atob(r.compressed_b64);
  const compArr   = new Uint8Array(compBytes.length);
  for (let i = 0; i < compBytes.length; i++) compArr[i] = compBytes.charCodeAt(i);
  const compBlob  = new Blob([compArr], { type: r.mime });
  const compSrc   = URL.createObjectURL(compBlob);

  // Libellé du format de sortie
  const fmtLabel = r.mime === 'image/png' ? 'PNG' : 'JPG';

  const card = document.createElement('div');
  card.className = 'result-card';
  card.innerHTML = `
    <div class="result-header">
      <span class="result-filename">${escHtml(r.compressed_name)}</span>
      <span class="result-badge ${positive ? 'badge-success' : 'badge-neutral'}">
        ${positive ? '−' + ratio + '%' : '+' + Math.abs(ratio) + '%'}
      </span>
    </div>
    <div class="result-images">
      <div class="result-image-wrap">
        <div class="result-image-label">Avant</div>
        <img src="${originalSrc}" alt="Avant" class="result-image" loading="lazy">
        <div class="result-image-size">${fmtSize(r.original_size)}</div>
      </div>
      <div class="result-arrow">→</div>
      <div class="result-image-wrap">
        <div class="result-image-label">Après (${fmtLabel})</div>
        <img src="${compSrc}" alt="Après" class="result-image" loading="lazy">
        <div class="result-image-size result-size-new">${fmtSize(r.compressed_size)}</div>
      </div>
    </div>
    <div class="result-footer">
      <span class="result-saving">Gain : ${fmtSize(saving)}</span>
      <button class="btn btn-sm btn-outline" data-name="${escHtml(r.compressed_name)}">
        ⬇ Télécharger
      </button>
    </div>
    ${r.mime === 'image/jpeg' ? `
    <p style="font-size:.78rem;color:#555;background:#f0eeff;border-radius:0 0 10px 10px;
              padding:.45rem .85rem;margin:0;text-align:center;">
      💡 Réduire encore plus ?
      <a href="/" style="color:#6c63ff;font-weight:600;">Convertir en WebP pour −90% →</a>
    </p>` : ''}`;

  card.querySelector('[data-name]').addEventListener('click', () => {
    triggerDownload(compBlob, r.compressed_name);
    scheduleTpPopup(r.original_size - r.compressed_size);
  });

  resultsContainer.appendChild(card);
}

function renderStats() {
  const tot  = convertedResults.reduce((s, r) => s + r.original_size, 0);
  const conv = convertedResults.reduce((s, r) => s + r.compressed_size, 0);
  const gain = tot - conv;
  const pct  = tot ? ((gain / tot) * 100).toFixed(1) : '0';
  statOriginal.textContent  = fmtSize(tot);
  statConverted.textContent = fmtSize(conv);
  statGain.textContent      = fmtSize(gain);
  statPct.textContent       = pct + '%';
}

// ── Downloads ──────────────────────────────────────────────────────────────────
async function downloadZip() {
  if (!uploadedFiles.length) return;
  zipBtn.disabled  = true;
  zipBtn.textContent = '⏳ Préparation du ZIP…';
  try {
    const fd = new FormData();
    uploadedFiles.forEach((f) => fd.append('files', f));
    fd.append('quality', qualitySlider.value);
    const res  = await fetch('/api/compress-zip', { method: 'POST', body: fd });
    const blob = await res.blob();
    triggerDownload(blob, 'images_compressed.zip');
    const totalGain = convertedResults.reduce((s, r) => s + (r.original_size - r.compressed_size), 0);
    scheduleTpPopup(totalGain);
  } finally {
    zipBtn.disabled    = false;
    zipBtn.textContent = '⬇ Télécharger toutes les images en ZIP';
  }
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = Object.assign(document.createElement('a'), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

// ── Popup cross-tool (vers le convertisseur WebP) ────────────────────────────
const CROSS_KEY      = 'cross_webp_v2';
const CROSS_DELAY_MS = 3500;

function scheduleCrossPopup() {
  if (localStorage.getItem(CROSS_KEY)) return;
  setTimeout(showCrossPopup, CROSS_DELAY_MS);
}

function showCrossPopup() {
  if (localStorage.getItem(CROSS_KEY)) return;
  if (document.querySelector('.tp-overlay')) return; // un popup est déjà ouvert
  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.innerHTML = `
    <div class="tp-popup">
      <div class="tp-popup-stars">⚡</div>
      <h2 class="tp-popup-title">Réduire encore plus ?</h2>
      <p class="tp-popup-body">
        Convertissez vos images au format <strong>WebP</strong> pour réduire leur poids
        jusqu'à <strong>90%</strong> — bien plus qu'une simple compression.
      </p>
      <div class="tp-popup-cta">
        <a href="/" class="tp-btn-review">Convertir en WebP →</a>
        <button class="tp-btn-later" id="cross-btn-later">Non merci</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeCrossPopup(overlay); });
  document.getElementById('cross-btn-later').addEventListener('click', () => {
    localStorage.setItem(CROSS_KEY, '1');
    closeCrossPopup(overlay);
  });
}

function closeCrossPopup(overlay) {
  overlay.style.opacity = '0';
  overlay.style.transition = 'opacity .2s ease';
  setTimeout(() => overlay.remove(), 200);
}

// ── Popup Trustpilot ─────────────────────────────────────────────────────────
const TP_URL         = 'https://fr.trustpilot.com/review/convertwebp.fr';
const TP_LATER_KEY   = 'tp_later_v2';
const TP_DONE_KEY    = 'tp_done_v2';
const TP_DELAY_MS    = 1500;
const TP_SNOOZE_DAYS = 7;

function shouldShowTpPopup() {
  if (localStorage.getItem(TP_DONE_KEY)) return false;
  const until = localStorage.getItem(TP_LATER_KEY);
  if (until && Date.now() < parseInt(until, 10)) return false;
  return true;
}

function scheduleTpPopup(savedBytes) {
  if (!shouldShowTpPopup()) return;
  setTimeout(() => showTpPopup(savedBytes), TP_DELAY_MS);
}

function showTpPopup(savedBytes) {
  if (!shouldShowTpPopup()) return;
  if (document.querySelector('.tp-overlay')) return; // un popup est déjà ouvert
  const saved = fmtSize(savedBytes);
  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'tp-title');
  overlay.innerHTML = `
    <div class="tp-popup">
      <div class="tp-popup-stars">★★★★★</div>
      <h2 class="tp-popup-title" id="tp-title">Votre avis compte !</h2>
      <p class="tp-popup-body">
        Vous venez d'économiser <strong>${saved}</strong>&nbsp;!<br>
        Si l'outil vous a plu, 30 secondes sur Trustpilot nous aident énormément.
      </p>
      <div class="tp-popup-cta">
        <a href="${TP_URL}" target="_blank" rel="noopener" class="tp-btn-review" id="tp-btn-review">
          Laisser un avis ⭐
        </a>
        <button class="tp-btn-later" id="tp-btn-later">Plus tard</button>
      </div>
      <div class="tp-popup-logo">Trustpilot</div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) snoozeTp(overlay); });
  document.getElementById('tp-btn-review').addEventListener('click', () => {
    localStorage.setItem(TP_DONE_KEY, '1');
    closeTp(overlay);
  });
  document.getElementById('tp-btn-later').addEventListener('click', () => snoozeTp(overlay));
}

function snoozeTp(overlay) {
  localStorage.setItem(TP_LATER_KEY, (Date.now() + TP_SNOOZE_DAYS * 86400000).toString());
  closeTp(overlay);
}

function closeTp(overlay) {
  overlay.style.opacity = '0';
  overlay.style.transition = 'opacity .2s ease';
  setTimeout(() => overlay.remove(), 200);
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function fmtSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  return (bytes / 1024).toFixed(1) + ' KB';
}
function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
