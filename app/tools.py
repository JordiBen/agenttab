"""The paid resources: small web-intelligence tools an agent can buy per call."""
from __future__ import annotations

import ipaddress
import re
import socket
from decimal import Decimal
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = "AgentTab/1.0 (+https://github.com/JordiBen/agenttab)"

PRICES = {
    "page_markdown": Decimal("12.00"),
    "tech_detect": Decimal("24.00"),
    "seo_audit": Decimal("49.00"),
}

DESCRIPTIONS = {
    "page_markdown": "Clean, LLM-ready Markdown of a public web page.",
    "tech_detect": "Detect the technology stack (CMS, frameworks, analytics, payments) of a site.",
    "seo_audit": "On-page SEO audit: title, meta, headings, links, images, issues.",
}


class UnsafeTarget(ValueError):
    pass


MAX_BYTES = 2_000_000      # response body cap (streamed; we stop reading after this)
MAX_REDIRECTS = 4


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Only globally routable unicast addresses. Rejects private, loopback, link-local (cloud metadata),
    CGNAT, reserved, multicast, unspecified (0.0.0.0, ::) and IPv4-mapped/compatible forms like ::ffff:127.0.0.1."""
    if ip.version == 6:
        mapped = ip.ipv4_mapped or (ip.sixtofour if ip.sixtofour else None)
        if mapped is not None:
            return ip_is_public(mapped)
        if ip.packed[:12] == bytes(12):   # deprecated IPv4-compatible ::a.b.c.d
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback
                                 or ip.is_link_local or ip.is_private)


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def resolve_public(url: str, allow_private: bool = False) -> tuple[str, str | None]:
    """Validate one URL and return (hostname, ip to connect to). Every resolved address must be public,
    and the caller connects to exactly that address (no second DNS lookup = no DNS-rebinding window)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeTarget("only public http(s) URLs are allowed")
    if u.username or u.password:
        raise UnsafeTarget("credentials in URLs are not allowed")
    host = u.hostname
    if allow_private:
        return host, None
    try:
        addrs = _resolve(host)
    except (socket.gaierror, UnicodeError):
        raise UnsafeTarget("host does not resolve")
    if not addrs:
        raise UnsafeTarget("host does not resolve")
    for a in addrs:
        if not ip_is_public(ipaddress.ip_address(a.split("%")[0])):
            raise UnsafeTarget("private or reserved address blocked")
    return host, addrs[0]


def check_url(url: str, allow_private: bool = False) -> str:
    resolve_public(url, allow_private)
    return url


class FetchResult:
    """The bits of an HTTP response the tools use (url, headers, text)."""
    def __init__(self, url: str, headers: httpx.Headers, body: bytes, encoding: str | None):
        self.url, self.headers = url, headers
        self.text = body.decode(encoding or "utf-8", errors="replace")


def fetch(url: str, allow_private: bool = False) -> tuple[str, FetchResult]:
    """GET with SSRF protection: each redirect hop is re-validated, the connection goes to the validated IP
    (Host header and TLS SNI/certificate check still use the hostname), and the body is capped at 2 MB."""
    for _ in range(MAX_REDIRECTS + 1):
        host, ip = resolve_public(url, allow_private)
        u = urlparse(url)
        req_url, ext, headers = url, {}, {"User-Agent": UA}
        if ip:
            netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{u.port}" if u.port else "")
            req_url = u._replace(netloc=netloc).geturl()
            headers["Host"] = host + (f":{u.port}" if u.port else "")
            if u.scheme == "https":
                ext["sni_hostname"] = host
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as c:
            req = c.build_request("GET", req_url, headers=headers, extensions=ext)
            r = c.send(req, stream=True)
            try:
                if r.is_redirect:
                    loc = r.headers.get("location", "")
                    if not loc:
                        raise UnsafeTarget("redirect without Location")
                    url = urljoin(url, loc)
                    continue
                r.raise_for_status()
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        del body[MAX_BYTES:]
                        break
                res = FetchResult(url, r.headers, bytes(body), r.charset_encoding)
                return res.text, res
            finally:
                r.close()
    raise UnsafeTarget(f"more than {MAX_REDIRECTS} redirects")


