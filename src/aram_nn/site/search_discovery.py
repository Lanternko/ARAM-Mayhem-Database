"""Search discovery artifacts derived only from this build's public pages."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML_NS = "http://www.w3.org/1999/xhtml"

# The shell's inline build config carries the data snapshot date; that is the
# only freshness signal a static build actually has.
_BUILD_RE = re.compile(r"window\.__ARAM_BUILD__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
_BUILD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _page_build_date(text: str) -> str:
    match = _BUILD_RE.search(text)
    if not match:
        return ""
    try:
        snapshot = json.loads(match[1])
    except ValueError:
        return ""
    value = snapshot.get("buildDate") if isinstance(snapshot, dict) else None
    if isinstance(value, str) and _BUILD_DATE_RE.match(value):
        return value
    return ""


class _PageHead(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical = ""
        self.excluded = False
        self.alternates: dict[str, str] = {}
        self.lastmod = ""
        self.in_head = True

    def handle_starttag(self, tag: str, attrs) -> None:
        if not self.in_head:
            return
        a = dict(attrs)
        if tag == "meta":
            if a.get("http-equiv", "").lower() == "refresh":
                self.excluded = True
            if a.get("name", "").lower() in {"robots", "googlebot"}:
                directives = a.get("content", "").lower().replace(",", " ").split()
                self.excluded |= bool({"noindex", "none"} & set(directives))
        if tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel:
                self.canonical = a.get("href", "")
            if "alternate" in rel and a.get("hreflang"):
                self.alternates[a["hreflang"]] = a.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False


def write_search_discovery(
    out_dir: Path, pages: list[Path], *, site_url: str,
) -> list[Path]:
    """Emit a sitemap and robots.txt for indexable self-canonical HTML only.

    Explicit build outputs avoid publishing stale/unlisted files from disk.
    Redirect stubs, 404s, private utilities and noncanonical aliases are omitted.

    ``lastmod`` is the data snapshot date the page itself declares in
    ``window.__ARAM_BUILD__`` (``buildDate``, ``YYYY-MM-DD``).  Pages without it
    — about, privacy, redirect stubs — get no ``lastmod`` rather than a made-up
    one: the shell build date is not data freshness, and file mtimes / wall-clock
    time say even less.  A malformed ``buildDate`` is skipped, not coerced.
    """
    parsed = urlsplit(site_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    if parsed.query or parsed.fragment:
        raise ValueError("site_url must not include a query or fragment")
    base = site_url.rstrip("/") + "/"
    root = Path(out_dir).resolve()
    entries: dict[str, _PageHead] = {}
    for page in pages:
        page = Path(page).resolve()
        relative = page.relative_to(root).as_posix()
        if page.suffix != ".html" or not page.is_file():
            continue
        route = relative.removesuffix("index.html") if page.name == "index.html" else relative
        expected = urljoin(base, route)
        text = page.read_text(encoding="utf-8")
        head = _PageHead()
        head.feed(text)
        if not head.excluded and urljoin(expected, head.canonical) == expected and head.canonical:
            head.lastmod = _page_build_date(text)
            entries[expected] = head

    ET.register_namespace("", SITEMAP_NS)
    ET.register_namespace("xhtml", XHTML_NS)
    sitemap = ET.Element(f"{{{SITEMAP_NS}}}urlset")
    for url, head in sorted(entries.items()):
        node = ET.SubElement(sitemap, f"{{{SITEMAP_NS}}}url")
        ET.SubElement(node, f"{{{SITEMAP_NS}}}loc").text = url
        if head.lastmod:
            ET.SubElement(node, f"{{{SITEMAP_NS}}}lastmod").text = head.lastmod
        for lang, href in sorted(head.alternates.items()):
            target = urljoin(url, href)
            # Only advertise real, self-canonical pages with reciprocal links.
            other = entries.get(target)
            if other is not None and any(urljoin(target, h) == url for h in other.alternates.values()):
                ET.SubElement(node, f"{{{XHTML_NS}}}link", {
                    "rel": "alternate", "hreflang": lang, "href": target,
                })
    ET.indent(sitemap, space="  ")
    sitemap_path = root / "sitemap.xml"
    ET.ElementTree(sitemap).write(sitemap_path, encoding="utf-8", xml_declaration=True)
    robots_path = root / "robots.txt"
    robots_path.write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}sitemap.xml\n", encoding="utf-8")
    return [sitemap_path, robots_path]
