import gc
import streamlit as st
import pandas as pd
import requests
import zipfile
import re
import time
import io
import numpy as np
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

    if ratio == '3:4':
        img_final = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
        return img_final, f'{orig_w}x{orig_h}', f'{TARGET_W}x{TARGET_H}', \
               f'{TARGET_W}x{TARGET_H}', ratio, None

    if ratio == '1:1':
        action_note = 'upscaled' if orig_w < TARGET_W else 'downscaled'
        img_scaled  = img.resize((TARGET_W, TARGET_W), Image.LANCZOS)
        canvas      = Image.new('RGB', (TARGET_W, TARGET_H), (255, 255, 255))
        offset_y    = (TARGET_H - TARGET_W) // 2
        canvas.paste(img_scaled, (0, offset_y))
        img_scaled.close()
        return canvas, f'{orig_w}x{orig_h}', f'{TARGET_W}x{TARGET_W} ({action_note})', \
               f'{TARGET_W}x{TARGET_H}', ratio, None

    scale = TARGET_W / orig_w
    new_w = TARGET_W
    new_h = int(orig_h * scale)

    if new_h > TARGET_H:
        return None, f'{orig_w}x{orig_h}', '-', '-', f'unknown ({orig_w/orig_h:.2f})', \
               f'Height after scaling = {new_h}px exceeds 1440px — ratio {orig_w}:{orig_h} not supported'

    img_scaled = img.resize((new_w, new_h), Image.LANCZOS)
    canvas     = Image.new('RGB', (TARGET_W, TARGET_H), (255, 255, 255))
    offset_y   = (TARGET_H - new_h) // 2
    canvas.paste(img_scaled, (0, offset_y))
    img_scaled.close()

    return canvas, f'{orig_w}x{orig_h}', f'{new_w}x{new_h}', \
           f'{TARGET_W}x{TARGET_H}', f'unknown ({orig_w/orig_h:.2f})', None


def adjust_size_to_range(img: Image.Image, min_mb: float, max_mb: float) -> tuple:
    min_bytes = int(min_mb * 1024 * 1024)
    max_bytes = int(max_mb * 1024 * 1024)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, optimize=True)
    size = buf.tell()

    if min_bytes <= size <= max_bytes:
        return buf.getvalue(), round(size / (1024*1024), 2), 'ok'

    if size > max_bytes:
        for quality in range(93, 58, -2):
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue(), round(buf.tell() / (1024*1024), 2), 'compressed'
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=60, optimize=True)
        return buf.getvalue(), round(buf.tell() / (1024*1024), 2), 'compressed'

    if size < min_bytes:
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=100, optimize=True)
        if buf.tell() >= min_bytes:
            return buf.getvalue(), round(buf.tell() / (1024*1024), 2), 'quality-boosted'

        for noise_level in [1, 2, 3, 4, 5]:
            img_array = np.array(img, dtype=np.int16)
            noise     = np.random.randint(-noise_level, noise_level + 1,
                                          img_array.shape, dtype=np.int16)
            noisy_img = Image.fromarray(
                np.clip(img_array + noise, 0, 255).astype(np.uint8)
            )
            del img_array, noise
            buf = io.BytesIO()
            noisy_img.save(buf, format='JPEG', quality=100, optimize=True)

            if buf.tell() >= min_bytes:
                if buf.tell() <= max_bytes:
                    result = buf.getvalue(), round(buf.tell() / (1024*1024), 2), \
                             f'noise-boosted (level {noise_level})'
                    noisy_img.close()
                    return result
                else:
                    for quality in range(93, 58, -2):
                        buf2 = io.BytesIO()
                        noisy_img.save(buf2, format='JPEG', quality=quality, optimize=True)
                        if min_bytes <= buf2.tell() <= max_bytes:
                            result = buf2.getvalue(), round(buf2.tell() / (1024*1024), 2), \
                                     f'noise-boosted then compressed (level {noise_level})'
                            noisy_img.close()
                            return result
            noisy_img.close()

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=100, optimize=True)
        return buf.getvalue(), round(buf.tell() / (1024*1024), 2), 'REJECTED-below-500kb'


