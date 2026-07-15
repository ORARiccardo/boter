"""
Scans each webpage URL listed in urls.txt, finds all links pointing to PDFs,
downloads any new ones, and extracts their text.

Layout produced:
  pdfs/<hash>.pdf              - the downloaded PDF
  extracted/<hash>.txt         - extracted text
  extracted/<hash>.meta.txt    - source webpage + original PDF URL, for traceability
"""

import hashlib
import os
import subprocess
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URLS_FILE = "urls.txt"
PDF_DIR = "pdfs"
EXTRACTED_DIR = "extracted"
REQUEST_TIMEOUT = 60
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PDFScraperBot/1.0)"}


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=5,  # waits 5s, 10s, 20s between retries
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


def hash_for(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def find_pdf_links(page_url: str) -> list[str]:
    resp = SESSION.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_links = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path.lower()

        # Case 1: URL itself ends in .pdf (the simple, common case)
        if path.endswith(".pdf"):
            pdf_links.add(absolute)
            continue

        # Case 2: some IR platforms (e.g. NASDAQ/Q4) use opaque URLs like
        # /static-files/<uuid> and only reveal the real filename via the
        # title attribute or link text, e.g. title="...Earnings.pdf"
        title = (tag.get("title") or "").lower()
        text = tag.get_text().lower()
        if title.endswith(".pdf") or ".pdf" in title or ".pdf" in text:
            pdf_links.add(absolute)

    return sorted(pdf_links)


def download_pdf(pdf_url: str):
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  Failed to download {pdf_url}: {exc}", file=sys.stderr)
        return None

    content_type = resp.headers.get("Content-Type", "")
    if "application/pdf" not in content_type and not resp.content.startswith(b"%PDF"):
        print(f"  Skipping, not a PDF: {pdf_url}", file=sys.stderr)
        return None

    return resp.content


def extract_text(pdf_path: str, txt_path: str) -> bool:
    result = subprocess.run(["pdftotext", pdf_path, txt_path], capture_output=True)
    return result.returncode == 0


def main():
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(EXTRACTED_DIR, exist_ok=True)

    if not os.path.exists(URLS_FILE):
        print(f"{URLS_FILE} not found, nothing to do.")
        return

    with open(URLS_FILE, "r") as f:
        page_urls = [line.strip() for line in f if line.strip()]

    for page_url in page_urls:
        print(f"Scanning page: {page_url}")
        try:
            pdf_links = find_pdf_links(page_url)
        except requests.RequestException as exc:
            print(f"  Failed to fetch page: {exc}", file=sys.stderr)
            continue

        print(f"  Found {len(pdf_links)} PDF link(s)")

        for pdf_url in pdf_links:
            file_hash = hash_for(pdf_url)
            pdf_path = os.path.join(PDF_DIR, f"{file_hash}.pdf")
            txt_path = os.path.join(EXTRACTED_DIR, f"{file_hash}.txt")
            meta_path = os.path.join(EXTRACTED_DIR, f"{file_hash}.meta.txt")

            if os.path.exists(txt_path):
                print(f"    Already processed: {pdf_url}")
                continue

            print(f"    Downloading: {pdf_url}")
            content = download_pdf(pdf_url)
            if content is None:
                continue

            with open(pdf_path, "wb") as f:
                f.write(content)

            if extract_text(pdf_path, txt_path):
                with open(meta_path, "w") as f:
                    f.write(f"source_page: {page_url}\npdf_url: {pdf_url}\n")
                print(f"    Extracted text -> {txt_path}")
            else:
                print(f"    Text extraction failed for: {pdf_url}", file=sys.stderr)


if __name__ == "__main__":
    main()
