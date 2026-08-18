import os
import json
import asyncio
import re
import xml.etree.ElementTree as ET

from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from twscrape import API, gather
from playwright.async_api import async_playwright


STATE = Path("official_seen.json")
ENGINE = "official_pao_13_sources_v1"
MAX_SEEN = 10000

TOPIC = os.environ["NTFY_OFFICIAL_TOPIC"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]


# =========================================================
# OFFICIAL SOURCES ONLY
# =========================================================

WEBSITE_SOURCES = [
    {
        "key": "site_pae",
        "org": "PAE",
        "url": "https://www.pao.gr/all-news/",
        "host": "www.pao.gr",
        "article_prefix": None,
    },
    {
        "key": "site_ao",
        "org": "AO",
        "url": "https://www.pao1908.com/category/nea/",
        "host": "www.pao1908.com",
        "article_prefix": None,
    },
    {
    "key": "site_kae",
    "org": "KAE",
    "url": "https://www.paobc.gr/news/",
    "host": "www.paobc.gr",
    "article_prefix": None,
    },
]


YOUTUBE_SOURCES = [
    {
        "key": "youtube_pae",
        "org": "PAE",
        "channel_id": "UCvDGYaeFq9sBdj0cGnZ_Uhg",
    },
    {
        "key": "youtube_kae",
        "org": "KAE",
        "channel_id": "UCbGAOY8tnarNw6T0pHghleg",
    },
]


X_SOURCES = [
    {
        "key": "x_pae",
        "org": "PAE",
        "username": "paofc_",
    },
    {
        "key": "x_kae",
        "org": "KAE",
        "username": "Paobcgr",
    },
    {
        "key": "x_ao",
        "org": "AO",
        "username": "acpanathinaikos",
    },
]


BROWSER_SOURCES = [
    {
        "key": "instagram_pae",
        "org": "PAE",
        "platform": "INSTAGRAM",
        "url": "https://www.instagram.com/fcpanathinaikos/",
    },
    {
        "key": "facebook_pae",
        "org": "PAE",
        "platform": "FACEBOOK",
        "url": "https://www.facebook.com/paofcgr",
    },
    {
        "key": "facebook_kae",
        "org": "KAE",
        "platform": "FACEBOOK",
        "url": "https://www.facebook.com/paobcgr/",
    },
    {
        "key": "instagram_kae",
        "org": "KAE",
        "platform": "INSTAGRAM",
        "url": "https://www.instagram.com/paobcgr/",
    },
    {
        "key": "instagram_ao",
        "org": "AO",
        "platform": "INSTAGRAM",
        "url": "https://www.instagram.com/panathinaikos_1908/",
    },
]


# =========================================================
# STATE / DEDUP
# =========================================================

def load_state():
    try:
        data = json.loads(
            STATE.read_text(
                encoding="utf-8"
            )
        )

        return {
            "engine": data.get("engine"),
            "ids": set(
                str(x)
                for x in data.get(
                    "ids",
                    [],
                )
            ),
            "initialized_sources": set(
                str(x)
                for x in data.get(
                    "initialized_sources",
                    [],
                )
            ),
        }

    except Exception:
        return {
            "engine": None,
            "ids": set(),
            "initialized_sources": set(),
        }


