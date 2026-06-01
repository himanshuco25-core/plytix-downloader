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

# ── Helpers ──────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()

def convert_to_jpg(content: bytes) -> bytes:
    """Convert any image format to JPG."""
    img = Image.open(io.BytesIO(content))
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA').convert('RGB')
    else:
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, optimize=True)
    return buf.getvalue()

def download_image(url: str, timeout: int = 30):
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content, 'ok'
    except Exception as e:
        return None, f'error: {e}'

# ── UI ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Plytix Asset Downloader", page_icon="📦", layout="centered")

st.title("📦 Plytix Asset Downloader")
st.markdown("Upload your CSV → images download into folders → get a ZIP file.")

with st.expander("📋 Expected CSV format"):
    st.markdown("""
| SKU | Assets | Style ID |
|---|---|---|
| NS14898-G | https://url1.jpg, https://url2.jpg | 1234567 |
| BS14897-G | https://url3.jpg | 1234568 |

- **SKU** — product code  
- **Assets** — one or more image URLs separated by commas  
- **Style ID** — used as the folder name (optional, falls back to SKU)
- All images are converted to **JPG format** automatically
""")

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

    st.success(f"✅ {len(df)} SKUs found | {total_images} images to download")

    preview_df = df[['SKU', 'Style ID']].copy()
    preview_df['Image Count'] = df['url_list'].apply(len)
    st.dataframe(preview_df, use_container_width=True)

    st.divider()

    if st.button("⬇️ Start Download & Build ZIP", type="primary", use_container_width=True):

        zip_buffer   = io.BytesIO()
        progress_bar = st.progress(0, text="Starting...")
        status_text  = st.empty()
        log_rows     = []
        total_done   = 0

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for _, row in df.iterrows():
                style_id = sanitize(str(row['Style ID']).strip())
                sku      = str(row['SKU']).strip()
                urls     = row['url_list']

                for rank, url in enumerate(urls, start=1):
                    filename = f"{rank}.jpg"          # always .jpg
                    zip_path = f"{style_id}/{filename}"

                    status_text.markdown(f"**Downloading:** `{zip_path}`")

                    content, status = download_image(url)

                    if content:
                        try:
                            jpg_content = convert_to_jpg(content)
                            zf.writestr(zip_path, jpg_content)
                        except Exception as e:
                            status = f'error: conversion failed — {e}'

                    log_rows.append({
                        'SKU'     : sku,
                        'Style ID': style_id,
                        'File'    : filename,
                        'URL'     : url,
                        'Status'  : status
                    })

                    total_done += 1
                    progress_bar.progress(
                        total_done / total_images,
                        text=f"{total_done}/{total_images} images processed"
                    )
                    time.sleep(0.05)

        zip_buffer.seek(0)
        status_text.empty()
        progress_bar.empty()

        log_df = pd.DataFrame(log_rows)
        ok     = (log_df['Status'] == 'ok').sum()
        errors = (log_df['Status'] != 'ok').sum()

        col1, col2 = st.columns(2)
        col1.metric("✅ Downloaded", ok)
        col2.metric("❌ Errors", errors)

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
