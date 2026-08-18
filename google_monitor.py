import os
import json
import html
import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs
import xml.etree.ElementTree as ET

import requests


STATE = Path("google_seen.json")
ENGINE = "google_global_news_plus_web_v2"
MAX_SEEN = 12000

ALERT_RSS = os.environ["GOOGLE_ALERT_RSS"]
TOPIC = os.environ["NTFY_GOOGLE_TOPIC"]


# =========================================================
# TARGET TERMS
# =========================================================

TERMS = [
    "panathinaikos",
    "Panathinaïkos",
    "παναθηναϊκός",
    "παναθηναϊκού",
    "παναθηναϊκό",
    "παναθηναικος",
    "παναθηναικου",
    "παναθηναικο",
]


# =========================================================
# GOOGLE NEWS EDITIONS
# =========================================================

NEWS_EDITIONS = [
    ("GR", "el", "el"),
    ("US", "en-US", "en"),
    ("GB", "en-GB", "en"),
    ("ES", "es", "es"),
    ("IT", "it", "it"),
    ("FR", "fr", "fr"),
    ("DE", "de", "de"),
    ("TR", "tr", "tr"),
    ("NL", "nl", "nl"),
    ("PL", "pl", "pl"),
    ("RO", "ro", "ro"),
    ("CZ", "cs", "cs"),
    ("RS", "sr", "sr"),
    ("HR", "hr", "hr"),
    ("AU", "en-AU", "en"),
    ("CA", "en-CA", "en"),
    ("IN", "en-IN", "en"),
]


QUERY = (
    "("
    + " OR ".join(
        f'"{t}"'
        for t in TERMS
    )
    + ") when:1d"
)


# =========================================================
# NORMALIZATION / MATCHING
# =========================================================

def normalize_for_match(text):
    text = html.unescape(
        text or ""
    )

    text = unicodedata.normalize(
        "NFKD",
        text,
    )

    text = "".join(
        ch
        for ch in text
        if not unicodedata.combining(ch)
    )

    text = text.casefold()

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text


NORMALIZED_TERMS = {
    normalize_for_match(term)
    for term in TERMS
}


def title_matches_panathinaikos(title):
    normalized_title = normalize_for_match(
        title
    )

    if not normalized_title:
        return False

    return any(
        term in normalized_title
        for term in NORMALIZED_TERMS
    )


# =========================================================
# STATE
# =========================================================

def load_state():
    try:
        data = json.loads(
            STATE.read_text(
                encoding="utf-8"
            )
        )

        return {
            "engine": data.get(
                "engine"
            ),
            "ids": set(
                str(x)
                for x in data.get(
                    "ids",
                    [],
                )
            ),
        }

    except Exception:
        return {
            "engine": None,
            "ids": set(),
        }