def page_markdown(url: str, allow_private: bool = False) -> dict:
    html, r = fetch(url, allow_private)
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer"]):
        t.decompose()
    out: list[str] = []
    title = soup.title.get_text(strip=True) if soup.title else ""
    if title:
        out.append(f"# {title}\n")
    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        if not txt:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            out.append("#" * int(el.name[1]) + " " + txt)
        elif el.name == "li":
            out.append("- " + txt)
        elif el.name == "pre":
            out.append("```\n" + el.get_text() + "\n```")
        elif el.name == "blockquote":
            out.append("> " + txt)
        else:
            out.append(txt)
    md = "\n\n".join(out)
    return {"url": str(r.url), "title": title, "markdown": md[:100_000], "chars": len(md)}


FINGERPRINTS = {
    "WordPress": [r"wp-content/", r"wp-includes/"],
    "Shopify": [r"cdn\.shopify\.com", r"Shopify\.theme"],
    "Wix": [r"static\.wixstatic\.com"],
    "Squarespace": [r"static1\.squarespace\.com"],
    "Webflow": [r"webflow\.js|data-wf-page"],
    "Next.js": [r"/_next/static/", r"__NEXT_DATA__"],
    "Nuxt": [r"/_nuxt/", r"__NUXT__"],
    "React": [r"data-reactroot|react-dom"],
    "Vue.js": [r"data-v-[0-9a-f]{8}|vue(\.min)?\.js"],
    "Angular": [r"ng-version="],
    "Gatsby": [r"___gatsby"],
    "Tailwind CSS": [r"tailwind"],
    "Bootstrap": [r"bootstrap(\.min)?\.(css|js)"],
    "jQuery": [r"jquery(\.min)?\.js"],
    "Google Analytics": [r"googletagmanager\.com/gtag|google-analytics\.com"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js"],
    "Meta Pixel": [r"connect\.facebook\.net/.+/fbevents\.js"],
    "Hotjar": [r"static\.hotjar\.com"],
    "HubSpot": [r"js\.hs-scripts\.com|hs-analytics"],
    "Intercom": [r"widget\.intercom\.io"],
    "PayPal": [r"paypal\.com/sdk/js|paypalobjects\.com"],
    "Stripe": [r"js\.stripe\.com"],
    "Cloudflare": [r"cdnjs\.cloudflare\.com|cf-ray"],
}
HEADER_FP = {"Cloudflare": ("server", "cloudflare"), "Vercel": ("server", "vercel"),
             "Netlify": ("server", "netlify"), "nginx": ("server", "nginx"), "Apache": ("server", "apache")}


def tech_detect(url: str, allow_private: bool = False) -> dict:
    html, r = fetch(url, allow_private)
    found = {name for name, pats in FINGERPRINTS.items() if any(re.search(p, html, re.I) for p in pats)}
    for name, (h, v) in HEADER_FP.items():
        if v in r.headers.get(h, "").lower():
            found.add(name)
    gen = BeautifulSoup(html, "html.parser").find("meta", attrs={"name": "generator"})
    return {"url": str(r.url), "technologies": sorted(found),
            "generator": gen.get("content") if gen else None}


def seo_audit(url: str, allow_private: bool = False) -> dict:
    html, r = fetch(url, allow_private)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag.get("content", "") if desc_tag else ""
    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    imgs = soup.find_all("img")
    no_alt = [i.get("src", "") for i in imgs if not i.get("alt")]
    host = urlparse(str(r.url)).hostname
    links = [urljoin(str(r.url), a["href"]) for a in soup.find_all("a", href=True)]
    internal = [l for l in links if urlparse(l).hostname == host]
    issues = []
    if not title:
        issues.append("missing <title>")
    elif not 15 <= len(title) <= 65:
        issues.append(f"title length {len(title)} (aim 15-65)")
    if not desc:
        issues.append("missing meta description")
    elif not 50 <= len(desc) <= 160:
        issues.append(f"meta description length {len(desc)} (aim 50-160)")
    if len(h1) != 1:
        issues.append(f"{len(h1)} <h1> tags (aim 1)")
    if no_alt:
        issues.append(f"{len(no_alt)} images without alt text")
    if not soup.find("link", rel="canonical"):
        issues.append("no canonical link")
    if not soup.find("meta", attrs={"name": "viewport"}):
        issues.append("no viewport meta (mobile)")
    score = max(0, 100 - 12 * len(issues))
    return {"url": str(r.url), "score": score, "title": title, "meta_description": desc, "h1": h1,
            "images": len(imgs), "images_missing_alt": len(no_alt), "links": len(links),
            "internal_links": len(internal), "issues": issues}


TOOLS = {"page_markdown": page_markdown, "tech_detect": tech_detect, "seo_audit": seo_audit}
