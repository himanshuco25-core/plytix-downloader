import streamlit as st
import pandas as pd
import requests
import zipfile
import re
import time
import io
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image

# ── Image processing functions ───────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def detect_ratio(img: Image.Image) -> str:
    w, h  = img.size
    ratio = w / h
    if abs(ratio - 1.0)  <= 0.02: return '1:1'
    if abs(ratio - 0.75) <= 0.02: return '3:4'
    return 'unknown'


def resize_to_1080x1440(img: Image.Image) -> tuple:
    TARGET_W = 1080
    TARGET_H = 1440
    orig_w, orig_h = img.size
    ratio = detect_ratio(img)

    if ratio == 'unknown':
        return None, f'{orig_w}x{orig_h}', '-', '-', ratio, \
               f'Unexpected ratio {orig_w}x{orig_h} ({orig_w/orig_h:.2f}) — expected 1:1 or 3:4'

    if ratio == '3:4':
        img_final = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
        return img_final, f'{orig_w}x{orig_h}', f'{TARGET_W}x{TARGET_H}', \
               f'{TARGET_W}x{TARGET_H}', ratio, None

    if ratio == '1:1':
        action_note  = 'upscaled' if orig_w < TARGET_W else 'downscaled'
        img_scaled   = img.resize((TARGET_W, TARGET_W), Image.LANCZOS)
        canvas       = Image.new('RGB', (TARGET_W, TARGET_H), (255, 255, 255))
        offset_y     = (TARGET_H - TARGET_W) // 2
        canvas.paste(img_scaled, (0, offset_y))
        return canvas, f'{orig_w}x{orig_h}', f'{TARGET_W}x{TARGET_W} ({action_note})', \
               f'{TARGET_W}x{TARGET_H}', ratio, None


def compress_to_limit(img: Image.Image, max_mb: float = 1.8) -> bytes:
    max_bytes = int(max_mb * 1024 * 1024)
    for quality in range(95, 58, -2):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=60, optimize=True)
    return buf.getvalue()


def to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        return bg
    return img.convert('RGB')


# ── UI ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Plytix Asset Downloader", page_icon="📦", layout="wide")

st.title("📦 Plytix Asset Downloader")
st.markdown("Upload your CSV → images are processed → download as ZIP.")

with st.expander("📋 Expected CSV format"):
    st.markdown("""
| SKU | Assets | Style ID |
|---|---|---|
| NS14898-G | https://url1.jpg, https://url2.jpg | 1234567 |
| BS14897-G | https://url3.jpg | 1234568 |

- **SKU** — product code
- **Assets** — one or more image URLs separated by commas
- **Style ID** — used as the folder name (optional, falls back to SKU)
- All images converted to **JPG** automatically
""")

# ── Settings ──────────────────────────────────────────────────────────────────
with st.expander("⚙️ Processing Settings", expanded=True):
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        do_resize   = st.toggle("Resize to 1080×1440", value=True,
                                help="1:1 → scale to 1080x1080 then pad. 3:4 → scale directly to 1080x1440.")
    with col2:
        do_compress = st.toggle("Compress if > 2MB", value=True,
                                help="Compress images above threshold down to target size.")
    with col3:
        max_mb      = st.number_input("Compress threshold (MB)", value=2.0, step=0.1, min_value=0.5)
    with col4:
        target_mb   = st.number_input("Target size after compress (MB)", value=1.8, step=0.1, min_value=0.3)

st.divider()

# ── Upload ────────────────────────────────────────────────────────────────────
uploaded_file = st.file_uploader("Upload CSV", type=['csv'])

