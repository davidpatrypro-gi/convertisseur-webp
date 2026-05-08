import asyncio
import base64
import gc
import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import AsyncGenerator, List

import resend
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydantic import BaseModel

# ── Stats SQLite ───────────────────────────────────────────────────────────────
_DB   = Path("stats.db")
STATS_TOKEN = "cwp-stats-2026"   # Changez ce token si vous le partagez

def _db_conn():
    conn = sqlite3.connect(_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS conversions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT    DEFAULT (datetime('now')),
                files_count    INTEGER NOT NULL,
                original_bytes INTEGER NOT NULL,
                converted_bytes INTEGER NOT NULL,
                quality        INTEGER NOT NULL
            )
        """)

_init_db()

def _record(files_count: int, original_bytes: int, converted_bytes: int, quality: int):
    with _db_conn() as c:
        c.execute(
            "INSERT INTO conversions (files_count, original_bytes, converted_bytes, quality)"
            " VALUES (?,?,?,?)",
            (files_count, original_bytes, converted_bytes, quality),
        )

def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3: return f"{b / 1024 ** 3:.1f} Go"
    if b >= 1024 ** 2: return f"{b / 1024 ** 2:.1f} Mo"
    if b >= 1024:      return f"{b / 1024:.1f} Ko"
    return f"{b} o"


# ── Resend ─────────────────────────────────────────────────────────────────────
resend.api_key = "re_JFetnr12_M4ftDxDgBGGpAcbauBzTFLPy"
CONTACT_EMAIL  = "david.patrypro@gmail.com"
FROM_EMAIL     = "ConvertWebP <contact@convertwebp.fr>"


class ContactForm(BaseModel):
    name:    str
    email:   str
    url:     str
    message: str = ""

app = FastAPI(title="ConvertWebP — Convertisseur images en ligne")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(GZipMiddleware, minimum_size=1000)
templates = Jinja2Templates(directory="templates")

# Chargement de la config avis Trustpilot — injecté globalement dans tous les templates
with open("reviews_config.json") as _f:
    _reviews_cfg = json.load(_f)
templates.env.globals["reviews"] = _reviews_cfg["trustpilot"]


@app.exception_handler(404)
async def custom_404(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "404.html", status_code=404)


# ── Middlewares ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def add_performance_headers(request: Request, call_next):
    """Cache-Control long sur les assets statiques + en-têtes de sécurité."""
    response = await call_next(request)
    path = request.url.path
    ct = response.headers.get("content-type", "")

    if path.startswith("/static/"):
        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
        if ext in ('js', 'css'):
            # JS/CSS : 1h — on les met à jour souvent, immutable interdirait les MAJ
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            # Images, fonts, ico : cache 1 an immuable (ne changent jamais)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif "text/html" in ct:
        # Pages HTML : jamais en cache (contenu dynamique)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"

    # En-têtes de sécurité sur toutes les réponses
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    return response


@app.middleware("http")
async def normalize_url(request: Request, call_next):
    """301 : URLs majuscules → minuscules, trailing slash (hors racine)."""
    path = request.url.path
    new_path = None
    if path != "/" and path.endswith("/"):
        new_path = path.rstrip("/")
    elif path != path.lower():
        new_path = path.lower()
    if new_path:
        url = str(request.url).replace(path, new_path, 1)
        return RedirectResponse(url=url, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def redirect_www(request: Request, call_next):
    """Redirige www.convertwebp.fr → convertwebp.fr (301)."""
    host = request.headers.get("host", "")
    if host.startswith("www."):
        url = str(request.url).replace("://www.", "://", 1)
        return RedirectResponse(url=url, status_code=301)
    return await call_next(request)

# Thread pool dédié aux conversions CPU-bound.
# max_workers=2 : sur Render Free (512 Mo), limiter les threads réduit
# la mémoire de pile (~8 Mo/thread sur Linux) et la pression mémoire globale.
_executor = ThreadPoolExecutor(max_workers=2)

# Largeur max des aperçus base64 renvoyés au client (réduit la RAM et la taille JSON)
_PREVIEW_MAX_W  = 800
# Taille max par fichier uploadé — rejeté avant décodage Pillow (évite les OOM)
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 Mo


def _log_mem(label: str) -> None:
    """Log la consommation RAM du process si psutil est installé."""
    if not _HAS_PSUTIL:
        return
    rss_mb = _psutil.Process().memory_info().rss / 1_048_576
    print(f"[MEM] {label} : {rss_mb:.1f} MB RSS", flush=True)

# Favicon mis en cache en mémoire au démarrage
_FAVICON: bytes = b""
try:
    _FAVICON = Path("static/favicon.ico").read_bytes()
except FileNotFoundError:
    pass


# ── Filename helpers ───────────────────────────────────────────────────────────

def _safe_stem(filename: str | None) -> str:
    """Extrait et assainit le nom de base (sans extension) d'un fichier upload.

    Gère tous les cas problématiques :
      - filename None (python-multipart ne l'a pas trouvé)
      - URL-encoding (%40 → @, %20 → espace, etc.)
      - Caractères spéciaux : @, #, &, +, !, (, ), espaces
      - Accents et caractères Unicode (é→e, ñ→n, …)
      - Noms vides après nettoyage
    """
    if not filename:
        return "image"
    # Décode les % éventuels (%40 → @, %20 → espace, etc.)
    filename = unquote(filename)
    # Isole le nom de base (sans extension)
    parts = filename.rsplit('.', 1)
    stem = parts[0] if len(parts) > 1 else filename
    if not stem:
        return "image"
    # Normalise les accents (é→e, ñ→n, …)
    stem = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode('ascii')
    # Remplace tout caractère non alphanumérique (sauf tiret et underscore) par _
    stem = re.sub(r'[^\w\-]', '_', stem)
    # Écrase les séquences de séparateurs et supprime les extrémités
    stem = re.sub(r'[_\-]{2,}', '_', stem).strip('_-')
    return stem or "image"


def _file_ext(filename: str | None) -> str:
    """Retourne l'extension en minuscules, 'jpg' par défaut si absente ou None."""
    if not filename or '.' not in filename:
        return 'jpg'
    return unquote(filename).rsplit('.', 1)[-1].lower()


# ── Conversion helpers ─────────────────────────────────────────────────────────

def _normalize_mode(img: Image.Image) -> Image.Image:
    """Normalise le mode colorimétrique d'une Image Pillow vers RGB ou L.

    - Transparence (RGBA/LA/P+alpha) → fond blanc RGB (nécessaire pour WebP lossy)
    - CMYK → RGB (photos pro au format CMYK)
    - Tout autre mode exotique → RGB

    Ferme l'image source si elle est remplacée par un nouvel objet Pillow.
    Centralise la logique pour _to_webp et _to_webp_with_thumb afin d'éviter
    la duplication et les divergences de comportement entre les deux fonctions.
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg  = Image.new("RGB", img.size, (255, 255, 255))
        src = img.convert("RGBA")
        bg.paste(src, mask=src.split()[3])
        src.close(); img.close()
        return bg
    if img.mode == "CMYK":
        tmp = img.convert("RGB"); img.close(); return tmp
    if img.mode not in ("RGB", "L"):
        tmp = img.convert("RGB"); img.close(); return tmp
    return img  # Déjà dans un mode compatible WebP


def _to_webp(content: bytes, quality: int) -> bytes:
    """Conversion synchrone vers WebP — exécutée dans le thread pool.
    Tous les objets Pillow et BytesIO sont fermés explicitement en sortie."""
    in_buf  = io.BytesIO(content)
    out_buf = io.BytesIO()
    img     = None
    try:
        img = Image.open(in_buf)
        img.load()  # Force le décodage complet — attrape fichiers tronqués / CMYK / etc.

        # Resize uniquement si le fichier dépasse 5 Mo ET que la résolution dépasse 4000px
        MAX_BYTES = 5 * 1024 * 1024
        MAX_DIM   = 4000
        if len(content) > MAX_BYTES and (img.width > MAX_DIM or img.height > MAX_DIM):
            img.thumbnail((MAX_DIM, MAX_DIM), Image.BILINEAR)

        img = _normalize_mode(img)
        img.save(out_buf, format="WebP", quality=quality, method=0)
        return out_buf.getvalue()
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
        in_buf.close()
        out_buf.close()


async def _to_webp_async(content: bytes, quality: int) -> bytes:
    """Lance _to_webp dans le thread pool sans bloquer la boucle asyncio."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _to_webp, content, quality)


def _to_webp_with_thumb(content: bytes, quality: int) -> tuple[int, str]:
    """Conversion WebP + aperçu 800px en un seul décodage Pillow.

    Retourne (conv_size, preview_b64) :
      - conv_size   : taille en octets du WebP pleine résolution (pour les stats)
      - preview_b64 : aperçu ≤ 800px encodé en base64 (affiché ET téléchargé)

    Avantage RAM critique vs l'ancienne approche en deux passes (_to_webp puis
    _make_preview_b64) : l'image est décodée UNE seule fois, puis la miniature est
    générée depuis l'objet Pillow déjà en mémoire → pic mémoire divisé par deux.
    """
    in_buf    = io.BytesIO(content)
    full_buf  = io.BytesIO()
    thumb_buf = io.BytesIO()
    img = thumb = None
    try:
        img = Image.open(in_buf)
        img.load()

        MAX_BYTES = 5 * 1024 * 1024
        MAX_DIM   = 4000
        if len(content) > MAX_BYTES and (img.width > MAX_DIM or img.height > MAX_DIM):
            img.thumbnail((MAX_DIM, MAX_DIM), Image.BILINEAR)

        img = _normalize_mode(img)

        # ① WebP pleine résolution → conv_size (bytes non conservés côté serveur)
        img.save(full_buf, format="WebP", quality=quality, method=0)
        conv_size = full_buf.tell()
        full_buf.close(); full_buf = None   # libère immédiatement

        # ② Aperçu ≤ 800px depuis le même objet Pillow déjà en mémoire
        if img.width > _PREVIEW_MAX_W:
            ratio = _PREVIEW_MAX_W / img.width
            thumb = img.resize((_PREVIEW_MAX_W, int(img.height * ratio)), Image.BILINEAR)
            thumb.save(thumb_buf, format="WebP", quality=72, method=0)
            thumb.close(); thumb = None
        else:
            img.save(thumb_buf, format="WebP", quality=72, method=0)

        return conv_size, base64.b64encode(thumb_buf.getvalue()).decode()
    finally:
        for obj in (thumb, img):
            if obj is not None:
                try: obj.close()
                except Exception: pass
        in_buf.close()
        if full_buf is not None:
            try: full_buf.close()
            except Exception: pass
        thumb_buf.close()


def _compress(content: bytes, filename: str | None, quality: int):
    """Compression synchrone dans le format d'origine (JPG→JPG, PNG→PNG).
    Retourne (compressed_bytes, out_extension, mime_type).
    Tous les objets Pillow et BytesIO sont fermés explicitement en sortie."""
    ext     = _file_ext(filename)
    in_buf  = io.BytesIO(content)
    out_buf = io.BytesIO()
    img     = None
    try:
        img = Image.open(in_buf)
        img.load()  # Force le décodage complet — même raison que dans _to_webp

        # Resize si > 5 Mo et dimension > 4000px (même règle que la conversion WebP)
        MAX_BYTES = 5 * 1024 * 1024
        MAX_DIM   = 4000
        if len(content) > MAX_BYTES and (img.width > MAX_DIM or img.height > MAX_DIM):
            img.thumbnail((MAX_DIM, MAX_DIM), Image.BILINEAR)

        if ext in ('jpg', 'jpeg'):
            if img.mode not in ('RGB', 'L'):
                tmp = img.convert('RGB')
                img.close()
                img = tmp
            # optimize=False : on-the-fly single-pass, bien plus rapide
            img.save(out_buf, format='JPEG', quality=quality, optimize=False, progressive=False)
            return out_buf.getvalue(), 'jpg', 'image/jpeg'
        elif ext == 'webp':
            if img.mode not in ('RGB', 'RGBA'):
                mode = 'RGBA' if img.mode in ('LA', 'PA', 'P') else 'RGB'
                tmp = img.convert(mode)
                img.close()
                img = tmp
            img.save(out_buf, format='WebP', quality=quality, method=0)
            return out_buf.getvalue(), 'webp', 'image/webp'
        elif ext == 'png':  # PNG — format sans perte, transparence conservée
            if img.mode == 'P':
                mode = 'RGBA' if 'transparency' in img.info else 'RGB'
                tmp = img.convert(mode)
                img.close()
                img = tmp
            # compress_level=6 : bon équilibre vitesse/taille
            img.save(out_buf, format='PNG', optimize=False, compress_level=6)
            return out_buf.getvalue(), 'png', 'image/png'
        else:  # Fallback : on compresse en JPEG
            if img.mode not in ('RGB', 'L'):
                tmp = img.convert('RGB')
                img.close()
                img = tmp
            img.save(out_buf, format='JPEG', quality=quality, optimize=False, progressive=False)
            return out_buf.getvalue(), 'jpg', 'image/jpeg'
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass
        in_buf.close()
        out_buf.close()


async def _compress_async(content: bytes, filename: str, quality: int):
    """Lance _compress dans le thread pool sans bloquer la boucle asyncio."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _compress, content, filename, quality)


async def _stream_buf(buf: io.BytesIO) -> AsyncGenerator[bytes, None]:
    """Itère un BytesIO en chunks de 64 Ko pour StreamingResponse."""
    buf.seek(0)
    while chunk := buf.read(65_536):
        yield chunk


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.head("/")
async def head_root():
    return Response(status_code=200)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Sert le favicon à la racine — mis en cache en mémoire au démarrage."""
    if not _FAVICON:
        raise HTTPException(status_code=404)
    return Response(
        content=_FAVICON,
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/mentions-legales", response_class=HTMLResponse)
async def mentions_legales(request: Request):
    return templates.TemplateResponse(request, "mentions-legales.html")


@app.get("/politique-confidentialite", response_class=HTMLResponse)
async def politique_confidentialite(request: Request):
    return templates.TemplateResponse(request, "politique-confidentialite.html")


@app.get("/blog", response_class=HTMLResponse)
async def blog(request: Request):
    return templates.TemplateResponse(request, "blog/index.html")


@app.get("/blog/pourquoi-convertir-images-webp-seo", response_class=HTMLResponse)
async def blog_webp_seo(request: Request):
    return templates.TemplateResponse(request, "blog/pourquoi-convertir-images-webp-seo.html")


@app.get("/blog/webp-vs-jpg-png-comparaison", response_class=HTMLResponse)
async def blog_webp_vs(request: Request):
    return templates.TemplateResponse(request, "blog/webp-vs-jpg-png-comparaison.html")


@app.get("/blog/optimiser-images-vitesse-site-google", response_class=HTMLResponse)
async def blog_optimiser(request: Request):
    return templates.TemplateResponse(request, "blog/optimiser-images-vitesse-site-google.html")


@app.get("/blog/compresser-images-en-ligne", response_class=HTMLResponse)
async def blog_compresser_images(request: Request):
    return templates.TemplateResponse(request, "blog/compresser-images-en-ligne.html")


@app.get("/sitemap.xml")
async def sitemap():
    content = open("static/sitemap.xml", "r", encoding="utf-8").read()
    return Response(content=content, media_type="application/xml")


@app.get("/robots.txt")
async def robots():
    content = open("static/robots.txt", "r", encoding="utf-8").read()
    return Response(content=content, media_type="text/plain")


_PARIS = timezone(timedelta(hours=2))  # CEST avril 2026 = UTC+2
_PUBLISH_SEO_LOCAL = datetime(2026, 4, 27, 9, 0, 0, tzinfo=_PARIS)

@app.get("/blog/seo-local-2026-guide-complet", response_class=HTMLResponse)
async def blog_seo_local(request: Request):
    # Publication planifiée : visible le 27 avril 2026 à 09h00 (heure de Paris)
    if datetime.now(tz=timezone.utc) < _PUBLISH_SEO_LOCAL:
        return Response(status_code=404)
    return templates.TemplateResponse(request, "blog/seo-local-2026-guide-complet.html")


# ── API ────────────────────────────────────────────────────────────────────────

@app.post("/api/contact")
async def api_contact(form: ContactForm):
    """Envoie deux emails via Resend :
    - notification interne à contact@convertwebp.fr
    - confirmation automatique à l'expéditeur
    """
    message_block = (
        f"<p><strong>Objectifs SEO :</strong><br>{form.message.replace(chr(10), '<br>')}</p>"
        if form.message.strip() else ""
    )

    # ── Email de notification (pour nous) ──────────────────────────────────────
    notif_html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;">
      <div style="background:#6c63ff;padding:16px 24px;border-radius:8px 8px 0 0;">
        <h1 style="color:white;margin:0;font-size:1.25rem;">
          🎯 Nouvelle demande d'audit SEO
        </h1>
      </div>
      <div style="background:#f9f9ff;border:1px solid #e0dbff;border-top:none;
                  border-radius:0 0 8px 8px;padding:24px;">
        <table style="width:100%;border-collapse:collapse;font-size:.95rem;">
          <tr>
            <td style="padding:8px 0;color:#666;width:130px;">Nom</td>
            <td style="padding:8px 0;font-weight:600;color:#1a1a2e;">{form.name}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#666;">Email</td>
            <td style="padding:8px 0;">
              <a href="mailto:{form.email}" style="color:#6c63ff;">{form.email}</a>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#666;">Site à auditer</td>
            <td style="padding:8px 0;">
              <a href="{form.url}" target="_blank" style="color:#6c63ff;">{form.url}</a>
            </td>
          </tr>
        </table>
        {message_block}
        <div style="margin-top:20px;">
          <a href="mailto:{form.email}?subject=Votre audit SEO ConvertWebP"
             style="background:#6c63ff;color:white;padding:10px 20px;
                    border-radius:6px;text-decoration:none;font-weight:600;">
            Répondre à {form.name} →
          </a>
        </div>
      </div>
    </div>"""

    # ── Email de confirmation (pour l'utilisateur) ─────────────────────────────
    confirm_html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;">
      <div style="background:#6c63ff;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h1 style="color:white;margin:0;font-size:1.3rem;">ConvertWebP</h1>
      </div>
      <div style="background:#ffffff;border:1px solid #e0dbff;border-top:none;
                  border-radius:0 0 8px 8px;padding:28px;">
        <h2 style="color:#1a1a2e;margin-top:0;">
          Votre demande d'audit a bien été reçue ✅
        </h2>
        <p style="color:#444;">Bonjour {form.name},</p>
        <p style="color:#444;">
          Nous avons bien reçu votre demande d'audit SEO pour
          <strong>{form.url}</strong>.
        </p>
        <div style="background:#f0eeff;border-left:4px solid #6c63ff;
                    border-radius:4px;padding:14px 18px;margin:20px 0;">
          <p style="margin:0;color:#333;font-size:.95rem;">
            Notre équipe analyse votre site et vous envoie un rapport personnalisé
            <strong>dans les 48 heures</strong> à cette adresse email.
          </p>
        </div>
        <p style="color:#444;">
          En attendant, si vous ne l'avez pas encore fait, vous pouvez
          commencer à optimiser vos images avec notre outil gratuit :
        </p>
        <p style="text-align:center;margin:24px 0;">
          <a href="https://convertwebp.fr"
             style="background:#6c63ff;color:white;padding:12px 24px;
                    border-radius:6px;text-decoration:none;font-weight:700;">
            ⚡ Convertir mes images en WebP
          </a>
        </p>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
        <p style="color:#999;font-size:.85rem;margin:0;">
          ConvertWebP · <a href="https://convertwebp.fr" style="color:#6c63ff;">convertwebp.fr</a>
          · Vous recevez cet email car vous avez rempli notre formulaire d'audit SEO.
        </p>
      </div>
    </div>"""

    loop = asyncio.get_running_loop()
    try:
        r1 = await loop.run_in_executor(_executor, lambda: resend.Emails.send({
            "from":    FROM_EMAIL,
            "to":      [CONTACT_EMAIL],
            "subject": f"Nouvelle demande d'audit SEO — {form.url}",
            "html":    notif_html,
        }))
        print(f"[Resend] notif envoyée : {r1}")

        r2 = await loop.run_in_executor(_executor, lambda: resend.Emails.send({
            "from":    FROM_EMAIL,
            "to":      [form.email],
            "subject": "Votre demande d'audit SEO a bien été reçue",
            "html":    confirm_html,
        }))
        print(f"[Resend] confirmation envoyée : {r2}")

    except Exception as exc:
        print(f"[Resend] ERREUR : {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


@app.get("/api/ping")
async def ping():
    """Endpoint de diagnostic — répond instantanément, permet de détecter le cold start."""
    return JSONResponse({"ok": True, "ts": datetime.now().isoformat()})


@app.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html")


@app.get("/compresser-images", response_class=HTMLResponse)
async def compresser_images(request: Request):
    return templates.TemplateResponse(request, "compresser-images.html")


@app.post("/api/convert")
async def api_convert(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    if len(files) > 30:
        raise HTTPException(
            status_code=400,
            detail="Maximum 30 images par conversion. Divisez votre lot en plusieurs envois.",
        )

    results    = []
    total_orig = 0
    total_conv = 0
    loop       = asyncio.get_running_loop()
    _log_mem("convert début")

    # Traitement séquentiel image par image : garantit qu'une seule image Pillow
    # est en mémoire à la fois — évite les OOM sur le plan Free de Render (512 Mo).
    for file in files:
        content = await file.read()
        stem    = _safe_stem(file.filename)
        # Rejet immédiat si le fichier dépasse la limite : évite un décodage Pillow OOM
        if len(content) > _MAX_FILE_BYTES:
            mb = round(len(content) / 1024 / 1024, 1)
            results.append({
                "original_name":  file.filename or "image",
                "webp_name":      f"{stem}.webp",
                "original_size":  len(content),
                "converted_size": 0,
                "webp_b64":       None,
                "error":          f"Fichier trop volumineux ({mb} Mo — max {_MAX_FILE_BYTES//1024//1024} Mo).",
            })
            del content; gc.collect(); continue
        try:
            # Un seul appel thread pool = un seul décodage Pillow (conversion + aperçu)
            conv_size, preview_b64 = await loop.run_in_executor(
                _executor, _to_webp_with_thumb, content, quality
            )
            orig_size   = len(content)
            total_orig += orig_size
            total_conv += conv_size
            results.append({
                "original_name":  file.filename or "image",
                "webp_name":      f"{stem}.webp",
                "original_size":  orig_size,
                "converted_size": conv_size,
                "webp_b64":       preview_b64,
            })
        except Exception as exc:
            print(f"[WARN] Conversion échouée {file.filename!r}: {exc}", flush=True)
            results.append({
                "original_name":  file.filename or "image",
                "webp_name":      f"{stem}.webp",
                "original_size":  len(content),
                "converted_size": 0,
                "webp_b64":       None,
                "error":          f"{type(exc).__name__}: {exc}",
            })
        finally:
            del content
            gc.collect()
        _log_mem(f"convert après {file.filename!r}")

    await loop.run_in_executor(_executor, _record, len(files), total_orig, total_conv, quality)
    _log_mem("convert fin")
    return results


@app.post("/api/convert-zip")
async def api_convert_zip(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    if len(files) > 30:
        raise HTTPException(
            status_code=400,
            detail="Maximum 30 images par conversion. Divisez votre lot en plusieurs envois.",
        )

    total_orig = 0
    total_conv = 0
    zip_buf    = io.BytesIO()
    loop       = asyncio.get_running_loop()
    _log_mem("convert-zip début")

    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for file in files:
            content = await file.read()
            if len(content) > _MAX_FILE_BYTES:
                print(f"[WARN] ZIP convert skip {file.filename!r}: trop volumineux ({len(content)} o)", flush=True)
                del content; gc.collect(); continue
            try:
                webp_result = await _to_webp_async(content, quality)
                total_orig += len(content)
                total_conv += len(webp_result)
                zf.writestr(f"{_safe_stem(file.filename)}.webp", webp_result)
                del webp_result
            except Exception as exc:
                print(f"[WARN] ZIP convert skip {file.filename!r}: {exc}", flush=True)
            finally:
                del content
                gc.collect()
            _log_mem(f"convert-zip après {file.filename!r}")

    await loop.run_in_executor(_executor, _record, len(files), total_orig, total_conv, quality)
    _log_mem("convert-zip fin")
    return StreamingResponse(
        _stream_buf(zip_buf),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="images_webp.zip"'},
    )


@app.post("/api/compress")
async def api_compress(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    if len(files) > 30:
        raise HTTPException(
            status_code=400,
            detail="Maximum 30 images par compression. Divisez votre lot en plusieurs envois.",
        )

    results = []
    loop    = asyncio.get_running_loop()
    _log_mem("compress début")

    for file in files:
        content = await file.read()
        stem    = _safe_stem(file.filename)
        if len(content) > _MAX_FILE_BYTES:
            mb = round(len(content) / 1024 / 1024, 1)
            results.append({
                "original_name":   file.filename or "image",
                "compressed_name": f"compressed_{stem}.jpg",
                "original_size":   len(content),
                "compressed_size": 0,
                "compressed_b64":  None,
                "mime":            "image/jpeg",
                "error":           f"Fichier trop volumineux ({mb} Mo — max {_MAX_FILE_BYTES//1024//1024} Mo).",
            })
            del content; gc.collect(); continue
        try:
            comp_result          = await _compress_async(content, file.filename, quality)
            compressed, ext, mime = comp_result
            comp_size            = len(compressed)
            comp_b64             = base64.b64encode(compressed).decode()
            del compressed  # libère immédiatement les bytes compressés
            results.append({
                "original_name":   file.filename or "image",
                "compressed_name": f"compressed_{stem}.{ext}",
                "original_size":   len(content),
                "compressed_size": comp_size,
                "compressed_b64":  comp_b64,
                "mime":            mime,
            })
        except Exception as exc:
            print(f"[WARN] Compression échouée {file.filename!r}: {exc}", flush=True)
            results.append({
                "original_name":   file.filename or "image",
                "compressed_name": f"compressed_{stem}.jpg",
                "original_size":   len(content),
                "compressed_size": 0,
                "compressed_b64":  None,
                "mime":            "image/jpeg",
                "error":           f"{type(exc).__name__}: {exc}",
            })
        finally:
            del content
            gc.collect()
        _log_mem(f"compress après {file.filename!r}")

    _log_mem("compress fin")
    return results


@app.post("/api/compress-zip")
async def api_compress_zip(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    if len(files) > 30:
        raise HTTPException(
            status_code=400,
            detail="Maximum 30 images par compression. Divisez votre lot en plusieurs envois.",
        )

    zip_buf = io.BytesIO()
    _log_mem("compress-zip début")

    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for file in files:
            content = await file.read()
            if len(content) > _MAX_FILE_BYTES:
                print(f"[WARN] ZIP compress skip {file.filename!r}: trop volumineux ({len(content)} o)", flush=True)
                del content; gc.collect(); continue
            try:
                comp_result          = await _compress_async(content, file.filename, quality)
                compressed, ext, _   = comp_result
                zf.writestr(f"compressed_{_safe_stem(file.filename)}.{ext}", compressed)
                del compressed
            except Exception as exc:
                print(f"[WARN] ZIP compress skip {file.filename!r}: {exc}", flush=True)
            finally:
                del content
                gc.collect()
            _log_mem(f"compress-zip après {file.filename!r}")

    _log_mem("compress-zip fin")
    return StreamingResponse(
        _stream_buf(zip_buf),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="images_compressed.zip"'},
    )


# ── Stats dashboard ────────────────────────────────────────────────────────────

@app.get("/stats", response_class=HTMLResponse)
async def stats_dashboard(request: Request, key: str = ""):
    if key != STATS_TOKEN:
        return Response(status_code=403)

    loop = asyncio.get_running_loop()

    def _query():
        conn = _db_conn()
        # Totaux globaux
        totals = conn.execute("""
            SELECT COUNT(*)                                          AS reqs,
                   COALESCE(SUM(files_count), 0)                    AS files,
                   COALESCE(SUM(original_bytes), 0)                 AS orig,
                   COALESCE(SUM(converted_bytes), 0)                AS conv,
                   COALESCE(ROUND(AVG(
                       100.0*(original_bytes-converted_bytes)/original_bytes
                   ),1), 0)                                         AS avg_gain
            FROM conversions
        """).fetchone()

        # Aujourd'hui
        today = conn.execute("""
            SELECT COUNT(*) AS reqs, COALESCE(SUM(files_count),0) AS files
            FROM conversions WHERE DATE(ts)=DATE('now')
        """).fetchone()

        # 14 derniers jours
        days = conn.execute("""
            SELECT DATE(ts) AS day,
                   COUNT(*)              AS reqs,
                   SUM(files_count)      AS files,
                   SUM(original_bytes)   AS orig,
                   SUM(converted_bytes)  AS conv
            FROM conversions
            WHERE ts >= DATETIME('now','-14 days')
            GROUP BY day ORDER BY day ASC
        """).fetchall()

        # 30 dernières conversions
        recent = conn.execute("""
            SELECT ts, files_count, original_bytes, converted_bytes, quality,
                   ROUND(100.0*(original_bytes-converted_bytes)/original_bytes,1) AS gain_pct
            FROM conversions ORDER BY ts DESC LIMIT 30
        """).fetchall()

        conn.close()
        return totals, today, days, recent

    totals, today_row, days, recent = await loop.run_in_executor(_executor, _query)

    saved = totals["orig"] - totals["conv"]
    max_files = max((r["files"] for r in days), default=1) or 1

    def bar(val):
        return max(4, round(val / max_files * 100))

    ctx = {
        "token":      key,
        "reqs":       totals["reqs"],
        "files":      totals["files"],
        "saved":      _fmt_bytes(saved),
        "avg_gain":   totals["avg_gain"],
        "today_reqs": today_row["reqs"],
        "today_files":today_row["files"],
        "days":       [dict(r) for r in days],
        "recent":     [dict(r) for r in recent],
        "bar":        bar,
        "fmt":        _fmt_bytes,
    }
    return templates.TemplateResponse(request, "stats.html", ctx)




