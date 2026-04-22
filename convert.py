import io
import zipfile

import altair as alt
import pandas as pd
import streamlit as st
import streamlit_analytics2 as streamlit_analytics
import streamlit_analytics2.display as _sa2_display
import streamlit_analytics2.utils as _sa2_utils
from PIL import Image


def _show_results_fr(data, reset_callback, unsafe_password=None):
    """Version française de streamlit_analytics2.display.show_results."""
    st.title("Tableau de bord Analytics")
    st.markdown(
        "Vous consultez les statistiques de l'application. "
        "Retirez `?analytics=on` de l'URL pour revenir à l'app."
    )

    show = True
    if unsafe_password is not None:
        password_input = st.text_input("Mot de passe", type="password")
        if password_input != unsafe_password:
            show = False
            if len(password_input) > 0:
                st.write("Mot de passe incorrect.")

    if show:
        st.header("Trafic")
        st.write(f"depuis le {data['start_time']}")
        col1, col2, col3 = st.columns(3)
        col1.metric("Pages vues", data["total_pageviews"],
                    help="Chaque fois qu'un utilisateur charge la page.")
        col2.metric("Interactions", data["total_script_runs"],
                    help="Chaque fois que Streamlit relance le script.")
        col3.metric("Temps passé", _sa2_utils.format_seconds(data["total_time_seconds"]),
                    help="Temps total cumulé sur tous les utilisateurs.")
        st.write("")

        df = pd.DataFrame(data["per_day"])
        if pd.to_datetime(df["days"]).dt.year.nunique() > 1:
            x_axis_ticks = "yearmonthdate(days):O"
        else:
            x_axis_ticks = "monthdate(days):O"

        base = alt.Chart(df).encode(
            x=alt.X(x_axis_ticks, axis=alt.Axis(title="", grid=True))
        )
        line1 = base.mark_line(point=True, stroke="#5276A7").encode(
            alt.Y("pageviews:Q", axis=alt.Axis(
                titleColor="#5276A7", tickColor="#5276A7",
                labelColor="#5276A7", format=".0f", tickMinStep=1,
            ), scale=alt.Scale(domain=(0, df["pageviews"].max() + 1)))
        )
        line2 = base.mark_line(point=True, stroke="#57A44C").encode(
            alt.Y("script_runs:Q", axis=alt.Axis(
                title="interactions", titleColor="#57A44C",
                tickColor="#57A44C", labelColor="#57A44C",
                format=".0f", tickMinStep=1,
            ))
        )
        st.altair_chart(
            alt.layer(line1, line2)
            .resolve_scale(y="independent")
            .configure_axis(titleFontSize=15, labelFontSize=12, titlePadding=10),
            use_container_width=True,
        )

        st.header("Interactions par widget")
        for i in data["widgets"].keys():
            st.markdown(f"##### Widget `{i}`")
            if isinstance(data["widgets"][i], dict):
                st.dataframe(
                    pd.DataFrame({
                        "widget": i,
                        "valeur": list(data["widgets"][i].keys()),
                        "interactions": data["widgets"][i].values(),
                    }).sort_values(by="interactions", ascending=False)
                )
            else:
                st.dataframe(
                    pd.DataFrame({
                        "widget": i,
                        "interactions": data["widgets"][i],
                    }, index=[0])
                )

        st.header("Zone dangereuse")
        with st.expander("Réinitialiser les statistiques"):
            st.write("**Attention : cette action effacera toutes les données de suivi.**")
            choix = st.selectbox("Continuer ?", [
                "Non, annuler",
                "Oui, je veux réinitialiser",
            ])
            if choix == "Oui, je veux réinitialiser":
                if st.button("Confirmer la réinitialisation"):
                    reset_callback()
                    st.write("Réinitialisation effectuée. Rechargez la page.")


_sa2_display.show_results = _show_results_fr

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
