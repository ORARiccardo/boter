"""
Scans each webpage URL listed in urls.txt, finds all links pointing to PDFs,
downloads any new ones, and extracts their text.

Layout produced:
  pdfs/<original-name>__<hash>.pdf        - the downloaded PDF, named after its
                                             real title where possible
  extracted/<original-name>__<hash>.txt   - extracted text
  extracted/<hash>.meta.txt               - source webpage, PDF URL, and the
                                             detected original filename
  manifest.csv                            - a single running index of every
                                             PDF ever downloaded, for a quick
                                             overview without opening each file

The <hash> suffix (short MD5 of the PDF's URL) guarantees uniqueness even if
two PDFs happen to share a display name, and is also used internally to
detect whether a link has already been processed, independent of renames.
"""

import csv
import hashlib
import os
import re
import subprocess
import sys
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URLS_FILE = "urls.txt"
PDF_DIR = "pdfs"
EXTRACTED_DIR = "extracted"
MANIFEST_PATH = "manifest.csv"
REQUEST_TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=3,  # waits 3s, 6s between retries
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


def hash_for(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def sanitize_filename(name: str, fallback: str) -> str:
    """Strips a candidate filename down to something safe to write to disk."""
    name = (name or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = re.sub(r"[^\w\-. ]+", "_", name).strip("_ ")
    name = re.sub(r"\s+", "_", name)
    if not name:
        return fallback
    return name[:120]  # keep filenames from getting unreasonably long


DOWNLOAD_KEYWORDS = ["download", "pdf", "document", "file", "minutes", "report", "attachment"]


def looks_like_document_link(tag) -> bool:
    """Heuristic pre-filter: does this link look document-related at all,
    based on its class/id/aria attributes or visible text? This avoids
    sending a network request for every single link on the page."""
    class_attr = tag.get("class") or []
    if isinstance(class_attr, list):
        class_attr = " ".join(class_attr)

    haystack = " ".join([
        class_attr,
        tag.get("id", "") or "",
        tag.get("aria-labelledby", "") or "",
        tag.get("title", "") or "",
        tag.get_text() or "",
    ]).lower()

    return any(keyword in haystack for keyword in DOWNLOAD_KEYWORDS)


def is_pdf_content_type(url: str) -> bool:
    """Confirms via the actual HTTP response whether a link serves a PDF.
    Used as a last resort for links with no textual .pdf indicator."""
    try:
        resp = SESSION.head(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "")
        if not content_type or resp.status_code >= 400:
            resp = SESSION.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
            content_type = resp.headers.get("Content-Type", "")
            resp.close()
        return "application/pdf" in content_type.lower()
    except requests.RequestException:
        return False


def find_pdf_links(page_url: str) -> dict[str, str]:
    """Returns {absolute_pdf_url: name_hint}. name_hint is the best guess at
    the PDF's real name based on the link's title/text, or '' if unknown
    (in which case we'll try Content-Disposition or the URL at download time)."""
    resp = SESSION.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_links = {}
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path

        title = (tag.get("title") or "").strip()
        text = tag.get_text().strip()

        # Case 1: URL itself ends in .pdf (the simple, common case)
        if path.lower().endswith(".pdf"):
            pdf_links[absolute] = unquote(os.path.basename(path))
            continue

        # Case 2: opaque URL, but the real filename shows up in the title or link text
        if title.lower().endswith(".pdf"):
            pdf_links[absolute] = title
            continue
        if ".pdf" in text.lower():
            pdf_links[absolute] = text
            continue

        # Case 3: no textual clue at all - only worth the extra request if it
        # at least looks document-related based on class/id/text
        if looks_like_document_link(tag):
            print(f"    Checking possible document link: {absolute}")
            if is_pdf_content_type(absolute):
                # use whatever link text exists as a rough name hint, even if
                # it didn't mention .pdf explicitly (e.g. "Download")
                pdf_links[absolute] = title or text or ""

    return pdf_links


def name_from_content_disposition(resp: requests.Response) -> str:
    disposition = resp.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\n]+)"?', disposition)
    return unquote(match.group(1)) if match else ""


def download_pdf(pdf_url: str):
    try:
        resp = SESSION.get(pdf_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  Failed to download {pdf_url}: {exc}", file=sys.stderr)
        return None, ""

    content_type = resp.headers.get("Content-Type", "")
    if "application/pdf" not in content_type and not resp.content.startswith(b"%PDF"):
        print(f"  Skipping, not a PDF: {pdf_url}", file=sys.stderr)
        return None, ""

    server_name = name_from_content_disposition(resp)
    return resp.content, server_name


def extract_text(pdf_path: str, txt_path: str) -> bool:
    result = subprocess.run(["pdftotext", pdf_path, txt_path], capture_output=True)
    return result.returncode == 0


def append_to_manifest(row: dict):
    file_exists = os.path.exists(MANIFEST_PATH)
    with open(MANIFEST_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["original_name", "pdf_file", "text_file", "source_page", "pdf_url"])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


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

        for pdf_url, name_hint in pdf_links.items():
            file_hash = hash_for(pdf_url)
            meta_path = os.path.join(EXTRACTED_DIR, f"{file_hash}.meta.txt")

            if os.path.exists(meta_path):
                print(f"    Already processed: {pdf_url}")
                continue

            print(f"    Downloading: {pdf_url}")
            content, server_name = download_pdf(pdf_url)
            if content is None:
                continue

            # Prefer, in order: name hint from the page, name from the
            # server's Content-Disposition header, then just the hash.
            best_name = name_hint or server_name or ""
            display_name = sanitize_filename(best_name, fallback=file_hash)

            base_filename = f"{display_name}__{file_hash}"
            pdf_path = os.path.join(PDF_DIR, f"{base_filename}.pdf")
            txt_path = os.path.join(EXTRACTED_DIR, f"{base_filename}.txt")

            with open(pdf_path, "wb") as f:
                f.write(content)

            if extract_text(pdf_path, txt_path):
                with open(meta_path, "w") as f:
                    f.write(
                        f"original_name: {best_name or '(unknown, used hash)'}\n"
                        f"source_page: {page_url}\n"
                        f"pdf_url: {pdf_url}\n"
                        f"pdf_file: {pdf_path}\n"
                        f"text_file: {txt_path}\n"
                    )
                append_to_manifest({
                    "original_name": best_name or "(unknown)",
                    "pdf_file": pdf_path,
                    "text_file": txt_path,
                    "source_page": page_url,
                    "pdf_url": pdf_url,
                })
                print(f"    Saved -> {pdf_path}")
            else:
                print(f"    Text extraction failed for: {pdf_url}", file=sys.stderr)


if __name__ == "__main__":
    main()
