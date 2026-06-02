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


def to_rgb(img: Image.Image) -> Image.Image:
    """Convert any image mode to RGB with white background for transparency."""
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        return bg
    return img.convert('RGB')


def resize_to_1080x1440(img: Image.Image) -> Image.Image:
    """
    Resize to exactly 1080x1440 with white padding.
    Never crops. Never upscales. Centers product on white canvas.
    """
    TARGET_W, TARGET_H = 1080, 1440
    if img.size == (TARGET_W, TARGET_H):
        return img
    scale = min(TARGET_W / img.width, TARGET_H / img.height)
    if scale > 1:
        scale = 1
    new_w = int(img.width * scale)
    new_h = int(img.height * scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas   = Image.new('RGB', (TARGET_W, TARGET_H), (255, 255, 255))
    offset_x = (TARGET_W - new_w) // 2
    offset_y = (TARGET_H - new_h) // 2
    canvas.paste(img_resized, (offset_x, offset_y))
    return canvas


def compress_to_limit(img: Image.Image, max_mb: float = 1.8) -> bytes:
    """Compress image to under max_mb. Starts quality at 95, steps down by 2."""
    max_bytes = int(max_mb * 1024 * 1024)
    for quality in range(95, 58, -2):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=60, optimize=True)
    return buf.getvalue()


