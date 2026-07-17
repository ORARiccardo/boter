"""
Checks each website URL listed in url_monitor.txt for changes since the last run.

Layout produced:
  snapshots/<hash>.html          - most recent fetched content
  snapshots/<hash>.meta.txt      - which URL this snapshot belongs to
  snapshots/<hash>.diff.txt      - diff from the previous version (only written when changed)

Exits with a list of changed URLs printed to stdout, and writes them to
$GITHUB_OUTPUT as "changed_urls" (newline-joined) for the workflow to use
in a notification step.
"""

import difflib
import hashlib
import os
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URLS_FILE = "url_monitor.txt"
SNAPSHOT_DIR = "snapshots"
REQUEST_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WebsiteMonitorBot/1.0)"}


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


def hash_for(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def fetch(url: str) -> str:
    resp = SESSION.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def main():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    if not os.path.exists(URLS_FILE):
        print(f"{URLS_FILE} not found, nothing to do.")
        return

    with open(URLS_FILE, "r") as f:
        urls = [line.strip() for line in f if line.strip()]

    changed_urls = []

    for url in urls:
        print(f"Checking: {url}")
        file_hash = hash_for(url)
        current_path = os.path.join(SNAPSHOT_DIR, f"{file_hash}.html")
        meta_path = os.path.join(SNAPSHOT_DIR, f"{file_hash}.meta.txt")
        diff_path = os.path.join(SNAPSHOT_DIR, f"{file_hash}.diff.txt")

        try:
            new_content = fetch(url)
        except requests.RequestException as exc:
            print(f"  Failed to fetch: {exc}", file=sys.stderr)
            continue

        previous_content = None
        if os.path.exists(current_path):
            with open(current_path, "r", encoding="utf-8", errors="ignore") as f:
                previous_content = f.read()

        if previous_content is None:
            print("  First check, establishing baseline.")
            with open(meta_path, "w") as f:
                f.write(f"url: {url}\n")
        elif previous_content != new_content:
            print("  Change detected.")
            diff = difflib.unified_diff(
                previous_content.splitlines(),
                new_content.splitlines(),
                fromfile="previous",
                tofile="latest",
                lineterm="",
            )
            with open(diff_path, "w", encoding="utf-8") as f:
                f.write("\n".join(diff))
            changed_urls.append(url)
        else:
            print("  No change.")
            if os.path.exists(diff_path):
                os.remove(diff_path)

        with open(current_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write("changed=" + ("true" if changed_urls else "false") + "\n")
            f.write("changed_urls<<EOF\n")
            f.write("\n".join(changed_urls))
            f.write("\nEOF\n")

    if changed_urls:
        print(f"\n{len(changed_urls)} site(s) changed:")
        for url in changed_urls:
            print(f"  - {url}")
    else:
        print("\nNo sites changed.")


if __name__ == "__main__":
    main()