def save_state(ids):
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
                "initialized": True,
                "ids": ordered[
                    -MAX_SEEN:
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(s):
    if not s:
        return ""

    s = html.unescape(
        s
    )

    s = re.sub(
        r"<[^>]+>",
        "",
        s,
    )

    return " ".join(
        s.split()
    )


def unwrap_google_url(url):
    if not url:
        return ""

    try:
        parsed = urlparse(
            url
        )

        qs = parse_qs(
            parsed.query
        )

        for key in (
            "url",
            "q",
        ):
            if (
                key in qs
                and qs[key]
            ):
                return qs[key][0]

    except Exception:
        pass

    return url


def canonical_title(title):
    t = clean_text(
        title
    ).lower()

    # Google News συχνά βάζει:
    # "Τίτλος άρθρου - Publisher"
    t = re.sub(
        r"\s+-\s+[^-]{1,100}$",
        "",
        t,
    )

    t = re.sub(
        r"\s+",
        " ",
        t,
    ).strip()

    return t


def make_key(
    title,
    url,
    fallback="",
):
    basis = (
        canonical_title(title)
        or unwrap_google_url(url)
        or fallback
    )

    return hashlib.sha256(
        basis.encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()


# =========================================================
# GOOGLE ALERTS / WEB
# =========================================================

def parse_google_alert_atom(
    xml_text,
):
    root = ET.fromstring(
        xml_text
    )

    ns = {
        "a": (
            "http://www.w3.org/"
            "2005/Atom"
        )
    }

    entries = []

    for e in root.findall(
        ".//a:entry",
        ns,
    ):
        eid = (
            e.findtext(
                "a:id",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        title = clean_text(
            e.findtext(
                "a:title",
                default="",
                namespaces=ns,
            )
        )

        link_el = e.find(
            "a:link",
            ns,
        )

        link = (
            link_el.get(
                "href",
                "",
            )
            if link_el is not None
            else ""
        )

        updated = (
            e.findtext(
                "a:updated",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        url = unwrap_google_url(
            link
        )

        entries.append(
            {
                "id": make_key(
                    title,
                    url,
                    eid or updated,
                ),
                "title": (
                    title
                    or "(χωρίς τίτλο)"
                ),
                "url": url,
                "source": "WEB",
                "publisher": "",
                "edition": (
                    "Google Alerts / Web"
                ),
            }
        )

    return entries


def fetch_web_alert():
    r = requests.get(
        ALERT_RSS,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0"
            )
        },
    )

    r.raise_for_status()

    entries = (
        parse_google_alert_atom(
            r.text
        )
    )

    print(
        "Google Web/Alerts "
        f"results: {len(entries)}"
    )

    return entries


# =========================================================
# GOOGLE NEWS
# =========================================================

def fetch_google_news_edition(
    country,
    hl,
    lang,
):
    q = quote(
        QUERY
    )

    url = (
        "https://news.google.com/"
        f"rss/search?q={q}"
        f"&hl={quote(hl)}"
        f"&gl={country}"
        f"&ceid={country}:{lang}"
    )

    r = requests.get(
        url,
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

    entries = []

    raw_count = 0
    rejected_count = 0

    for item in root.findall(
        ".//item"
    ):
        raw_count += 1

        title = clean_text(
            item.findtext(
                "title"
            )
            or ""
        )

        # ==============================================
        # CRITICAL FILTER:
        # Google News περνάει ΜΟΝΟ αν ο τίτλος
        # αναφέρει πραγματικά Παναθηναϊκό.
        # ==============================================

        if not title_matches_panathinaikos(
            title
        ):
            rejected_count += 1
            continue

        link = (
            item.findtext(
                "link"
            )
            or ""
        ).strip()

        guid = (
            item.findtext(
                "guid"
            )
            or ""
        ).strip()

        pubdate = (
            item.findtext(
                "pubDate"
            )
            or ""
        ).strip()

        publisher = clean_text(
            item.findtext(
                "source"
            )
            or ""
        )

        entries.append(
            {
                "id": make_key(
                    title,
                    link,
                    guid or pubdate,
                ),
                "title": (
                    title
                    or "(χωρίς τίτλο)"
                ),
                "url": link,
                "source": "NEWS",
                "publisher": publisher,
                "edition": country,
            }
        )

    print(
        f"Google News [{country}]: "
        f"{len(entries)} accepted / "
        f"{raw_count} raw "
        f"({rejected_count} filtered)"
    )

    return entries


# =========================================================
# NTFY
# =========================================================

def notify(entry):
    endpoint = (
        f"https://ntfy.sh/{TOPIC}"
    )

    if (
        entry["source"]
        == "NEWS"
    ):
        alert_title = (
            "NEO GOOGLE NEWS: "
            "PANATHINAIKOS"
        )

        tags = "newspaper"

    else:
        alert_title = (
            "NEO GOOGLE WEB: "
            "PANATHINAIKOS"
        )

        tags = "mag"

    headers = {
        "Title": alert_title,
        "Priority": "high",
        "Tags": tags,
    }

    if entry["url"]:
        headers["Click"] = (
            entry["url"]
        )

    body = entry["title"]

    if entry.get(
        "publisher"
    ):
        body += (
            "\nΠηγή: "
            f"{entry['publisher']}"
        )

    if (
        entry["source"]
        == "NEWS"
    ):
        body += (
            "\nGoogle News edition: "
            f"{entry['edition']}"
        )

    if entry["url"]:
        body += (
            "\n"
            + entry["url"]
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
# MAIN
# =========================================================

def main():
    state = load_state()

    seen = state["ids"]

    merged = {}


    # =====================================================
    # 1. GOOGLE WEB / ALERTS
    # =====================================================

    try:
        for e in fetch_web_alert():
            merged[
                e["id"]
            ] = e

    except Exception as exc:
        print(
            "Google Web/Alerts "
            f"error: {exc}"
        )


    # =====================================================
    # 2. INTERNATIONAL GOOGLE NEWS
    # =====================================================

    successful_editions = 0

    for (
        country,
        hl,
        lang,
    ) in NEWS_EDITIONS:

        try:
            entries = (
                fetch_google_news_edition(
                    country,
                    hl,
                    lang,
                )
            )

            successful_editions += 1

            for e in entries:
                # Prefer NEWS data
                # αν υπάρχει ίδιο story
                # και στο Web.
                merged[
                    e["id"]
                ] = e

        except Exception as exc:
            print(
                "Google News error "
                f"[{country}]: "
                f"{exc}"
            )


    print(
        "Successful Google News "
        f"editions: "
        f"{successful_editions}/"
        f"{len(NEWS_EDITIONS)}"
    )

    print(
        "Combined unique Google "
        f"results: {len(merged)}"
    )

    entries = list(
        merged.values()
    )


    # =====================================================
    # NOTHING FOUND
    # =====================================================

    if not entries:
        print(
            "No Google results "
            "right now."
        )

        return


    # =====================================================
    # BASELINE ONLY IF ENGINE CHANGED
    # =====================================================

    if (
        state["engine"]
        != ENGINE
    ):
        seen.update(
            e["id"]
            for e in entries
        )

        save_state(
            seen
        )

        print(
            "GLOBAL Google baseline "
            f"saved: {len(entries)} "
            "entries"
        )

        return


    # =====================================================
    # NEW RESULTS
    # =====================================================

    fresh = [
        e
        for e in entries
        if e["id"]
        not in seen
    ]

    news_fresh = [
        e
        for e in fresh
        if e["source"]
        == "NEWS"
    ]

    web_fresh = [
        e
        for e in fresh
        if e["source"]
        == "WEB"
    ]

    print(
        f"Fresh NEWS results: "
        f"{len(news_fresh)}"
    )

    print(
        f"Fresh WEB results: "
        f"{len(web_fresh)}"
    )


    # =====================================================
    # SEND
    # =====================================================

    for e in reversed(
        fresh
    ):
        notify(
            e
        )

        seen.add(
            e["id"]
        )

        print(
            f"Google "
            f"{e['source']} "
            "sent:",
            e["title"],
        )


    # =====================================================
    # SAVE SEEN
    # =====================================================

    seen.update(
        e["id"]
        for e in entries
    )

    save_state(
        seen
    )

    print(
        "Fresh GLOBAL Google "
        f"results: {len(fresh)}"
    )


if __name__ == "__main__":
    main()
