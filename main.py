import asyncio
import base64
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import AsyncGenerator, List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

app = FastAPI(title="ConvertWebP — Convertisseur images en ligne")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    img.save(buf, format="WebP", quality=quality, method=6, optimize=True)
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

@app.post("/api/convert")
async def api_convert(
    files: List[UploadFile] = File(...),
    quality: int = Form(80),
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
    quality: int = Form(80),
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