def process_image(content: bytes, resize: bool, compress: bool,
                  max_mb: float, target_mb: float) -> tuple:
    """
    Full processing pipeline:
    1. Convert to RGB / JPG
    2. Resize to 1080x1440 with white padding (if enabled)
    3. Compress to target size (if over limit)
    Returns (final_bytes, log_dict)
    """
    raw_mb  = len(content) / (1024 * 1024)
    img     = Image.open(io.BytesIO(content))
    orig_w, orig_h = img.size
    img     = to_rgb(img)

    # Resize
    if resize:
        img = resize_to_1080x1440(img)

    final_w, final_h = img.size

    # Save at high quality first
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, optimize=True)
    final_bytes = buf.getvalue()
    final_mb    = len(final_bytes) / (1024 * 1024)
    action      = 'resized' if resize else 'converted'

    # Compress if needed
    if compress and raw_mb > max_mb:
        final_bytes = compress_to_limit(img, target_mb)
        final_mb    = len(final_bytes) / (1024 * 1024)
        action      = 'compressed'
    elif compress and final_mb > max_mb:
        # Edge case: resize made it larger
        final_bytes = compress_to_limit(img, target_mb)
        final_mb    = len(final_bytes) / (1024 * 1024)
        action      = 'compressed'

    log = {
        'orig_dim'  : f'{orig_w}x{orig_h}',
        'final_dim' : f'{final_w}x{final_h}',
        'raw_mb'    : round(raw_mb, 2),
        'final_mb'  : round(final_mb, 2),
        'action'    : action,
    }
    return final_bytes, log


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
""")

# ── Settings panel ────────────────────────────────────────────────────────────
with st.expander("⚙️ Processing Settings", expanded=True):
    col1, col2, col3 = st.columns(3)
    with col1:
        do_resize   = st.toggle("Resize to 1080×1440", value=True,
                                help="Scale image to fit 1080x1440 with white padding. Never crops.")
    with col2:
        do_compress = st.toggle("Compress if > 2MB", value=True,
                                help="Compress images above 2MB down to 1.8MB.")
    with col3:
        max_mb      = st.number_input("Compress threshold (MB)", value=2.0, step=0.1, min_value=0.5)
        target_mb   = st.number_input("Target size after compress (MB)", value=1.8, step=0.1, min_value=0.3)

st.divider()

# ── File upload ───────────────────────────────────────────────────────────────
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
        log_box      = st.empty()
        log_rows     = []
        live_logs    = []
        total_done   = 0

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for _, row in df.iterrows():
                style_id = sanitize(str(row['Style ID']).strip())
                sku      = str(row['SKU']).strip()
                urls     = row['url_list']

                folder_log = f"\n📁 **Style ID: {style_id}** | SKU: {sku} | {len(urls)} images"
                live_logs.append(folder_log)
                log_box.markdown("\n".join(live_logs))

                for rank, url in enumerate(urls, start=1):
                    filename = f"{rank}.jpg"
                    zip_path = f"{style_id}/{filename}"

                    # Download
                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        content = resp.content
                    except Exception as e:
                        msg = f"  ❌ [{rank}] {filename} → download error: {e}"
                        live_logs.append(msg)
                        log_box.markdown("\n".join(live_logs))
                        log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                         'URL': url, 'Status': f'error:{e}',
                                         'Action': 'error', 'Original MB': 0, 'Final MB': 0,
                                         'Original Dim': '-', 'Final Dim': '-'})
                        total_done += 1
                        progress_bar.progress(total_done / total_images,
                                              text=f"{total_done}/{total_images} processed")
                        continue

                    # Process
                    try:
                        final_bytes, plog = process_image(
                            content,
                            resize   = do_resize,
                            compress = do_compress,
                            max_mb   = max_mb,
                            target_mb= target_mb
                        )
                        zf.writestr(zip_path, final_bytes)
                        status = 'ok'

                        if plog['action'] == 'compressed':
                            msg = (f"  ✅ [{rank}] {filename}"
                                   f" → {plog['orig_dim']} → {plog['final_dim']}"
                                   f" | {plog['raw_mb']}MB → {plog['final_mb']}MB"
                                   f" | 🗜️ compressed")
                        elif plog['action'] == 'resized':
                            msg = (f"  ✅ [{rank}] {filename}"
                                   f" → {plog['orig_dim']} → {plog['final_dim']}"
                                   f" | {plog['raw_mb']}MB → {plog['final_mb']}MB"
                                   f" | 📐 resized")
                        else:
                            msg = (f"  ✅ [{rank}] {filename}"
                                   f" → {plog['orig_dim']} → {plog['final_dim']}"
                                   f" | {plog['raw_mb']}MB → {plog['final_mb']}MB"
                                   f" | 🔄 converted to JPG")

                    except Exception as e:
                        final_bytes = content
                        plog   = {'orig_dim': '-', 'final_dim': '-',
                                  'raw_mb': 0, 'final_mb': 0, 'action': 'error'}
                        status = f'error: processing failed — {e}'
                        msg    = f"  ❌ [{rank}] {filename} → processing error: {e}"

                    live_logs.append(msg)
                    log_box.markdown("\n".join(live_logs))

                    log_rows.append({
                        'SKU'         : sku,
                        'Style ID'    : style_id,
                        'File'        : filename,
                        'URL'         : url,
                        'Status'      : status,
                        'Action'      : plog['action'],
                        'Original MB' : plog['raw_mb'],
                        'Final MB'    : plog['final_mb'],
                        'Original Dim': plog['orig_dim'],
                        'Final Dim'   : plog['final_dim'],
                    })

                    total_done += 1
                    progress_bar.progress(
                        total_done / total_images,
                        text=f"{total_done}/{total_images} images processed"
                    )

        zip_buffer.seek(0)
        progress_bar.empty()

        # ── Summary ───────────────────────────────────────────────────────────
        st.divider()
        log_df = pd.DataFrame(log_rows)
        ok          = (log_df['Status'] == 'ok').sum()
        errors      = (log_df['Status'] != 'ok').sum()
        compressed  = (log_df['Action'] == 'compressed').sum()
        resized     = (log_df['Action'] == 'resized').sum()
        converted   = (log_df['Action'] == 'converted').sum()

        st.subheader("📊 Summary")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("✅ Success",    ok)
        c2.metric("❌ Errors",     errors)
        c3.metric("📐 Resized",    resized)
        c4.metric("🗜️ Compressed", compressed)
        c5.metric("🔄 JPG only",   converted)

        ok_df = log_df[log_df['Status'] == 'ok']
        if not ok_df.empty:
            col1, col2, col3 = st.columns(3)
            col1.metric("Avg original size", f"{ok_df['Original MB'].mean():.2f} MB")
            col2.metric("Avg final size",    f"{ok_df['Final MB'].mean():.2f} MB")
            col3.metric("Total size saved",  f"{(ok_df['Original MB'].sum() - ok_df['Final MB'].sum()):.1f} MB")

        st.divider()

        st.download_button(
            label               = "📥 Download ZIP",
            data                = zip_buffer,
            file_name           = "plytix_assets.zip",
            mime                = "application/zip",
            use_container_width = True,
            type                = "primary"
        )

        if errors:
            st.markdown("**Failed downloads:**")
            st.dataframe(
                log_df[log_df['Status'] != 'ok'][['SKU', 'Style ID', 'File', 'URL', 'Status']],
                use_container_width=True
            )

        log_csv = log_df.to_csv(index=False).encode()
        st.download_button(
            label               = "📄 Download Log CSV",
            data                = log_csv,
            file_name           = "download_log.csv",
            mime                = "text/csv",
            use_container_width = True
        )
