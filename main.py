import base64
import io
import zipfile
from datetime import date
from typing import List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

app = FastAPI(title="ConvertWebP — Convertisseur images en ligne")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Conversion helper ──────────────────────────────────────────────────────────

def _to_webp(content: bytes, quality: int) -> bytes:
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


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/mentions-legales", response_class=HTMLResponse)
async def mentions_legales(request: Request):
    return templates.TemplateResponse("mentions-legales.html", {"request": request})


@app.get("/politique-confidentialite", response_class=HTMLResponse)
async def politique_confidentialite(request: Request):
    return templates.TemplateResponse("politique-confidentialite.html", {"request": request})


@app.get("/blog", response_class=HTMLResponse)
async def blog(request: Request):
    return templates.TemplateResponse("blog/index.html", {"request": request})


@app.get("/blog/pourquoi-convertir-images-webp-seo", response_class=HTMLResponse)
async def blog_webp_seo(request: Request):
    return templates.TemplateResponse(
        "blog/pourquoi-convertir-images-webp-seo.html", {"request": request}
    )


@app.get("/blog/webp-vs-jpg-png-comparaison", response_class=HTMLResponse)
async def blog_webp_vs(request: Request):
    return templates.TemplateResponse(
        "blog/webp-vs-jpg-png-comparaison.html", {"request": request}
    )


@app.get("/blog/optimiser-images-vitesse-site-google", response_class=HTMLResponse)
async def blog_optimiser(request: Request):
    return templates.TemplateResponse(
        "blog/optimiser-images-vitesse-site-google.html", {"request": request}
    )


# ── API ────────────────────────────────────────────────────────────────────────

@app.post("/api/convert")
async def api_convert(
    files: List[UploadFile] = File(...),
    quality: int = Form(80),
):
    results = []
    for file in files:
        content = await file.read()
        webp_bytes = _to_webp(content, quality)
        stem = file.filename.rsplit(".", 1)[0]
        results.append({
            "original_name": file.filename,
            "webp_name": f"{stem}.webp",
            "original_size": len(content),
            "converted_size": len(webp_bytes),
            "original_b64": base64.b64encode(content).decode(),
            "original_mime": file.content_type or "image/jpeg",
            "webp_b64": base64.b64encode(webp_bytes).decode(),
        })
    return results


@app.post("/api/convert-zip")
async def api_convert_zip(
    files: List[UploadFile] = File(...),
    quality: int = Form(80),
):
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            content = await file.read()
            webp_bytes = _to_webp(content, quality)
            stem = file.filename.rsplit(".", 1)[0]
            zf.writestr(f"{stem}.webp", webp_bytes)
    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=images_webp.zip"},
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
