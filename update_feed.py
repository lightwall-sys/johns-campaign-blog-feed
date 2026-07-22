#!/usr/bin/env python3
"""Build a validated JSON mirror of recent John's Campaign blog posts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag, UnicodeDammit
from dateutil import parser as date_parser
from PIL import Image

SOURCE_ORIGIN = "https://johnscampaign.org.uk"
BLOG_URL = f"{SOURCE_ORIGIN}/blog/"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs"
FEED_PATH = OUTPUT_DIR / "feed.json"
STATUS_PATH = OUTPUT_DIR / "status.json"
CHANGE_FLAG_PATH = Path(__file__).resolve().parents[1] / ".feed_changed"
MAX_POSTS = 6
MIN_POSTS = 3
HTTP_TIMEOUT = 25
MAX_IMAGE_BYTES = 8 * 1024 * 1024
USER_AGENT = "JohnsCampaignFeedMirror/1.1"

POST_PATH_RE = re.compile(r"^/post/[^/?#]+/?$", re.I)
DATE_LINE_RE = re.compile(
    r"(?P<author>[^|\n]{2,140}?)\s*\|\s*(?P<date>"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"[a-z]*\s+\d{1,2},\s+\d{4})",
    re.I,
)
GENERIC_IMAGE_RE = re.compile(
    r"(?:^|[/_.-])(logo|favicon|icon|placeholder|default|sprite|share|social|"
    r"logo-square|site-logo)(?:[/_.-]|$)",
    re.I,
)
BAD_TITLE_RE = re.compile(r"^(read more|blog|john'?s campaign|home|previous|next)$", re.I)
MOJIBAKE_RE = re.compile(r"(?:[\u0080-\u009f]|â[\u0080-\u00bf]|Â[\u0080-\u00bf]|Ã[\u0080-\u00bf]|ï¿½|\ufffd)")


@dataclass
class ImageData:
    url: str = ""
    alt: str = ""
    width: int | None = None
    height: int | None = None

    def as_dict(self) -> dict[str, Any] | None:
        if not self.url:
            return None
        return {
            "url": self.url,
            "alt": self.alt,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class Post:
    title: str = ""
    url: str = ""
    author: str = ""
    date: str = ""
    excerpt: str = ""
    image: ImageData = field(default_factory=ImageData)
    tags: list[str] = field(default_factory=list)
    method: set[str] = field(default_factory=set)

    def merge(self, other: "Post") -> None:
        for attr in ("title", "author", "date", "excerpt"):
            current = getattr(self, attr)
            incoming = getattr(other, attr)
            if not incoming or contains_mojibake(incoming):
                continue
            if not current or contains_mojibake(current) or len(incoming) > len(current):
                setattr(self, attr, incoming)
        if other.url:
            self.url = other.url
        if other.image.url and (not self.image.url or image_score(other.image) > image_score(self.image)):
            self.image = other.image
        self.tags = sorted(set(self.tags).union(other.tags))
        self.method.update(other.method)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "date": self.date,
            "excerpt": self.excerpt,
            "image": self.image.as_dict(),
            "tags": self.tags,
        }


class FeedError(RuntimeError):
    pass




def decode_html(content: bytes | str) -> str:
    """Decode HTML without trusting an incorrect HTTP charset guess."""
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = UnicodeDammit(content, is_html=True).unicode_markup
        if decoded is None:
            raise FeedError("Unable to decode HTML response.")
        return decoded


def html_soup(content: bytes | str) -> BeautifulSoup:
    return BeautifulSoup(decode_html(content), "html.parser")


def contains_mojibake(value: str) -> bool:
    return bool(value and MOJIBAKE_RE.search(value))

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    raw = str(value)
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True) if "<" in raw or "&" in raw else raw
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return shortened + "…"
    return text


def canonicalise_url(url: str, base: str = SOURCE_ORIGIN) -> str:
    absolute = urljoin(base, url.strip())
    parsed = urlparse(absolute)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != urlparse(SOURCE_ORIGIN).netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if POST_PATH_RE.match(path) and not path.endswith("/"):
        path += "/"
    return urlunparse(("https", host, path, "", "", ""))


def is_post_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.") == urlparse(SOURCE_ORIGIN).netloc and bool(
        POST_PATH_RE.match(parsed.path)
    )


def normalise_date(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = date_parser.parse(str(value), fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    current = utc_now().date()
    # Allow up to 48 hours of clock skew for future-dated publication systems.
    if dt.date().year < 2000 or (dt.date() - current).days > 2:
        return ""
    return dt.date().isoformat()


def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def same_source(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host == urlparse(SOURCE_ORIGIN).netloc


def resolve_image_url(value: str, base: str = SOURCE_ORIGIN) -> str:
    if not value:
        return ""
    value = value.strip().split()[0]
    url = urljoin(base, value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if GENERIC_IMAGE_RE.search(parsed.path.lower()):
        return ""
    return urlunparse(("https", parsed.netloc, parsed.path, "", parsed.query, ""))


def image_score(image: ImageData) -> int:
    score = 0
    if image.url:
        score += 20
    if same_source(image.url):
        score += 10
    if image.width and image.height:
        score += min(20, int((image.width * image.height) / 100_000))
    if image.alt:
        score += 3
    return score


def first_src_from_img(img: Tag, base: str) -> str:
    for attr in ("src", "data-src", "data-lazy-src"):
        value = img.get(attr)
        if value:
            resolved = resolve_image_url(str(value), base)
            if resolved:
                return resolved
    for attr in ("srcset", "data-srcset"):
        value = img.get(attr)
        if not value:
            continue
        candidates = [part.strip().split()[0] for part in str(value).split(",") if part.strip()]
        for candidate in reversed(candidates):
            resolved = resolve_image_url(candidate, base)
            if resolved:
                return resolved
    return ""


def image_from_tag(img: Tag | None, base: str) -> ImageData:
    if not img:
        return ImageData()
    url = first_src_from_img(img, base)
    if not url:
        return ImageData()
    width = int(img.get("width")) if str(img.get("width", "")).isdigit() else None
    height = int(img.get("height")) if str(img.get("height", "")).isdigit() else None
    return ImageData(url=url, alt=clean_text(img.get("alt", ""), 180), width=width, height=height)


def image_from_value(value: Any, base: str) -> ImageData:
    if isinstance(value, str):
        return ImageData(url=resolve_image_url(value, base))
    if isinstance(value, list):
        images = [image_from_value(item, base) for item in value]
        images = [item for item in images if item.url]
        return max(images, key=image_score, default=ImageData())
    if isinstance(value, dict):
        url = value.get("url") or value.get("contentUrl") or value.get("@id") or ""
        image = ImageData(
            url=resolve_image_url(str(url), base),
            alt=clean_text(value.get("caption") or value.get("name") or "", 180),
        )
        for attr in ("width", "height"):
            raw = value.get(attr)
            if isinstance(raw, dict):
                raw = raw.get("value")
            if str(raw or "").isdigit():
                setattr(image, attr, int(raw))
        return image
    return ImageData()


def iter_json_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def post_from_jsonld(node: dict[str, Any], base: str) -> Post | None:
    node_type = node.get("@type", "")
    types = {str(item).lower() for item in (node_type if isinstance(node_type, list) else [node_type])}
    if not types.intersection({"blogposting", "article", "newsarticle", "socialmediaposting"}):
        return None
    raw_url = node.get("url") or node.get("mainEntityOfPage") or node.get("@id") or ""
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("@id") or raw_url.get("url") or ""
    url = canonicalise_url(str(raw_url), base)
    if not is_post_url(url):
        return None
    author = node.get("author") or ""
    if isinstance(author, list):
        author = ", ".join(clean_text(item.get("name") if isinstance(item, dict) else item) for item in author)
    elif isinstance(author, dict):
        author = author.get("name") or ""
    keywords = node.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [item.strip() for item in re.split(r"[,;]", keywords) if item.strip()]
    return Post(
        title=clean_text(node.get("headline") or node.get("name"), 280),
        url=url,
        author=clean_text(author, 160),
        date=normalise_date(node.get("datePublished") or node.get("dateCreated")),
        excerpt=clean_text(node.get("description") or node.get("abstract"), 420),
        image=image_from_value(node.get("image") or node.get("thumbnailUrl"), base),
        tags=[clean_text(item, 80) for item in keywords if clean_text(item, 80)],
        method={"json-ld"},
    )


def parse_jsonld(soup: BeautifulSoup, base: str) -> list[Post]:
    posts: list[Post] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        payload = safe_json_loads(script.string or script.get_text())
        if payload is None:
            continue
        for node in iter_json_nodes(payload):
            post = post_from_jsonld(node, base)
            if post:
                posts.append(post)
    return posts


def heading_post_links(soup: BeautifulSoup, base: str) -> list[tuple[Tag, Tag, str]]:
    """Return primary article heading links in visible listing order."""
    results: list[tuple[Tag, Tag, str]] = []
    seen: set[str] = set()
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        for link in heading.find_all("a", href=True):
            url = canonicalise_url(str(link.get("href", "")), base)
            if not is_post_url(url) or url in seen:
                continue
            seen.add(url)
            results.append((heading, link, url))
            break
    return results


def nearest_card(node: Tag, expected_url: str = "") -> Tag:
    """Find the smallest dated ancestor that represents one listing item."""
    current: Tag = node
    best = node
    for _ in range(10):
        parent = current.parent
        if not isinstance(parent, Tag) or parent.name in {"html", "body"}:
            break
        best = parent
        text = clean_text(parent.get_text(" ", strip=True))
        has_date = bool(DATE_LINE_RE.search(text))
        if has_date:
            if not expected_url:
                return parent
            heading_urls = {
                canonicalise_url(str(a.get("href", "")), SOURCE_ORIGIN)
                for heading in parent.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
                for a in heading.find_all("a", href=True)
            }
            if expected_url in heading_urls:
                return parent
        current = parent
    return best


def extract_title(card: Tag, url: str) -> str:
    for heading in card.find_all(["h1", "h2", "h3", "h4"]):
        link = heading.find("a", href=True)
        if link and canonicalise_url(str(link.get("href")), SOURCE_ORIGIN) == url:
            title = clean_text(heading.get_text(" ", strip=True), 280)
            if title and not BAD_TITLE_RE.match(title):
                return title
    candidates: list[str] = []
    for link in card.find_all("a", href=True):
        if canonicalise_url(str(link.get("href")), SOURCE_ORIGIN) != url:
            continue
        text = clean_text(link.get_text(" ", strip=True), 280)
        if text and not BAD_TITLE_RE.match(text) and len(text) >= 8:
            candidates.append(text)
    return max(candidates, key=len, default="")


def extract_author_date(card: Tag) -> tuple[str, str]:
    matches: list[tuple[int, str, str]] = []
    for element in card.find_all(["p", "time", "small", "span", "div"]):
        text = clean_text(element.get_text(" ", strip=True))
        if "|" not in text or len(text) > 240:
            continue
        match = DATE_LINE_RE.search(text)
        if match:
            matches.append((len(text), clean_text(match.group("author"), 160), normalise_date(match.group("date"))))
    if matches:
        _, author, date_value = min(matches, key=lambda item: item[0])
        return author, date_value
    text = clean_text(card.get_text(" ", strip=True))
    match = DATE_LINE_RE.search(text)
    if match:
        return clean_text(match.group("author"), 160), normalise_date(match.group("date"))
    return "", ""


def extract_excerpt(card: Tag, title: str, author: str, date_value: str) -> str:
    paragraphs = []
    for element in card.find_all(["p", "div"]):
        if element.find_parent(["nav", "header", "footer"]):
            continue
        text = clean_text(element.get_text(" ", strip=True), 420)
        if not text or len(text) < 35:
            continue
        lowered = text.lower()
        if title and title.lower() in lowered:
            continue
        if author and author.lower() in lowered and "|" in text:
            continue
        if date_value and date_value in text:
            continue
        if lowered.startswith("read more"):
            continue
        paragraphs.append(text)
    return max(paragraphs, key=len, default="")


def post_from_listing_card(card: Tag, url: str, base: str, heading: Tag | None = None) -> Post:
    author, date_value = extract_author_date(card)
    title = clean_text(heading.get_text(" ", strip=True), 280) if heading else extract_title(card, url)
    if not title:
        title = extract_title(card, url)
    image = ImageData()
    for img in card.find_all("img"):
        candidate = image_from_tag(img, base)
        if candidate.url and image_score(candidate) > image_score(image):
            image = candidate
    tags: list[str] = []
    for tag_link in card.find_all("a", href=True):
        href = str(tag_link.get("href", ""))
        if "/tag/" in href or "?tag=" in href:
            tag = clean_text(tag_link.get_text(" ", strip=True), 80)
            if tag:
                tags.append(tag)
    return Post(
        title=title,
        url=url,
        author=author,
        date=date_value,
        excerpt=extract_excerpt(card, title, author, date_value),
        image=image,
        tags=tags,
        method={"blog-listing"},
    )


def parse_listing_cards(soup: BeautifulSoup, base: str) -> list[Post]:
    by_url: dict[str, Post] = {}

    # Primary path: use heading links, which remain unambiguous even when a card
    # also links to a related series or another article.
    for heading, link, url in heading_post_links(soup, base):
        card = nearest_card(heading, url)
        candidate = post_from_listing_card(card, url, base, heading)
        if url not in by_url:
            by_url[url] = candidate
        else:
            by_url[url].merge(candidate)

    # Secondary path only when the page does not expose enough heading links.
    # This avoids treating related-series links inside a valid card as articles.
    if len(by_url) < MIN_POSTS:
        for link in soup.find_all("a", href=True):
            url = canonicalise_url(str(link.get("href", "")), base)
            if not is_post_url(url) or url in by_url:
                continue
            card = nearest_card(link, url)
            candidate = post_from_listing_card(card, url, base)
            by_url[url] = candidate

    return list(by_url.values())


def listing_order(soup: BeautifulSoup, base: str) -> list[str]:
    return [url for _, _, url in heading_post_links(soup, base)]


def discover_feed_urls(soup: BeautifulSoup, base: str) -> list[str]:
    urls: list[str] = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])) if isinstance(link.get("rel"), list) else str(link.get("rel", ""))
        mime = str(link.get("type", "")).lower()
        if "alternate" in rel.lower() and any(token in mime for token in ("rss", "atom", "json", "xml")):
            urls.append(urljoin(base, str(link.get("href"))))
    urls.extend(
        urljoin(SOURCE_ORIGIN, path)
        for path in (
            "/feed.json",
            "/blog/feed.json",
            "/feed.xml",
            "/blog/feed.xml",
            "/rss.xml",
            "/blog/rss.xml",
            "/atom.xml",
        )
    )
    return list(dict.fromkeys(urls))


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        if _xml_local_name(child.tag) in wanted and child.text:
            return clean_text(child.text)
    return ""


def parse_feed_content(content: bytes, feed_url: str) -> list[Post]:
    posts: list[Post] = []
    text = content.decode("utf-8", errors="replace").lstrip()
    if text.startswith("{") or feed_url.endswith(".json"):
        payload = safe_json_loads(text)
        items = []
        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("posts") or []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = canonicalise_url(str(item.get("url") or item.get("external_url") or ""), feed_url)
            if not is_post_url(url):
                continue
            author = item.get("author") or item.get("authors") or ""
            if isinstance(author, list):
                author = ", ".join(clean_text(a.get("name") if isinstance(a, dict) else a) for a in author)
            elif isinstance(author, dict):
                author = author.get("name") or ""
            posts.append(
                Post(
                    title=clean_text(item.get("title"), 280),
                    url=url,
                    author=clean_text(author, 160),
                    date=normalise_date(item.get("date_published") or item.get("date_modified")),
                    excerpt=clean_text(item.get("summary") or item.get("content_text") or item.get("content_html"), 420),
                    image=image_from_value(item.get("image") or item.get("banner_image"), feed_url),
                    tags=[clean_text(tag, 80) for tag in item.get("tags", []) if clean_text(tag, 80)],
                    method={"declared-feed"},
                )
            )
        return posts

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return posts

    entries = [node for node in root.iter() if _xml_local_name(node.tag) in {"item", "entry"}]
    for entry in entries:
        raw_url = ""
        for child in entry:
            name = _xml_local_name(child.tag)
            if name == "link":
                raw_url = child.attrib.get("href") or clean_text(child.text)
                rel = child.attrib.get("rel", "alternate")
                if rel not in {"", "alternate"}:
                    raw_url = ""
                    continue
                if raw_url:
                    break
            if name in {"guid", "id"} and not raw_url:
                raw_url = clean_text(child.text)
        url = canonicalise_url(raw_url, feed_url)
        if not is_post_url(url):
            continue

        image = ImageData()
        tags: list[str] = []
        for child in entry.iter():
            name = _xml_local_name(child.tag)
            if name in {"content", "thumbnail", "enclosure"}:
                candidate_url = child.attrib.get("url") or child.attrib.get("href") or ""
                candidate = image_from_value({
                    "url": candidate_url,
                    "width": child.attrib.get("width"),
                    "height": child.attrib.get("height"),
                }, feed_url)
                if candidate.url and image_score(candidate) > image_score(image):
                    image = candidate
            elif name == "category":
                tag = clean_text(child.attrib.get("term") or child.text, 80)
                if tag:
                    tags.append(tag)

        description = _xml_child_text(entry, "description", "summary", "content", "encoded")
        if not image.url and description:
            fragment = BeautifulSoup(description, "html.parser")
            image = image_from_tag(fragment.find("img"), feed_url)

        author = _xml_child_text(entry, "creator", "author", "name")
        posts.append(
            Post(
                title=_xml_child_text(entry, "title")[:280],
                url=url,
                author=author[:160],
                date=normalise_date(_xml_child_text(entry, "pubdate", "published", "updated", "date")),
                excerpt=clean_text(description, 420),
                image=image,
                tags=sorted(set(tags)),
                method={"declared-feed"},
            )
        )
    return posts


def meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))
    return ""


def article_from_html(html: bytes | str, requested_url: str) -> Post:
    soup = html_soup(html)
    json_posts = parse_jsonld(soup, requested_url)
    post = json_posts[0] if json_posts else Post(url=requested_url, method={"article-page"})
    post.method.add("article-page")

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical_url = canonicalise_url(str(canonical.get("href", "")), requested_url) if canonical else ""
    if is_post_url(canonical_url):
        post.url = canonical_url
    elif not post.url:
        post.url = requested_url

    if not post.title:
        post.title = clean_text(meta_content(soup, "og:title", "twitter:title"), 280)
    if not post.title:
        heading = soup.find("h1")
        post.title = clean_text(heading.get_text(" ", strip=True), 280) if heading else ""

    if not post.author:
        post.author = clean_text(meta_content(soup, "author", "article:author"), 160)

    if not post.date:
        post.date = normalise_date(meta_content(soup, "article:published_time", "date", "datePublished"))
    if not post.date:
        time_tag = soup.find("time")
        post.date = normalise_date(time_tag.get("datetime") or time_tag.get_text(" ", strip=True)) if time_tag else ""

    if not post.excerpt:
        post.excerpt = clean_text(meta_content(soup, "og:description", "description", "twitter:description"), 420)
    if not post.excerpt:
        root = soup.find("article") or soup.find("main") or soup
        for paragraph in root.find_all("p"):
            text = clean_text(paragraph.get_text(" ", strip=True), 420)
            if len(text) >= 60:
                post.excerpt = text
                break

    if not post.image.url:
        post.image = image_from_value(meta_content(soup, "og:image", "twitter:image"), requested_url)
    if not post.image.url:
        root = soup.find("article") or soup.find("main") or soup
        for img in root.find_all("img"):
            candidate = image_from_tag(img, requested_url)
            if candidate.url and image_score(candidate) > image_score(post.image):
                post.image = candidate
    if post.image.url and not post.image.alt:
        for img in soup.find_all("img"):
            if first_src_from_img(img, requested_url) == post.image.url:
                post.image.alt = clean_text(img.get("alt", ""), 180)
                break

    tags = []
    for tag in soup.find_all("meta", attrs={"property": "article:tag"}):
        if tag.get("content"):
            tags.append(clean_text(tag.get("content"), 80))
    post.tags = sorted(set(post.tags).union(tag for tag in tags if tag))
    return post


def parse_sitemap_document(content: bytes, sitemap_url: str) -> tuple[list[Post], list[str]]:
    posts: list[Post] = []
    child_sitemaps: list[str] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return posts, child_sitemaps

    entries: list[tuple[str, str]] = []
    for node in root:
        name = _xml_local_name(node.tag)
        if name == "sitemap":
            loc = _xml_child_text(node, "loc")
            if loc:
                child_sitemaps.append(urljoin(sitemap_url, loc))
        elif name == "url":
            loc = _xml_child_text(node, "loc")
            lastmod = _xml_child_text(node, "lastmod")
            url = canonicalise_url(loc, sitemap_url)
            if is_post_url(url):
                entries.append((url, normalise_date(lastmod)))

    entries.sort(key=lambda item: item[1] or "", reverse=True)
    for url, date_value in entries[:40]:
        posts.append(Post(url=url, date=date_value, method={"sitemap"}))
    return posts, list(dict.fromkeys(child_sitemaps))



class Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
            }
        )

    def get(self, url: str, *, max_bytes: int | None = None) -> requests.Response:
        response = self.session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        if max_bytes is not None and len(response.content) > max_bytes:
            raise FeedError(f"Response exceeded {max_bytes} bytes: {url}")
        return response


def enrich_image_dimensions(client: Client, image: ImageData) -> None:
    if not image.url or (image.width and image.height):
        return
    try:
        response = client.get(image.url, max_bytes=MAX_IMAGE_BYTES)
        with Image.open(io.BytesIO(response.content)) as opened:
            image.width, image.height = opened.size
    except Exception as exc:  # Image dimensions are optional.
        print(f"Image size unavailable for {image.url}: {exc}", file=sys.stderr)


def discover_sitemaps(client: Client) -> list[str]:
    candidates = [f"{SOURCE_ORIGIN}/sitemap.xml", f"{SOURCE_ORIGIN}/sitemap-index.xml"]
    try:
        robots = decode_html(client.get(f"{SOURCE_ORIGIN}/robots.txt", max_bytes=512_000).content)
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return list(dict.fromkeys(candidates))


def merge_posts(posts: Iterable[Post]) -> dict[str, Post]:
    merged: dict[str, Post] = {}
    for post in posts:
        post.url = canonicalise_url(post.url, SOURCE_ORIGIN)
        if not is_post_url(post.url):
            continue
        if post.url not in merged:
            merged[post.url] = post
        else:
            merged[post.url].merge(post)
    return merged


def validate_posts(posts: list[Post], expected_urls: list[str] | None = None) -> None:
    if len(posts) < MIN_POSTS:
        raise FeedError(f"Only {len(posts)} valid posts were found; at least {MIN_POSTS} are required.")
    seen: set[str] = set()
    for post in posts:
        if post.url in seen:
            raise FeedError(f"Duplicate post URL: {post.url}")
        seen.add(post.url)
        if not is_post_url(post.url):
            raise FeedError(f"Unexpected post URL: {post.url}")
        if not 8 <= len(post.title) <= 280 or BAD_TITLE_RE.match(post.title):
            raise FeedError(f"Invalid title for {post.url}")
        if not post.date:
            raise FeedError(f"Missing publication date for {post.url}")
        if not post.author:
            raise FeedError(f"Missing author for {post.url}")
        if post.image.url and GENERIC_IMAGE_RE.search(urlparse(post.image.url).path.lower()):
            raise FeedError(f"Generic image selected for {post.url}")
        text_fields = [post.title, post.author, post.excerpt, post.image.alt, *post.tags]
        if any(contains_mojibake(value) for value in text_fields):
            raise FeedError(f"Broken character encoding detected for {post.url}")
    dates = [post.date for post in posts]
    if dates != sorted(dates, reverse=True):
        raise FeedError("Posts are not in reverse chronological order.")
    if expected_urls:
        expected = expected_urls[: min(MAX_POSTS, len(expected_urls))]
        actual = [post.url for post in posts[: len(expected)]]
        if actual != expected:
            raise FeedError("Generated feed does not match the visible blog listing order.")


def public_post_signature(posts: list[Post]) -> str:
    payload = [post.as_dict() for post in posts]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def previous_signature() -> str:
    try:
        data = json.loads(FEED_PATH.read_text(encoding="utf-8"))
        posts = data.get("posts", [])
        return hashlib.sha256(json.dumps(posts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    except Exception:
        return ""


def previous_last_success() -> str | None:
    for path in (STATUS_PATH, FEED_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("last_successful_update")
            if value:
                return str(value)
        except Exception:
            continue
    return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_change_flag(changed: bool) -> None:
    CHANGE_FLAG_PATH.write_text("true\n" if changed else "false\n", encoding="utf-8")
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"feed_changed={'true' if changed else 'false'}\n")


def build_feed() -> tuple[list[Post], set[str]]:
    client = Client()
    listing_response = client.get(BLOG_URL, max_bytes=8_000_000)
    listing_soup = html_soup(listing_response.content)
    expected_order = listing_order(listing_soup, listing_response.url)

    collected: list[Post] = []
    collected.extend(parse_jsonld(listing_soup, listing_response.url))
    collected.extend(parse_listing_cards(listing_soup, listing_response.url))

    for feed_url in discover_feed_urls(listing_soup, listing_response.url):
        try:
            response = client.get(feed_url, max_bytes=8_000_000)
            collected.extend(parse_feed_content(response.content, response.url))
        except Exception:
            continue

    merged = merge_posts(collected)

    # Preserve every primary listing URL even if its card metadata was sparse.
    # The article page can then supply the remaining fields.
    for url in expected_order[: max(MAX_POSTS * 2, 12)]:
        if url not in merged:
            merged[url] = Post(url=url, method={"blog-listing"})

    if len(merged) < MIN_POSTS:
        sitemap_queue = discover_sitemaps(client)
        seen_sitemaps: set[str] = set()
        while sitemap_queue and len(seen_sitemaps) < 12 and len(merged) < MIN_POSTS:
            sitemap_url = sitemap_queue.pop(0)
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            try:
                response = client.get(sitemap_url, max_bytes=10_000_000)
                sitemap_posts, children = parse_sitemap_document(response.content, response.url)
                merged = merge_posts([*merged.values(), *sitemap_posts])
                sitemap_queue.extend(child for child in children if child not in seen_sitemaps)
            except Exception:
                continue

    # Enrich visible listing entries first, then any feed or sitemap fallbacks.
    ordered_candidates = [merged[url] for url in expected_order if url in merged]
    remainder = [post for url, post in merged.items() if url not in set(expected_order)]
    remainder.sort(key=lambda post: (post.date or "", post.url), reverse=True)
    candidates = (ordered_candidates + remainder)[: max(MAX_POSTS * 3, 12)]

    enriched: list[Post] = []
    for candidate in candidates:
        try:
            response = client.get(candidate.url, max_bytes=8_000_000)
            article = article_from_html(response.content, response.url)
            candidate.merge(article)
        except Exception as exc:
            print(f"Article enrichment failed for {candidate.url}: {exc}", file=sys.stderr)
        enriched.append(candidate)

    valid_by_url = {post.url: post for post in enriched if post.title and post.date and post.author}
    selected: list[Post] = []
    if expected_order:
        expected_top = expected_order[: min(MAX_POSTS, len(expected_order))]
        missing = [url for url in expected_top if url not in valid_by_url]
        if missing:
            raise FeedError("Could not validate all recent posts shown on the blog listing.")
        selected.extend(valid_by_url[url] for url in expected_top)

    if len(selected) < MAX_POSTS:
        fallback = [post for post in valid_by_url.values() if post.url not in {item.url for item in selected}]
        fallback.sort(key=lambda post: (post.date, post.url), reverse=True)
        selected.extend(fallback[: MAX_POSTS - len(selected)])

    for post in selected:
        post.excerpt = clean_text(post.excerpt, 420)
        post.tags = [tag for tag in post.tags if not contains_mojibake(tag)]
        if contains_mojibake(post.image.alt):
            post.image.alt = ""
        if post.image.url:
            enrich_image_dimensions(client, post.image)
        if not post.image.alt:
            post.image.alt = post.author or post.title

    validate_posts(selected, expected_order)
    methods: set[str] = set()
    for post in selected:
        methods.update(post.method)
    return selected, methods


def main() -> int:
    checked_at = iso_now()
    last_success = previous_last_success()
    old_signature = previous_signature()
    try:
        posts, methods = build_feed()
        new_signature = public_post_signature(posts)
        changed = new_signature != old_signature
        feed = {
            "version": 1,
            "source": {"name": "John's Campaign", "blog_url": BLOG_URL},
            "generated_at": checked_at,
            "last_successful_update": checked_at,
            "post_count": len(posts),
            "posts": [post.as_dict() for post in posts],
        }
        status = {
            "ok": True,
            "stale": False,
            "checked_at": checked_at,
            "last_successful_update": checked_at,
            "post_count": len(posts),
            "methods": sorted(methods),
            "message": "Feed updated successfully.",
        }
        write_json(FEED_PATH, feed)
        write_json(STATUS_PATH, status)
        write_change_flag(changed)
        print(f"Validated {len(posts)} posts. Feed changed: {changed}.")
        return 0
    except Exception as exc:
        status = {
            "ok": False,
            "stale": True,
            "checked_at": checked_at,
            "last_successful_update": last_success,
            "post_count": 0,
            "message": "Update failed; the last valid feed was retained.",
        }
        write_json(STATUS_PATH, status)
        write_change_flag(False)
        print(f"Feed update failed: {exc}", file=sys.stderr)
        print("::warning::Feed update failed; the last valid feed was retained.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
