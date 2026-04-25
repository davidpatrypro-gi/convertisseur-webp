import asyncio
import base64
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import AsyncGenerator, List

import resend
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydantic import BaseModel

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
app.add_middleware(GZipMiddleware, minimum_size=1000)  # compresse HTML/JSON/CSS/JS > 1 Ko
templates = Jinja2Templates(directory="templates")


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

# Thread pool dédié aux conversions CPU-bound
_executor = ThreadPoolExecutor()


# ── Conversion helpers ─────────────────────────────────────────────────────────

def _to_webp(content: bytes, quality: int) -> bytes:
    """Conversion synchrone — exécutée dans le thread pool."""
    img = Image.open(io.BytesIO(content))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        src = img.convert("RGBA")
        bg.paste(src, mask=src.split()[3])
        img = bg
    else:
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="WebP", quality=quality)
    return buf.getvalue()


async def _to_webp_async(content: bytes, quality: int) -> bytes:
    """Lance _to_webp dans le thread pool sans bloquer la boucle asyncio."""
    loop = asyncio.get_event_loop()
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
    """Sert le favicon à la racine (requis par Google Search et les navigateurs)."""
    import os
    favicon_path = os.path.join("static", "favicon.ico")
    with open(favicon_path, "rb") as f:
        content = f.read()
    return Response(content=content, media_type="image/x-icon")


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


@app.get("/api/contact/test")
async def api_contact_test():
    """Route de diagnostic — envoie un email de test à contact@convertwebp.fr.
    Accéder à https://convertwebp.fr/api/contact/test pour vérifier Resend."""
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(_executor, lambda: resend.Emails.send({
            "from":    FROM_EMAIL,
            "to":      [CONTACT_EMAIL],
            "subject": "[TEST] Diagnostic Resend — ConvertWebP",
            "html":    "<p>Si vous recevez cet email, Resend fonctionne correctement.</p>",
        }))
        print(f"[Resend TEST] résultat : {r}")
        return JSONResponse({"ok": True, "resend_response": str(r)})
    except Exception as exc:
        print(f"[Resend TEST] ERREUR : {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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

    return [
        {
            "original_name": file.filename,
            "webp_name": f"{file.filename.rsplit('.', 1)[0]}.webp",
            "original_size": len(content),
            "converted_size": len(webp_bytes),
            "original_b64": base64.b64encode(content).decode(),
            "original_mime": file.content_type or "image/jpeg",
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

    entries = [
        (f"{f.filename.rsplit('.', 1)[0]}.webp", wb)
        for f, wb in zip(files, webp_list)
    ]

    return StreamingResponse(
        _zip_stream(entries),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="images_webp.zip"'},
    )


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
        ("https://convertwebp.fr/mentions-legales",                              "0.3", "yearly"),
        ("https://convertwebp.fr/politique-confidentialite",                     "0.3", "yearly"),
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
