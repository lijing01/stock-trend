#!/usr/bin/env python3
"""Fetch web page content and extract readable text.

Replacement for WebFetch when domains are blocked by enterprise security policy
(e.g. zx.sina.cn). Uses urllib + html.parser — no external dependencies.

Usage:
    python3 web_page.py <url> [--encoding gbk] [--timeout 15] [--max-lines 500]
    python3 web_page.py <url> --html  # output raw HTML instead of text
    python3 web_page.py <url> --json  # output {url, title, text, content_length, status}
"""

import argparse
import re
import sys
import urllib.request
import urllib.error
from html.parser import HTMLParser

# Browser-like headers — same pattern as eastmoney_utils.py
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Tags whose content should be stripped entirely
STRIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "canvas", "nav", "footer", "header"}


class TextExtractor(HTMLParser):
    """Extract readable text from HTML, stripping script/style/nav elements."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.current_line = []
        self.strip_depth = 0
        self.skip_tag = None
        self._title = None
        self._in_title = False
        self._in_body = False
        self._seen_text = set()  # crude dedup of repetitive boilerplate

    @property
    def title(self):
        return self._title

    def handle_starttag(self, tag, attrs):
        if tag in STRIP_TAGS:
            self.strip_depth += 1
            self.skip_tag = tag
        elif tag == "title":
            self._in_title = True
        elif tag == "body":
            self._in_body = True
        # Block-level elements: flush current line for paragraph-like breaks
        if tag in {"p", "div", "article", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                   "tr", "blockquote", "pre", "figcaption"}:
            self._flush_line()

    def handle_endtag(self, tag):
        if tag in STRIP_TAGS:
            if self.strip_depth > 0:
                self.strip_depth -= 1
            if self.strip_depth == 0:
                self.skip_tag = None
        elif tag == "title":
            self._in_title = False
        # Block-level end: flush
        if tag in {"p", "div", "article", "section", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                   "tr", "blockquote", "pre"}:
            self._flush_line()
        if tag == "br":
            self._flush_line()

    def handle_data(self, data):
        if self.strip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title = text
        if text in self._seen_text:
            return
        if len(text) < 3 and not text.isascii():
            self._seen_text.add(text)
        self.current_line.append(text)

    def _flush_line(self):
        if self.current_line:
            line = " ".join(self.current_line).strip()
            if line and len(line) > 1:
                self.lines.append(line)
            self.current_line = []

    def get_text(self):
        self._flush_line()
        return "\n".join(self.lines)


def detect_charset(html_bytes, content_type_header):
    """Detect charset from HTTP header or HTML meta tag."""
    # 1. From Content-Type header
    if content_type_header:
        m = re.search(rb'charset\s*=\s*([^\s;]+)', content_type_header.encode("ascii", "ignore")
                      if isinstance(content_type_header, str) else content_type_header)
        if m:
            charset = m.group(1).decode("ascii").strip().lower().replace('"', '').replace("'", "")
            return charset

    # 2. From HTML <meta charset> or <meta http-equiv>
    head = html_bytes[:2048]
    # <meta charset="gbk">
    m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?([^"\';>\s]+)', head, re.IGNORECASE)
    if m:
        charset = m.group(1).decode("ascii", errors="ignore").strip().lower()
        return charset

    # 3. Default
    return "utf-8"


def fetch_page(url, encoding=None, timeout=15):
    """Fetch a web page, return (body_text, content_type, final_url).

    Args:
        url: Full URL to fetch.
        encoding: Force a specific encoding (e.g. 'gbk'). Auto-detected if None.
        timeout: Request timeout in seconds.

    Returns:
        (body_text, content_type_str, final_url_after_redirects)

    Raises:
        urllib.error.URLError on network/HTTP errors.
    """
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    raw = None
    first_error = None
    content_type = None

    # Try with system proxy first, then proxyless (same pattern as eastmoney_utils)
    for use_proxyless in (False, True):
        try:
            if use_proxyless:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, timeout=timeout)

            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.url
            break
        except Exception as e:
            first_error = e
            continue

    if raw is None:
        raise first_error or RuntimeError(f"Failed to fetch {url}")

    # Decompress gzip if needed
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)

    # Detect encoding
    if not encoding:
        encoding = detect_charset(raw, content_type)
    # Normalize
    encoding_map = {"gb2312": "gbk", "gb18030": "gbk", "utf-8-sig": "utf-8", "ansi": "gbk",
                    "iso-8859-1": "latin-1"}
    encoding = encoding_map.get(encoding, encoding)

    try:
        html = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = raw.decode("utf-8", errors="replace")

    return html, content_type, final_url


def extract_text(html):
    """Extract readable text from HTML."""
    extractor = TextExtractor()
    extractor.feed(html)
    return extractor.get_text(), extractor.title


def main():
    parser = argparse.ArgumentParser(description="Fetch web page and extract readable text")
    parser.add_argument("url", help="Page URL to fetch")
    parser.add_argument("--encoding", default=None, help="Force encoding (gbk, utf-8, etc.)")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")
    parser.add_argument("--max-lines", type=int, default=500, help="Max output lines (default 500)")
    parser.add_argument("--html", action="store_true", help="Output raw HTML instead of text")
    parser.add_argument("--json", action="store_true", help="Output JSON with metadata")
    parser.add_argument("--out", "-o", default=None, help="Write to file instead of stdout")
    args = parser.parse_args()

    try:
        html, content_type, final_url = fetch_page(args.url, encoding=args.encoding,
                                                    timeout=args.timeout)
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.html:
        output = html
    else:
        text, title = extract_text(html)
        lines = text.split("\n")
        if args.max_lines and len(lines) > args.max_lines:
            text = "\n".join(lines[:args.max_lines])
            text += f"\n\n[... truncated, {len(lines)} total lines]"

        if args.json:
            import json
            result = {
                "url": args.url,
                "final_url": final_url,
                "title": title,
                "text": text,
                "content_length": len(html),
                "text_lines": len(lines),
                "status": "ok",
            }
            output = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            output = f"# {title or '（无标题）'}\n\n{text}"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved {len(output)} bytes to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