def to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        return bg
    return img.convert('RGB')


def get_size_tag(size_action: str) -> str:
    if size_action == 'ok':
        return '✅ in range'
    elif size_action == 'compressed':
        return '🗜️ compressed — in range'
    elif size_action == 'quality-boosted':
        return '✅ quality boosted — in range'
    elif 'noise-boosted' in size_action:
        return f'✅ {size_action} — in range'
    elif size_action == 'REJECTED-below-500kb':
        return '❌ REJECTED — cannot reach 500KB, needs re-photography'
    return size_action


# ── Session state init ────────────────────────────────────────────────────────
if 'zip_buffer' not in st.session_state: st.session_state.zip_buffer = None
if 'log_df'     not in st.session_state: st.session_state.log_df     = None
if 'done'       not in st.session_state: st.session_state.done       = False

# ── UI ────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Plytix Asset Downloader", page_icon="📦", layout="wide")
st.title("📦 Plytix Asset Downloader")

# ── Results screen ────────────────────────────────────────────────────────────
if st.session_state.done:
    st.success("✅ Processing complete! Download your files below.")

    log_df     = st.session_state.log_df
    ok         = (log_df['Status'] == 'ok').sum()
    rejected   = (log_df['Status'] == 'rejected').sum()
    errors     = (log_df['Status'].str.startswith('error')).sum()
    compressed = (log_df['Size Action'] == 'compressed').sum()
    boosted    = log_df['Size Action'].str.contains('boosted', na=False).sum()
    bad_ratio  = (log_df['Action'] == 'skipped-bad-ratio').sum()

    st.subheader("📊 Summary")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("✅ Success",         ok)
    c2.metric("❌ Rejected <500KB", rejected)
    c3.metric("⚠️ Bad ratio",       bad_ratio)
    c4.metric("❌ Errors",           errors)
    c5.metric("🗜️ Compressed",      compressed)
    c6.metric("⬆️ Boosted",         boosted)

    ok_df = log_df[log_df['Status'] == 'ok']
    if not ok_df.empty:
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Avg original size", f"{ok_df['Original MB'].mean():.2f} MB")
        cc2.metric("Avg final size",    f"{ok_df['Final MB'].mean():.2f} MB")
        cc3.metric("Total size saved",  f"{(ok_df['Original MB'].sum() - ok_df['Final MB'].sum()):.1f} MB")

    if rejected > 0:
        st.error(f"⚠️ {rejected} image(s) could not reach 500KB — check log CSV for details.")

    st.divider()

    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            label     = "📥 Download ZIP",
            data      = st.session_state.zip_buffer,
            file_name = "plytix_assets.zip",
            mime      = "application/zip",
            width     = 'stretch',
            type      = "primary"
        )
    with dl2:
        st.download_button(
            label     = "📄 Download Log CSV",
            data      = log_df.to_csv(index=False).encode(),
            file_name = "download_log.csv",
            mime      = "text/csv",
            width     = 'stretch'
        )

    with st.expander("📋 Full Processing Log", expanded=False):
        st.dataframe(log_df, width='stretch')

    st.divider()
    if st.button("🔄 Process Another Batch", width='stretch'):
        st.session_state.zip_buffer = None
        st.session_state.log_df     = None
        st.session_state.done       = False
        st.rerun()

    st.stop()

# ── Upload screen ─────────────────────────────────────────────────────────────
st.markdown("Upload your CSV → images are processed → download as ZIP.")

with st.expander("📋 Expected CSV format"):
    st.markdown("""
| SKU | Assets | Style ID |
|---|---|---|
| NS14898-G | https://url1.jpg, https://url2.jpg | 1234567 |

- **SKU** — product code
- **Assets** — one or more image URLs separated by commas
- **Style ID** — folder name (optional, falls back to SKU)
- All images converted to **JPG** automatically
""")

with st.expander("⚙️ Processing Settings", expanded=True):
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        do_resize = st.toggle("Resize to 1080×1440", value=True,
                              help="1:1 → scale to 1080x1080 + white padding. 3:4 → scale directly.")
    with col2:
        do_size   = st.toggle("Adjust size (500KB–2MB)", value=True,
                              help="Compress if >2MB. Boost if <500KB.")
    with col3:
        min_mb    = st.number_input("Min size (MB)", value=0.5, step=0.1, min_value=0.1)
    with col4:
        max_mb    = st.number_input("Max size (MB)", value=2.0, step=0.1, min_value=0.5)

