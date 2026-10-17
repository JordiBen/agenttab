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
    "page_markdown": Decimal("0.01"),
    "tech_detect": Decimal("0.02"),
    "seo_audit": Decimal("0.03"),
}

DESCRIPTIONS = {
    "page_markdown": "Clean, LLM-ready Markdown of a public web page.",
    "tech_detect": "Detect the technology stack (CMS, frameworks, analytics, payments) of a site.",
    "seo_audit": "On-page SEO audit: title, meta, headings, links, images, issues.",
}


class UnsafeTarget(ValueError):
    pass


def check_url(url: str, allow_private: bool = False) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeTarget("only public http(s) URLs are allowed")
    if allow_private:
        return url
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror:
        raise UnsafeTarget("host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeTarget("private or reserved address blocked")
    return url


def fetch(url: str) -> tuple[str, httpx.Response]:
    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    return r.text[:2_000_000], r


def page_markdown(url: str) -> dict:
    html, r = fetch(url)
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


def tech_detect(url: str) -> dict:
    html, r = fetch(url)
    found = {name for name, pats in FINGERPRINTS.items() if any(re.search(p, html, re.I) for p in pats)}
    for name, (h, v) in HEADER_FP.items():
        if v in r.headers.get(h, "").lower():
            found.add(name)
    gen = BeautifulSoup(html, "html.parser").find("meta", attrs={"name": "generator"})
    return {"url": str(r.url), "technologies": sorted(found),
            "generator": gen.get("content") if gen else None}


def seo_audit(url: str) -> dict:
    html, r = fetch(url)
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
