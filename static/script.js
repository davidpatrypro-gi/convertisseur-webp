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
// Ping dès le chargement de la page : si le serveur dormait, il se réveille
// pendant que l'utilisateur choisit ses fichiers, pas au moment de convertir.
let _serverReady = false;
const _pingStart = Date.now();

fetch('/api/ping')
  .then((r) => r.json())
  .then(() => {
    _serverReady = true;
    const ms = Date.now() - _pingStart;
    if (ms > 3000) {
      // Cold start détecté : on affiche un bandeau discret sur la drop zone
      const banner = document.createElement('p');
      banner.id = 'server-banner';
      banner.style.cssText =
        'font-size:.8rem;color:#a16207;background:#fef9c3;border-radius:6px;' +
        'padding:.4rem .75rem;margin-top:.5rem;text-align:center;';
      banner.textContent = '⚠️ Serveur en cours de démarrage, première conversion peut prendre quelques secondes.';
      const dz = document.getElementById('drop-zone');
      if (dz) dz.after(banner);
      setTimeout(() => banner.remove(), 15000);
    }
  })
  .catch(() => { _serverReady = true; }); // on continue même si le ping échoue

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
  // Vérification primaire : type MIME déclaré par le navigateur
  if (['image/jpeg', 'image/jpg', 'image/png'].includes(file.type)) return true;
  // Fallback : vérification par extension du nom de fichier.
  // Nécessaire quand le navigateur retourne file.type="" (ex: certains fichiers
  // dont le nom commence par "@", "#" ou d'autres caractères spéciaux).
  const ext = file.name.split('.').pop().toLowerCase();
  return ['jpg', 'jpeg', 'png'].includes(ext);
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
  const quality  = +qualitySlider.value;
  convertedResults = [];

  resultsContainer.innerHTML = '';
  statsContainer.classList.add('hidden');
  zipWrap.classList.add('hidden');
  convertBtn.disabled = true;
  progressWrap.classList.remove('hidden');
  progressBar.style.width = '0%';

  const total = uploadedFiles.length;
  let done = 0;
  progressText.textContent = `0 / ${total} image${total > 1 ? 's' : ''} convertie…`;

  // Un POST par fichier, tous lancés en parallèle.
  // Chaque résultat s'affiche dès qu'il est prêt → pas d'attente de la dernière image.
  // Message de patience si une image volumineuse prend du temps
  const wakeTimer = setTimeout(() => {
    if (done < total) {
      progressText.textContent = 'Traitement en cours, image volumineuse… ⏳';
    }
  }, 10000);

  const tasks = uploadedFiles.map((file) => {
    const fd = new FormData();
    fd.append('files', file);
    fd.append('quality', quality);

    return fetch('/api/convert', { method: 'POST', body: fd })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(([result]) => {
        if (result.error) {
          // Le serveur a converti la requête mais ce fichier précis a échoué
          // (ex: JPEG corrompu, encodage non standard, mode colorimétrique CMYK…)
          console.warn('Conversion échouée côté serveur', file.name, result.error);
          renderConvertError(file.name, result.error);
          return;
        }
        convertedResults.push(result);
        renderResult(result, file);
      })
      .catch((err) => {
        // Erreur réseau ou HTTP non-ok (4xx/5xx non gérés)
        console.error('Erreur conversion', file.name, err);
        renderConvertError(file.name, err.message);
      })
      .finally(() => {
        done++;
        progressBar.style.width = Math.round((done / total) * 100) + '%';
        progressText.textContent =
          `${done} / ${total} image${total > 1 ? 's' : ''} convertie${done > 1 ? 's' : ''}…`;
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
    // Popup cross-tool (vers le compresseur) après 3,5s
    scheduleCrossPopup();
  }
}

// ── Progress ───────────────────────────────────────────────────────────────────
function setProgress(done, total) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  progressBar.style.width = pct + '%';
  progressText.textContent = `Conversion en cours… ${done} / ${total}`;
}

// ── Render results ─────────────────────────────────────────────────────────────
function renderResult(r, originalFile) {
  const ratio    = ((1 - r.converted_size / r.original_size) * 100).toFixed(1);
  const saving   = r.original_size - r.converted_size;
  const positive = parseFloat(ratio) > 0;

  // Aperçu "avant" : on utilise le fichier local, zéro octet supplémentaire transféré
  const originalSrc = URL.createObjectURL(originalFile);

  // Aperçu "après" : blob WebP depuis le base64 reçu
  const webpBytes = atob(r.webp_b64);
  const webpArr   = new Uint8Array(webpBytes.length);
  for (let i = 0; i < webpBytes.length; i++) webpArr[i] = webpBytes.charCodeAt(i);
  const webpBlob  = new Blob([webpArr], { type: 'image/webp' });
  const webpSrc   = URL.createObjectURL(webpBlob);

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
        <img src="${originalSrc}" alt="Avant" class="result-image" loading="lazy">
        <div class="result-image-size">${fmtSize(r.original_size)}</div>
      </div>
      <div class="result-arrow">→</div>
      <div class="result-image-wrap">
        <div class="result-image-label">Après (WebP)</div>
        <img src="${webpSrc}" alt="Après" class="result-image" loading="lazy">
        <div class="result-image-size result-size-new">${fmtSize(r.converted_size)}</div>
      </div>
    </div>
    <div class="result-footer">
      <span class="result-saving">Gain : ${fmtSize(saving)}</span>
      <button class="btn btn-sm btn-outline" data-name="${escHtml(r.webp_name)}">
        ⬇ Télécharger
      </button>
    </div>`;

  // Téléchargement + popup TP après le clic
  card.querySelector('[data-name]').addEventListener('click', () => {
    triggerDownload(webpBlob, r.webp_name);
    scheduleTpPopup(r.original_size - r.converted_size);
  });

  resultsContainer.appendChild(card);
}

function renderConvertError(filename, detail) {
  const card = document.createElement('div');
  card.className = 'result-card';
  card.style.cssText =
    'border-left:4px solid #ef4444;background:#fef2f2;padding:1rem 1.25rem;';
  const detailHtml = detail
    ? `<div style="font-size:.8rem;color:#9ca3af;margin-top:.25rem;font-family:monospace">${escHtml(detail)}</div>`
    : '';
  card.innerHTML = `
    <div style="color:#b91c1c;font-weight:600;margin-bottom:.3rem;">
      ⚠ Échec de conversion : ${escHtml(filename)}
    </div>
    <div style="font-size:.875rem;color:#6b7280;">
      Ce fichier n'a pas pu être converti. Vérifiez qu'il s'agit bien d'une image JPG ou PNG valide.
    </div>
    ${detailHtml}`;
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

// ── Trustpilot popup ──────────────────────────────────────────────────────────
const TP_URL        = 'https://fr.trustpilot.com/review/convertwebp.fr';
const TP_LATER_KEY  = 'tp_later_v3';
const TP_DONE_KEY   = 'tp_done_v3';
const TP_DELAY_MS   = 3000;
const TP_SNOOZE_DAYS = 7;

function shouldShowTpPopup() {
  if (localStorage.getItem(TP_DONE_KEY)) return false;
  const until = localStorage.getItem(TP_LATER_KEY);
  if (until && Date.now() < parseInt(until, 10)) return false;
  return true;
}

function scheduleTpPopup(savedBytes) {
  if (!shouldShowTpPopup()) return;
  setTimeout(() => {
    // TP prend priorité : ferme tout popup existant (cross-tool, etc.)
    const existing = document.querySelector('.tp-overlay');
    if (existing) existing.remove();
    showTpPopup(savedBytes);
  }, TP_DELAY_MS);
}

function showTpPopup(savedBytes) {
  if (!shouldShowTpPopup()) return;

  const saved = fmtSize(savedBytes);

  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'tp-title');
  overlay.innerHTML = `
    <div class="tp-popup" style="position:relative;">
      <button id="tp-btn-close" aria-label="Fermer"
        style="position:absolute;top:10px;left:12px;background:none;border:none;
               cursor:pointer;font-size:1.2rem;color:#ccc;padding:0;line-height:1;">←</button>
      <div class="tp-popup-stars">★★★★★</div>
      <h2 class="tp-popup-title" id="tp-title">Votre avis compte !</h2>
      <p class="tp-popup-body">
        Vous venez d'économiser <strong>${saved}</strong>&nbsp;!<br>
        Si l'outil vous a plu, 30 secondes sur Trustpilot nous aident énormément.
      </p>
      <div class="tp-popup-cta">
        <a href="${TP_URL}" target="_blank" rel="noopener"
           class="tp-btn-review" id="tp-btn-review">
          Laisser un avis ⭐
        </a>
      </div>
      <div class="tp-popup-logo">Trustpilot</div>
    </div>`;

  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) snoozeTp(overlay); });
  document.getElementById('tp-btn-review').addEventListener('click', () => {
    localStorage.setItem(TP_DONE_KEY, '1');
    closeTp(overlay);
  });
  document.getElementById('tp-btn-close').addEventListener('click', () => snoozeTp(overlay));
}

function snoozeTp(overlay) {
  const until = Date.now() + TP_SNOOZE_DAYS * 24 * 60 * 60 * 1000;
  localStorage.setItem(TP_LATER_KEY, until.toString());
  closeTp(overlay);
}

function closeTp(overlay) {
  overlay.style.opacity = '0';
  overlay.style.transition = 'opacity .2s ease';
  setTimeout(() => overlay.remove(), 200);
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
    // Popup TP après téléchargement ZIP
    const totalGain = convertedResults.reduce((s, r) => s + (r.original_size - r.converted_size), 0);
    scheduleTpPopup(totalGain);
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

// ── Popup cross-tool (vers le compresseur) ────────────────────────────────────
const CROSS_KEY      = 'cross_compress_v3';
const CROSS_DELAY_MS = 3500;

function scheduleCrossPopup() {
  if (localStorage.getItem(CROSS_KEY)) return;
  setTimeout(showCrossPopup, CROSS_DELAY_MS);
}

// retries : si le popup TP est ouvert au moment où ce timer se déclenche,
// on réessaie toutes les 4s (jusqu'à 5 fois) pour ne pas le rater.
function showCrossPopup(retries = 5) {
  if (localStorage.getItem(CROSS_KEY)) return;
  if (document.querySelector('.tp-overlay')) {
    // Un popup TP est ouvert — on attend qu'il se ferme, puis on réessaie
    if (retries > 0) setTimeout(() => showCrossPopup(retries - 1), 4000);
    return;
  }
  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.innerHTML = `
    <div class="tp-popup">
      <div class="tp-popup-stars">🗜️</div>
      <h2 class="tp-popup-title">Aller encore plus loin ?</h2>
      <p class="tp-popup-body">
        Vos nouvelles images WebP peuvent encore être allégées.<br>
        Notre compresseur réduit le poids <strong>sans changer de format</strong>.
      </p>
      <div class="tp-popup-cta">
        <a href="/compresser-images" class="tp-btn-review">Compresser mes images →</a>
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

// ── Helpers ────────────────────────────────────────────────────────────────────
function fmtSize(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  return (bytes / 1024).toFixed(1) + ' KB';
}
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