st.divider()

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
    st.dataframe(preview_df, width='stretch')

    st.divider()

    if st.button("⬇️ Start Processing & Build ZIP", type="primary", width='stretch'):

        zip_buffer    = io.BytesIO()
        progress_bar  = st.progress(0, text="Starting...")
        log_rows      = []
        total_done    = 0

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
                                         'Original MB': 0, 'Final MB': 0, 'Original Dim': '-',
                                         'Scaled Dim': '-', 'Final Dim': '-', 'Ratio': '-',
                                         'Size Action': '-'})
                        total_done += 1
                        progress_bar.progress(total_done / total_images,
                                              text=f"{total_done}/{total_images} processed")
                        continue

                    raw_mb = len(content) / (1024 * 1024)
                    img    = Image.open(io.BytesIO(content))
                    img    = to_rgb(img)

                    # Step 1: Resize
                    if do_resize:
                        img, dim_orig, dim_scaled, dim_final, ratio, err = resize_to_1080x1440(img)
                        if err:
                            with log_container:
                                st.warning(f"⚠️ [{rank}] `{filename}` → {err} — skipped")
                            log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                             'URL': url, 'Status': err,
                                             'Action': 'skipped-bad-ratio',
                                             'Original MB': round(raw_mb, 2), 'Final MB': 0,
                                             'Original Dim': dim_orig, 'Scaled Dim': '-',
                                             'Final Dim': '-', 'Ratio': ratio,
                                             'Size Action': '-'})
                            total_done += 1
                            progress_bar.progress(total_done / total_images,
                                                  text=f"{total_done}/{total_images} processed")
                            del content
                            gc.collect()
                            continue
                    else:
                        dim_orig   = f'{img.width}x{img.height}'
                        dim_scaled = dim_orig
                        dim_final  = dim_orig
                        ratio      = detect_ratio(img)

                    # Step 2: Adjust size
                    if do_size:
                        final_bytes, final_mb, size_action = adjust_size_to_range(img, min_mb, max_mb)
                    else:
                        buf = io.BytesIO()
                        img.save(buf, format='JPEG', quality=95, optimize=True)
                        final_bytes = buf.getvalue()
                        final_mb    = round(len(final_bytes) / (1024*1024), 2)
                        size_action = 'skipped'

                    zf.writestr(zip_path, final_bytes)

                    size_tag = get_size_tag(size_action)
                    status   = 'rejected' if size_action == 'REJECTED-below-500kb' else 'ok'

                    with log_container:
                        ca, cb, cc = st.columns([1, 3, 2])
                        ca.markdown(f"{'❌' if status == 'rejected' else '✅'} `[{rank}] {filename}`")
                        cb.markdown(f"📐 `{dim_orig}` ({ratio}) → `{dim_scaled}` → `{dim_final}`")
                        cc.markdown(f"💾 `{raw_mb:.2f}MB` → `{final_mb:.2f}MB` | {size_tag}")

                    log_rows.append({'SKU': sku, 'Style ID': style_id, 'File': filename,
                                     'URL': url, 'Status': status, 'Action': 'resized',
                                     'Original MB': round(raw_mb, 2), 'Final MB': final_mb,
                                     'Original Dim': dim_orig, 'Scaled Dim': dim_scaled,
                                     'Final Dim': dim_final, 'Ratio': ratio,
                                     'Size Action': size_action})

                    # ── Free memory after each image ──────────────────────────
                    del content, img, final_bytes
                    gc.collect()

                    total_done += 1
                    progress_bar.progress(
                        total_done / total_images,
                        text=f"{total_done}/{total_images} images processed"
                    )

        zip_buffer.seek(0)
        progress_bar.empty()

        st.session_state.zip_buffer = zip_buffer.getvalue()
        st.session_state.log_df     = pd.DataFrame(log_rows)
        st.session_state.done       = True
        st.rerun()