if uploaded_file:
    df = pd.read_csv(uploaded_file)

    missing = [c for c in ['SKU', 'Assets'] if c not in df.columns]
    if missing:
        st.error(f"Missing required columns: {missing}. Got: {df.columns.tolist()}")
        st.stop()

    if 'Style ID' not in df.columns:
        st.warning("⚠️ 'Style ID' column not found — using SKU as folder name.")
        df['Style ID'] = df['SKU']

    df = df.dropna(subset=['Assets']).reset_index(drop=True)
    df['url_list'] = df['Assets'].apply(
        lambda x: [u.strip() for u in str(x).split(',') if u.strip()]
    )

    total_images = int(df['url_list'].apply(len).sum())
    st.success(f"✅ {len(df)} SKUs | {total_images} total images")

    preview_df = df[['SKU', 'Style ID']].copy()
    preview_df['Image Count'] = df['url_list'].apply(len)
    st.dataframe(preview_df, use_container_width=True)

    st.divider()

    if st.button("⬇️ Start Processing & Build ZIP", type="primary", use_container_width=True):

        zip_buffer   = io.BytesIO()
        progress_bar = st.progress(0, text="Starting...")
        log_rows     = []
        total_done   = 0

        # ── Live log container ─────────────────────────────────────────────────
        st.markdown("### 📋 Processing Log")
        log_container = st.container()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for _, row in df.iterrows():
                style_id = sanitize(str(row['Style ID']).strip())
                sku      = str(row['SKU']).strip()
                urls     = row['url_list']

                with log_container:
                    st.markdown(f"---\n**📁 Style ID: `{style_id}`** | SKU: `{sku}` | {len(urls)} images")

                for rank, url in enumerate(urls, start=1):
                    filename = f"{rank}.jpg"
                    zip_path = f"{style_id}/{filename}"

                    # Download
                    try:
                        resp    = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        content = resp.content
                    except Exception as e:
                        with log_container:
                            st.error(f"❌ [{rank}] `{filename}` → Download error: {e}")
                        log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                         'URL': url, 'Status': f'error:{e}', 'Action': 'error',
                                         'Original MB': 0, 'Final MB': 0,
                                         'Original Dim': '-', 'Scaled Dim': '-', 'Final Dim': '-',
                                         'Ratio': '-'})
                        total_done += 1
                        progress_bar.progress(total_done / total_images,
                                              text=f"{total_done}/{total_images} processed")
                        continue

                    raw_mb = len(content) / (1024 * 1024)

                    # Convert to RGB
                    img = Image.open(io.BytesIO(content))
                    img = to_rgb(img)

                    # Resize
                    if do_resize:
                        img, dim_orig, dim_scaled, dim_final, ratio, err = resize_to_1080x1440(img)
                        if err:
                            with log_container:
                                st.warning(f"⚠️ [{rank}] `{filename}` → {err} — **skipped**")
                            log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                             'URL': url, 'Status': err, 'Action': 'skipped-bad-ratio',
                                             'Original MB': round(raw_mb, 2), 'Final MB': 0,
                                             'Original Dim': dim_orig, 'Scaled Dim': '-',
                                             'Final Dim': '-', 'Ratio': ratio})
                            total_done += 1
                            progress_bar.progress(total_done / total_images,
                                                  text=f"{total_done}/{total_images} processed")
                            continue
                    else:
                        dim_orig   = f'{img.width}x{img.height}'
                        dim_scaled = dim_orig
                        dim_final  = dim_orig
                        ratio      = detect_ratio(img)

                    # Compress
                    needs_compression = raw_mb > max_mb

                    if not needs_compression:
                        buf = io.BytesIO()
                        img.save(buf, format='JPEG', quality=95, optimize=True)
                        final_bytes = buf.getvalue()
                        final_mb    = len(final_bytes) / (1024 * 1024)

                        if final_mb > max_mb:
                            final_bytes = compress_to_limit(img, target_mb)
                            final_mb    = len(final_bytes) / (1024 * 1024)
                            action      = 'compressed'
                        else:
                            action = 'resized' if do_resize else 'converted'
                    else:
                        final_bytes = compress_to_limit(img, target_mb)
                        final_mb    = len(final_bytes) / (1024 * 1024)
                        action      = 'compressed'

                    zf.writestr(zip_path, final_bytes)

                    # ── Per image log ──────────────────────────────────────────
                    size_change = f"{raw_mb:.2f}MB → {final_mb:.2f}MB"
                    dim_change  = f"{dim_orig} ({ratio}) → {dim_scaled} → {dim_final}"
                    tag         = "🗜️ compressed" if action == 'compressed' else "📐 resized" if action == 'resized' else "🔄 converted"

                    with log_container:
                        col_a, col_b, col_c = st.columns([1, 3, 2])
                        col_a.markdown(f"✅ `[{rank}] {filename}`")
                        col_b.markdown(f"📐 {dim_change}")
                        col_c.markdown(f"💾 {size_change} | {tag}")

                    log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                     'URL': url, 'Status': 'ok', 'Action': action,
                                     'Original MB': round(raw_mb, 2), 'Final MB': round(final_mb, 2),
                                     'Original Dim': dim_orig, 'Scaled Dim': dim_scaled,
                                     'Final Dim': dim_final, 'Ratio': ratio})

                    total_done += 1
                    progress_bar.progress(
                        total_done / total_images,
                        text=f"{total_done}/{total_images} images processed"
                    )

        zip_buffer.seek(0)
        progress_bar.empty()

        # ── Summary ───────────────────────────────────────────────────────────
        st.divider()
        st.subheader("📊 Summary")

        log_df     = pd.DataFrame(log_rows)
        ok         = (log_df['Status'] == 'ok').sum()
        errors     = (log_df['Status'] != 'ok').sum()
        compressed = (log_df['Action'] == 'compressed').sum()
        resized    = (log_df['Action'] == 'resized').sum()
        converted  = (log_df['Action'] == 'converted').sum()
        bad_ratio  = (log_df['Action'] == 'skipped-bad-ratio').sum()

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("✅ Success",       ok)
        c2.metric("❌ Errors",        errors)
        c3.metric("📐 Resized",       resized)
        c4.metric("🗜️ Compressed",    compressed)
        c5.metric("🔄 JPG converted", converted)
        c6.metric("⚠️ Bad ratio",     bad_ratio)

        ok_df = log_df[log_df['Status'] == 'ok']
        if not ok_df.empty:
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("Avg original size", f"{ok_df['Original MB'].mean():.2f} MB")
            cc2.metric("Avg final size",    f"{ok_df['Final MB'].mean():.2f} MB")
            cc3.metric("Total size saved",  f"{(ok_df['Original MB'].sum() - ok_df['Final MB'].sum()):.1f} MB")

        if bad_ratio > 0:
            st.warning("⚠️ Some images had unexpected ratios and were skipped. Check log CSV for details.")

        st.divider()

        st.download_button(
            label               = "📥 Download ZIP",
            data                = zip_buffer,
            file_name           = "plytix_assets.zip",
            mime                = "application/zip",
            use_container_width = True,
            type                = "primary"
        )

        log_csv = log_df.to_csv(index=False).encode()
        st.download_button(
            label               = "📄 Download Log CSV",
            data                = log_csv,
            file_name           = "download_log.csv",
            mime                = "text/csv",
            use_container_width = True
        )
