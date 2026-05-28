# Plytix Asset Downloader

A Streamlit web app that downloads images from Plytix URLs and packages them into a ZIP.

## How it works
1. User uploads a CSV with `SKU`, `Assets`, `Style ID` columns
2. App downloads all images in the background
3. Organises them into folders named by Style ID with files named 1.jpg, 2.jpg etc.
4. User downloads a single ZIP file

## CSV Format
| SKU | Assets | Style ID |
|---|---|---|
| NS14898-G | https://url1.jpg, https://url2.jpg | 1234567 |

## Deploy on Streamlit Community Cloud (Free)

1. Push this repo to GitHub
2. Go to https://share.streamlit.io
3. Click **New app**
4. Select your repo → branch → set `app.py` as the main file
5. Click **Deploy**

Done. Share the URL with your team.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