def save_state(
    ids,
    initialized_sources,
):
    ordered = sorted(
        set(
            str(x)
            for x in ids
        )
    )

    STATE.write_text(
        json.dumps(
            {
                "engine": ENGINE,
                "ids": ordered[
                    -MAX_SEEN:
                ],
                "initialized_sources": sorted(
                    initialized_sources
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# =========================================================
# NTFY
# =========================================================

def notify(item):
    endpoint = (
        f"https://ntfy.sh/{TOPIC}"
    )

    headers = {
        "Title": (
            f"OFFICIAL PAO - "
            f"{item['org']} - "
            f"{item['platform']}"
        ),
        "Priority": "high",
        "Tags": "green_circle",
        "Click": item["url"],
    }

    body_parts = []

    if item.get("author"):
        body_parts.append(
            item["author"]
        )

    if item.get("title"):
        body_parts.append(
            item["title"]
        )

    if item.get("text"):
        body_parts.append(
            item["text"]
        )

    body_parts.append(
        item["url"]
    )

    body = "\n".join(
        body_parts
    )

    r = requests.post(
        endpoint,
        data=body.encode(
            "utf-8"
        ),
        headers=headers,
        timeout=20,
    )

    r.raise_for_status()


# =========================================================
# WEBSITE PARSER
# =========================================================

class HeadingParser(
    HTMLParser
):
    def __init__(self):
        super().__init__()

        self.in_heading = False
        self.outer_href = None
        self.href = None
        self.parts = []
        self.items = []

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        attrs = dict(attrs)

        if tag == "a":
            href = attrs.get(
                "href"
            )

            if href:
                if self.in_heading:
                    self.href = href
                else:
                    self.outer_href = href

        if tag in (
            "h1",
            "h2",
            "h3",
            "h4",
        ):
            self.in_heading = True
            self.href = (
                self.outer_href
            )
            self.parts = []

    def handle_data(
        self,
        data,
    ):
        if self.in_heading:
            self.parts.append(
                data
            )

    def handle_endtag(
        self,
        tag,
    ):
        if (
            tag in (
                "h1",
                "h2",
                "h3",
                "h4",
            )
            and self.in_heading
        ):
            title = " ".join(
                "".join(
                    self.parts
                ).split()
            ).strip()

            if (
                self.href
                and title
            ):
                self.items.append(
                    (
                        title,
                        self.href,
                    )
                )

            self.in_heading = False
            self.href = None
            self.parts = []

        elif (
            tag == "a"
            and not self.in_heading
        ):
            self.outer_href = None


def normalize_host(
    host,
):
    return (
        host
        or ""
    ).lower().removeprefix(
        "www."
    )


def valid_article_url(
    full,
    source,
):
    parsed = urlparse(
        full
    )

    if (
        normalize_host(
            parsed.netloc
        )
        != normalize_host(
            source["host"]
        )
    ):
        return False

    path = (
        parsed.path.rstrip(
            "/"
        )
    )

    if not path:
        return False

    source_path = (
        urlparse(
            source["url"]
        )
        .path
        .rstrip("/")
    )

    if (
        path
        == source_path
    ):
        return False

    blocked = (
        "/category/",
        "/tag/",
        "/author/",
        "/page/",
        "/wp-content/",
        "/wp-admin/",
    )

    if any(
        x in path
        for x in blocked
    ):
        return False

    prefix = source.get(
        "article_prefix"
    )

    if (
        prefix
        and not path.startswith(
            prefix.rstrip("/")
            + "/"
        )
    ):
        return False

    if path.lower().endswith(
        (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".pdf",
            ".css",
            ".js",
        )
    ):
        return False

    return True


def make_website_item(
    source,
    title,
    full,
):
    return {
        "id": (
            f"{source['key']}:"
            f"{full}"
        ),
        "source_key": (
            source["key"]
        ),
        "org": source["org"],
        "platform": "WEBSITE",
        "title": title,
        "text": "",
        "author": "",
        "url": full,
    }


# =========================================================
# OFFICIAL WEBSITES - NORMAL FETCH
# =========================================================

def fetch_website(
    source,
):
    r = requests.get(
        source["url"],
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131 Safari/537.36"
            ),
            "Accept-Language": (
                "el-GR,el;q=0.9,"
                "en;q=0.8"
            ),
        },
    )

    r.raise_for_status()

    parser = HeadingParser()
    parser.feed(
        r.text
    )

    out = []
    used = set()

    for (
        title,
        href,
    ) in parser.items:

        full = urljoin(
            source["url"],
            href,
        )

        if not valid_article_url(
            full,
            source,
        ):
            continue

        if full in used:
            continue

        used.add(
            full
        )

        out.append(
            make_website_item(
                source,
                title,
                full,
            )
        )

    print(
        f"{source['key']} "
        f"website results: "
        f"{len(out)}"
    )

    return out[:50]


# =========================================================
# OFFICIAL WEBSITE - REAL CHROMIUM FALLBACK
# =========================================================

async def fetch_website_browser(
    source,
):
    async with (
        async_playwright()
        as p
    ):
        browser = (
            await p.chromium.launch(
                headless=True
            )
        )

        context = (
            await browser.new_context(
                viewport={
                    "width": 1400,
                    "height": 1000,
                },
                locale="el-GR",
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131 Safari/537.36"
                ),
            )
        )

        page = (
            await context.new_page()
        )

        try:
            if (
                source["key"]
                == "site_kae"
            ):
                await page.goto(
                    "https://www.paobc.gr/",
                    wait_until=(
                        "domcontentloaded"
                    ),
                    timeout=60000,
                )

                await page.wait_for_timeout(
                    2500
                )

            await page.goto(
                source["url"],
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=60000,
            )

            await page.wait_for_timeout(
                5000
            )

            links = (
                await page.locator(
                    "a"
                ).evaluate_all(
                    """
                    els => els.map(a => ({
                        href: a.href || "",
                        text: (
                            a.innerText ||
                            a.textContent ||
                            a.getAttribute("aria-label") ||
                            ""
                        ).trim()
                    }))
                    """
                )
            )

        finally:
            await browser.close()

    out = []
    used = set()

    for link in links:
        href = (
            link.get(
                "href"
            )
            or ""
        ).strip()

        title = " ".join(
            (
                link.get(
                    "text"
                )
                or ""
            ).split()
        ).strip()

        if (
            not href
            or not title
        ):
            continue

        full = urljoin(
            source["url"],
            href,
        )

        if not valid_article_url(
            full,
            source,
        ):
            continue

        if full in used:
            continue

        used.add(
            full
        )

        out.append(
            make_website_item(
                source,
                title[:300],
                full,
            )
        )

    print(
        f"{source['key']} "
        f"Playwright website results: "
        f"{len(out)}"
    )

    return out[:50]


# =========================================================
# OFFICIAL YOUTUBE
# =========================================================

def fetch_youtube(
    source,
):
    feed = (
        "https://www.youtube.com/"
        "feeds/videos.xml"
        f"?channel_id="
        f"{source['channel_id']}"
    )

    r = requests.get(
        feed,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0"
            )
        },
    )

    r.raise_for_status()

    root = ET.fromstring(
        r.text
    )

    ns = {
        "a": (
            "http://www.w3.org/"
            "2005/Atom"
        ),
        "yt": (
            "http://www.youtube.com/"
            "xml/schemas/2015"
        ),
    }

    out = []

    for entry in root.findall(
        "a:entry",
        ns,
    ):
        video_id = (
            entry.findtext(
                "yt:videoId",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        title = (
            entry.findtext(
                "a:title",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        channel = (
            entry.findtext(
                "a:author/a:name",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        if not video_id:
            continue

        url = (
            "https://www.youtube.com/"
            "watch?v="
            f"{video_id}"
        )

        out.append(
            {
                "id": (
                    f"{source['key']}:"
                    f"{video_id}"
                ),
                "source_key": (
                    source["key"]
                ),
                "org": (
                    source["org"]
                ),
                "platform": (
                    "YOUTUBE"
                ),
                "title": title,
                "text": "",
                "author": channel,
                "url": url,
            }
        )

    print(
        f"{source['key']} "
        f"YouTube results: "
        f"{len(out)}"
    )

    return out


# =========================================================
# OFFICIAL X
# =========================================================

async def fetch_x_sources():
    db = (
        "/tmp/"
        "twscrape_official.db"
    )

    Path(db).unlink(
        missing_ok=True
    )

    api = API(
        db,
        raise_when_no_account=True,
        wait_timeout=30,
        wait_interval=1,
    )

    await (
        api.pool
        .add_account_cookies(
            "official-pao",
            (
                f"auth_token="
                f"{X_AUTH_TOKEN}; "
                f"ct0={X_CT0}"
            ),
        )
    )

    results_by_source = {}

    for source in X_SOURCES:
        try:
            results = await gather(
                api.search(
                    (
                        f"from:"
                        f"{source['username']}"
                    ),
                    limit=20,
                )
            )

            out = []

            for tweet in results:
                username = (
                    getattr(
                        getattr(
                            tweet,
                            "user",
                            None,
                        ),
                        "username",
                        "",
                    )
                    or source[
                        "username"
                    ]
                )

                if (
                    username.lower()
                    != source[
                        "username"
                    ].lower()
                ):
                    continue

                tweet_id = str(
                    tweet.id
                )

                text = (
                    getattr(
                        tweet,
                        "rawContent",
                        "",
                    )
                    or ""
                ).strip()

                url = (
                    "https://x.com/"
                    f"{username}/"
                    "status/"
                    f"{tweet_id}"
                )

                out.append(
                    {
                        "id": (
                            f"{source['key']}:"
                            f"{tweet_id}"
                        ),
                        "source_key": (
                            source["key"]
                        ),
                        "org": (
                            source["org"]
                        ),
                        "platform": "X",
                        "title": "",
                        "text": text,
                        "author": (
                            f"@{username}"
                        ),
                        "url": url,
                    }
                )

            results_by_source[
                source["key"]
            ] = out

            print(
                f"{source['key']} "
                f"X results: "
                f"{len(out)}"
            )

        except Exception as exc:
            print(
                f"{source['key']} "
                f"X error: "
                f"{exc}"
            )

    return results_by_source


# =========================================================
# INSTAGRAM / FACEBOOK URL NORMALIZATION
# =========================================================

def canonical_instagram_url(
    href,
):
    parsed = urlparse(
        href
    )

    if (
        "instagram.com"
        not in parsed.netloc
    ):
        return None

    match = re.search(
        r"/(p|reel)/[^/?#]+",
        parsed.path,
    )

    if not match:
        return None

    return (
        "https://www.instagram.com"
        + match.group(0).rstrip(
            "/"
        )
        + "/"
    )


def canonical_facebook_url(
    href,
):
    parsed = urlparse(
        href
    )

    if (
        "facebook.com"
        not in parsed.netloc
    ):
        return None

    path = parsed.path

    if any(
        x in path
        for x in (
            "/posts/",
            "/reel/",
            "/videos/",
        )
    ):
        return (
            "https://www.facebook.com"
            + path.rstrip("/")
        )

    qs = parse_qs(
        parsed.query
    )

    if (
        "/photo" in path
        and qs.get(
            "fbid"
        )
    ):
        return (
            "https://www.facebook.com/"
            "photo/?fbid="
            f"{qs['fbid'][0]}"
        )

    if (
        "permalink.php"
        in path
        and qs.get(
            "story_fbid"
        )
    ):
        url = (
            "https://www.facebook.com/"
            "permalink.php?"
            "story_fbid="
            f"{qs['story_fbid'][0]}"
        )

        if qs.get("id"):
            url += (
                f"&id="
                f"{qs['id'][0]}"
            )

        return url

    return None


# =========================================================
# OFFICIAL INSTAGRAM + FACEBOOK
# =========================================================

async def fetch_browser_sources():
    results_by_source = {}

    async with (
        async_playwright()
        as p
    ):
        browser = (
            await p.chromium.launch(
                headless=True
            )
        )

        context = (
            await browser.new_context(
                viewport={
                    "width": 1400,
                    "height": 1000,
                },
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131 Safari/537.36"
                ),
            )
        )

        for source in (
            BROWSER_SOURCES
        ):
            page = (
                await context.new_page()
            )

            try:
                await page.goto(
                    source["url"],
                    wait_until=(
                        "domcontentloaded"
                    ),
                    timeout=60000,
                )

                await page.wait_for_timeout(
                    5000
                )

                links = (
                    await page.locator(
                        "a"
                    ).evaluate_all(
                        """
                        els => els.map(a => ({
                            href: a.href || "",
                            text: (
                                a.innerText ||
                                a.getAttribute("aria-label") ||
                                ""
                            ).trim()
                        }))
                        """
                    )
                )

                out = []
                used = set()

                for link in links:
                    href = (
                        link.get(
                            "href"
                        )
                        or ""
                    ).strip()

                    if not href:
                        continue

                    if (
                        source[
                            "platform"
                        ]
                        == "INSTAGRAM"
                    ):
                        canonical = (
                            canonical_instagram_url(
                                href
                            )
                        )

                    else:
                        canonical = (
                            canonical_facebook_url(
                                href
                            )
                        )

                    if not canonical:
                        continue

                    if canonical in used:
                        continue

                    used.add(
                        canonical
                    )

                    text = (
                        link.get(
                            "text"
                        )
                        or ""
                    ).strip()

                    if not text:
                        text = (
                            "New official "
                            f"{source['platform']} "
                            "post"
                        )

                    out.append(
                        {
                            "id": (
                                f"{source['key']}:"
                                f"{canonical}"
                            ),
                            "source_key": (
                                source["key"]
                            ),
                            "org": (
                                source["org"]
                            ),
                            "platform": (
                                source[
                                    "platform"
                                ]
                            ),
                            "title": (
                                text[:300]
                            ),
                            "text": "",
                            "author": "",
                            "url": (
                                canonical
                            ),
                        }
                    )

                results_by_source[
                    source["key"]
                ] = out[:30]

                print(
                    f"{source['key']} "
                    f"{source['platform']} "
                    f"results: "
                    f"{len(out[:30])}"
                )

            except Exception as exc:
                print(
                    f"{source['key']} "
                    f"{source['platform']} "
                    f"error: "
                    f"{exc}"
                )

            finally:
                await page.close()

        await browser.close()

    return results_by_source


# =========================================================
# PROCESS / BASELINE / DEDUP
# =========================================================

def process_source(
    source_key,
    items,
    seen,
    initialized_sources,
):
    if not items:
        print(
            f"{source_key}: "
            "no usable items; "
            "not initializing."
        )

        return

    if (
        source_key
        not in initialized_sources
    ):
        seen.update(
            item["id"]
            for item in items
        )

        initialized_sources.add(
            source_key
        )

        print(
            f"{source_key}: "
            "baseline saved "
            f"({len(items)} items)"
        )

        return

    fresh = [
        item
        for item in items
        if item["id"]
        not in seen
    ]

    for item in reversed(
        fresh
    ):
        notify(
            item
        )

        seen.add(
            item["id"]
        )

        print(
            "OFFICIAL SENT:",
            item["platform"],
            item["url"],
        )

    seen.update(
        item["id"]
        for item in items
    )

    print(
        f"{source_key}: "
        f"{len(fresh)} fresh"
    )


# =========================================================
# MAIN
# =========================================================

async def main():
    state = load_state()

    seen = state["ids"]

    initialized_sources = (
        state[
            "initialized_sources"
        ]
    )

    if (
        state["engine"]
        != ENGINE
    ):
        initialized_sources = set()

    results_by_source = {}


    # =====================================================
    # 1. OFFICIAL WEBSITES
    # =====================================================

    for source in (
        WEBSITE_SOURCES
    ):
        try:
            try:
                results_by_source[
                    source["key"]
                ] = fetch_website(
                    source
                )

            except Exception as first_exc:
                print(
                    f"{source['key']} "
                    "normal fetch failed: "
                    f"{first_exc}"
                )

                if (
                    source["key"]
                    == "site_kae"
                ):
                    print(
                        "site_kae: "
                        "trying real Chromium..."
                    )

                    results_by_source[
                        source["key"]
                    ] = (
                        await fetch_website_browser(
                            source
                        )
                    )

                else:
                    raise

        except Exception as exc:
            print(
                f"{source['key']} "
                "website error: "
                f"{exc}"
            )


    # =====================================================
    # 2. OFFICIAL YOUTUBE
    # =====================================================

    for source in (
        YOUTUBE_SOURCES
    ):
        try:
            results_by_source[
                source["key"]
            ] = fetch_youtube(
                source
            )

        except Exception as exc:
            print(
                f"{source['key']} "
                "YouTube error: "
                f"{exc}"
            )


    # =====================================================
    # 3. OFFICIAL X
    # =====================================================

    try:
        results_by_source.update(
            await fetch_x_sources()
        )

    except Exception as exc:
        print(
            "Official X global error: "
            f"{exc}"
        )


    # =====================================================
    # 4. OFFICIAL INSTAGRAM + FACEBOOK
    # =====================================================

    try:
        results_by_source.update(
            await fetch_browser_sources()
        )

    except Exception as exc:
        print(
            "Official social browser error: "
            f"{exc}"
        )


    # =====================================================
    # PROCESS ONLY APPROVED SOURCES
    # =====================================================

    approved_keys = {
        x["key"]
        for x in (
            WEBSITE_SOURCES
            + YOUTUBE_SOURCES
            + X_SOURCES
            + BROWSER_SOURCES
        )
    }

    for source_key in sorted(
        approved_keys
    ):
        process_source(
            source_key,
            results_by_source.get(
                source_key,
                [],
            ),
            seen,
            initialized_sources,
        )


    # =====================================================
    # SAVE STATE
    # =====================================================

    save_state(
        seen,
        initialized_sources,
    )

    print()

    print(
        "OFFICIAL PAO "
        "CHECK COMPLETE"
    )

    print(
        "Initialized "
        "approved sources:",
        len(
            initialized_sources
        ),
        "/",
        len(
            approved_keys
        ),
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
