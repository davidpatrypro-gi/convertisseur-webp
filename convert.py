import io
import zipfile

import streamlit as st
import streamlit_analytics2 as streamlit_analytics
from PIL import Image

st.set_page_config(page_title="Convertisseur WebP", layout="wide")

streamlit_analytics.start_tracking()

st.title("🕸️ Convertisseur JPG/PNG vers WebP")
st.write("Transformez vos images au format WebP pour booster votre SEO.")

uploaded_files = st.file_uploader(
    "Choisissez des images...",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True,
)

if uploaded_files:
    quality = st.sidebar.slider("Qualité WebP (80 est idéal)", 1, 100, 80)

    # --- Conversion de toutes les images (en mémoire) ---
    progress_bar = st.progress(0)
    total = len(uploaded_files)
    converted: list[tuple[str, bytes]] = []  # (webp_name, webp_bytes)

    for i, uploaded_file in enumerate(uploaded_files):
        image = Image.open(uploaded_file)

        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            background = Image.new("RGB", image.size, (255, 255, 255))
            src = image.convert("RGBA")
            background.paste(src, mask=src.split()[3])
            image = background
        else:
            image = image.convert("RGB")

        buf = io.BytesIO()
        image.save(buf, format="WebP", quality=quality)

        stem = uploaded_file.name.rsplit(".", 1)[0]
        converted.append((f"{stem}.webp", buf.getvalue()))
        progress_bar.progress((i + 1) / total)

    progress_bar.empty()

    # --- Bouton ZIP (affiché en haut, avant les expanders) ---
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for webp_name, webp_bytes in converted:
            zf.writestr(webp_name, webp_bytes)

    st.download_button(
        label="⬇️ Télécharger toutes les images en ZIP",
        data=zip_buf.getvalue(),
        file_name="images_webp.zip",
        mime="application/zip",
        use_container_width=True,
    )

    # --- Résumé global de compression ---
    total_original = sum(f.size for f in uploaded_files)
    total_converted = sum(len(b) for _, b in converted)
    gain = total_original - total_converted
    pct = gain / total_original * 100 if total_original else 0

    def fmt(size_bytes: int) -> str:
        mb = size_bytes / (1024 ** 2)
        return f"{mb:.2f} MB" if mb >= 1 else f"{size_bytes / 1024:.1f} KB"

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("📁 Poids original", fmt(total_original))
    col_b.metric("🌐 Poids WebP", fmt(total_converted))
    col_c.metric("💾 Gain", fmt(gain))
    col_d.metric("📉 Réduction", f"{pct:.1f}%")

    st.divider()

    # --- Aperçus et téléchargements individuels ---
    for uploaded_file, (webp_name, webp_bytes) in zip(uploaded_files, converted):
        with st.expander(f"Image : {uploaded_file.name}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Avant**")
                uploaded_file.seek(0)
                st.image(uploaded_file, use_container_width=True)
                st.write(f"Format original : {uploaded_file.type}")
                st.write(f"Poids : {uploaded_file.size / 1024:.2f} KB")
            with col2:
                st.write("**Après (WebP)**")
                st.image(webp_bytes, use_container_width=True)
                st.write("Format cible : **WebP**")
                st.write(f"Nouveau poids : {len(webp_bytes) / 1024:.2f} KB")

                st.download_button(
                    label=f"Télécharger {webp_name}",
                    data=webp_bytes,
                    file_name=webp_name,
                    mime="image/webp",
                    key=uploaded_file.name,
                )

streamlit_analytics.stop_tracking()
