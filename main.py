import asyncio
import base64
import io
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
        # Assets versionnés : cache 1 an, immuable
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

# Thread pool dédié aux conversions CPU-bound (borné pour Render free tier)
_executor = ThreadPoolExecutor(max_workers=4)

# Favicon mis en cache en mémoire au démarrage
_FAVICON: bytes = b""
try:
    _FAVICON = Path("static/favicon.ico").read_bytes()
except FileNotFoundError:
    pass


# ── Conversion helpers ─────────────────────────────────────────────────────────

def _to_webp(content: bytes, quality: int) -> bytes:
    """Conversion synchrone — exécutée dans le thread pool."""
    img = Image.open(io.BytesIO(content))

    # Resize uniquement si le fichier dépasse 5 Mo ET que la résolution dépasse 4000px
    # Garde les proportions, accélère drastiquement la conversion sans perte visible
    MAX_BYTES = 5 * 1024 * 1024   # 5 Mo
    MAX_DIM   = 4000
    if len(content) > MAX_BYTES and (img.width > MAX_DIM or img.height > MAX_DIM):
        img.thumbnail((MAX_DIM, MAX_DIM), Image.BILINEAR)

    # Gestion de la transparence
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        src = img.convert("RGBA")
        bg.paste(src, mask=src.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    # method=0 : encodage le plus rapide, qualité suffisante pour le web
    img.save(buf, format="WebP", quality=quality, method=0)
    return buf.getvalue()


async def _to_webp_async(content: bytes, quality: int) -> bytes:
    """Lance _to_webp dans le thread pool sans bloquer la boucle asyncio."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _to_webp, content, quality)


async def _zip_stream(entries: List[tuple]) -> AsyncGenerator[bytes, None]:
    """Génère le ZIP en chunks de 64 Ko pour StreamingResponse."""
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(
        zip_buf, "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as zf:
        for filename, data in entries:
            zf.writestr(filename, data)
    zip_buf.seek(0)
    while chunk := zip_buf.read(65_536):
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


@app.post("/api/convert")
async def api_convert(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    # Lecture I/O en parallèle
    contents = await asyncio.gather(*[f.read() for f in files])

    # Conversions CPU en parallèle dans le thread pool
    webp_list = await asyncio.gather(
        *[_to_webp_async(c, quality) for c in contents]
    )

    total_orig = sum(len(c) for c in contents)
    total_conv = sum(len(w) for w in webp_list)
    await asyncio.get_running_loop().run_in_executor(
        _executor, _record, len(contents), total_orig, total_conv, quality
    )

    # original_b64 supprimé : le navigateur a déjà le fichier, inutile de le renvoyer
    return [
        {
            "original_name": file.filename,
            "webp_name": f"{file.filename.rsplit('.', 1)[0]}.webp",
            "original_size": len(content),
            "converted_size": len(webp_bytes),
            "webp_b64": base64.b64encode(webp_bytes).decode(),
        }
        for file, content, webp_bytes in zip(files, contents, webp_list)
    ]


@app.post("/api/convert-zip")
async def api_convert_zip(
    files: List[UploadFile] = File(...),
    quality: int = Form(60),
):
    # Lecture I/O en parallèle
    contents = await asyncio.gather(*[f.read() for f in files])

    # Conversions CPU en parallèle dans le thread pool
    webp_list = await asyncio.gather(
        *[_to_webp_async(c, quality) for c in contents]
    )

    total_orig = sum(len(c) for c in contents)
    total_conv = sum(len(w) for w in webp_list)
    await asyncio.get_running_loop().run_in_executor(
        _executor, _record, len(contents), total_orig, total_conv, quality
    )

    entries = [
        (f"{f.filename.rsplit('.', 1)[0]}.webp", wb)
        for f, wb in zip(files, webp_list)
    ]

    return StreamingResponse(
        _zip_stream(entries),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="images_webp.zip"'},
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


# ── SEO files ──────────────────────────────────────────────────────────────────

@app.get("/robots.txt")
async def robots_txt():
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n\n"
            "Sitemap: https://convertwebp.fr/sitemap.xml\n"
        ),
        media_type="text/plain",
    )


@app.get("/sitemap.xml")
async def sitemap_xml():
    today = date.today().isoformat()
    pages = [
        ("https://convertwebp.fr/",                                              "1.0", "weekly"),
        ("https://convertwebp.fr/blog",                                          "0.8", "weekly"),
        ("https://convertwebp.fr/blog/pourquoi-convertir-images-webp-seo",       "0.7", "monthly"),
        ("https://convertwebp.fr/blog/webp-vs-jpg-png-comparaison",              "0.7", "monthly"),
        ("https://convertwebp.fr/blog/optimiser-images-vitesse-site-google",     "0.7", "monthly"),
        ("https://convertwebp.fr/blog/seo-local-2026-guide-complet",             "0.8", "monthly"),
        ("https://convertwebp.fr/contact",                                       "0.6", "monthly"),
        ("https://convertwebp.fr/mentions-legales",                              "0.2", "yearly"),
        ("https://convertwebp.fr/politique-confidentialite",                     "0.2", "yearly"),
    ]
    entries = "\n".join(
        f"  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        f"  </url>"
        for loc, prio, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")
