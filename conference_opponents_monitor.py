import hashlib
import html
import json
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
import telegram_delivery as telegram


STATE = Path("conference_seen.json")
ENGINE = "conference_opponents_v2_media"
FRESH_WINDOW = timedelta(hours=6)
CHECK_INTERVAL_SECONDS = 300
MAX_SEEN = 12000
MAX_SEND_PER_CHECK = 24

TEAMS = [
    {
        "key": "freiburg",
        "name": "Φράιμπουργκ",
        "query": '"SC Freiburg" OR "Sport-Club Freiburg"',
        "editions": [("DE", "de", "de"), ("GB", "en-GB", "en")],
        "official": "https://www.scfreiburg.com/",
        "host": "scfreiburg.com",
        "media": [
            {"name": "Kicker", "url": "https://www.kicker.de/sc-freiburg/team-news", "host": "kicker.de", "team_page": True},
            {"name": "SPORT1", "url": "https://www.sport1.de/team/sport-club-freiburg/opta_160", "host": "sport1.de", "team_page": True},
        ],
    },
    {
        "key": "brighton",
        "name": "Μπράιτον",
        "query": '"Brighton & Hove Albion" OR "Brighton Hove Albion"',
        "editions": [("GB", "en-GB", "en")],
        "official": "https://www.brightonandhovealbion.com/",
        "host": "brightonandhovealbion.com",
        "media": [
            {"name": "Sky Sports", "url": "https://www.skysports.com/brighton-and-hove-albion", "host": "skysports.com", "team_page": True},
            {"name": "The Guardian", "url": "https://www.theguardian.com/football/brightonfootball", "host": "theguardian.com", "team_page": True},
        ],
    },
    {
        "key": "borac",
        "name": "Μπόρατς",
        "query": '"Borac Banja Luka" OR "FK Borac Banja Luka"',
        "editions": [("BA", "bs", "bs"), ("GB", "en-GB", "en")],
        "official": "https://www.fkborac.net/",
        "host": "fkborac.net",
        "media": [
            {"name": "SportSport.ba", "url": "https://sportsport.ba/klub/fk-borac/103", "host": "sportsport.ba", "team_page": True},
            {"name": "Klix Sport", "url": "https://www.klix.ba/sport/nogomet", "host": "klix.ba", "include": ["borac"]},
        ],
    },
    {
        "key": "kairat",
        "name": "Καϊράτ",
        "query": '"Kairat Almaty" OR "FC Kairat" OR "Кайрат Алматы" OR "Қайрат"',
        "editions": [("KZ", "ru", "ru"), ("GB", "en-GB", "en")],
        "official": "https://fckairat.com/",
        "host": "fckairat.com",
        "media": [
            {"name": "Sports.kz", "url": "https://www.sports.kz/news", "host": "sports.kz", "include": ["кайрат", "kairat"]},
            {"name": "Vesti.kz", "url": "https://vesti.kz/team/24/", "host": "vesti.kz", "team_page": True},
        ],
    },
    {
        "key": "cska_sofia",
        "name": "ΤΣΣΚΑ Σόφιας",
        "query": '("CSKA Sofia" OR "ЦСКА София" OR "ЦСКА-София") -1948',
        "editions": [("BG", "bg", "bg"), ("GB", "en-GB", "en")],
        "official": "https://cska.bg/",
        "host": "cska.bg",
        "media": [
            {"name": "Sportal.bg", "url": "https://sportal.bg/", "host": "sportal.bg", "include": ["цска", "cska"], "exclude": ["1948"]},
            {"name": "Gong.bg", "url": "https://gong.bg/bg-football/efbet-liga", "host": "gong.bg", "include": ["цска", "cska"], "exclude": ["1948"]},
        ],
    },
    {
        "key": "nordsjaelland",
        "name": "Νόρτζελαντ",
        "query": '"FC Nordsjaelland" OR "FC Nordsjælland" OR "Nordsjaelland"',
        "editions": [("DK", "da", "da"), ("GB", "en-GB", "en")],
        "official": "https://www.fcn.dk/",
        "host": "fcn.dk",
        "media": [
            {"name": "Tipsbladet", "url": "https://www.tipsbladet.dk/klubber/fc-nordsjaelland/", "host": "tipsbladet.dk", "team_page": True},
            {"name": "Bold.dk", "url": "https://bold.dk/", "host": "bold.dk", "include": ["nordsjælland", "nordsjaelland"]},
        ],
    },
]

GENERIC_LINK_TEXT = {
    "home", "news", "latest", "more", "read more", "see more", "club", "team",
    "tickets", "shop", "contact", "privacy", "cookies", "menu", "login",
    "nyheder", "mehr", "weiter", "новини", "новости", "вијести", "вести",
}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.parts = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            text = " ".join("".join(self.parts).split())
            if text:
                self.items.append((text, self.href))
            self.href = None
            self.parts = []


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def make_id(team_key, source, title, url):
    basis = f"{team_key}\n{source}\n{normalize(title)}\n{url}"
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "engine": data.get("engine"),
                "ids": set(map(str, data.get("ids", []))),
                "last_check": data.get("last_check"),
            }
    except Exception:
        pass
    return {"engine": None, "ids": set(), "last_check": None}


def save_state(ids, last_check=None):
    STATE.write_text(
        json.dumps(
            {
                "engine": ENGINE,
                "initialized": True,
                "last_check": last_check or now_iso(),
                "ids": sorted(set(map(str, ids)))[-MAX_SEEN:],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def parse_dt(value):
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
    except Exception:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def due(last_check):
    dt = parse_dt(last_check)
    if not dt:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() >= CHECK_INTERVAL_SECONDS


def google_news(team, country, hl, lang):
    query = f'({team["query"]}) when:1d'
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote(query)}&hl={quote(hl)}&gl={country}&ceid={country}:{lang}"
    )
    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    root = ET.fromstring(response.text)
    items = []

    for node in root.findall(".//item"):
        title = " ".join((node.findtext("title") or "").split())
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        source_el = node.find("source")
        publisher = (
            " ".join((source_el.text or "").split())
            if source_el is not None
            else ""
        )
        if not title or not link:
            continue
        items.append(
            {
                "id": make_id(team["key"], "google", title, link),
                "team": team["name"],
                "kind": "GOOGLE NEWS",
                "title": title,
                "url": link,
                "publisher": publisher,
                "published": parse_dt(pub),
            }
        )

    print(f'Conference Google [{team["name"]}/{country}]: {len(items)}')
    return items


def canonical_host(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _skip_link_path(path):
    lowered = (path or "").lower()
    return (
        not lowered
        or lowered == "/"
        or any(
            token in lowered
            for token in (
                "/shop", "/ticket", "/contact", "/privacy", "/cookie",
                "/login", "/account", "/membership", "/impressum",
                "/video/", "/videos/", "/live/", "/livescore/",
                "/table", "/standings", "/fixtures", "/results",
            )
        )
    )


def direct_media(team, source):
    response = requests.get(
        source["url"],
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (PAO Conference Media Watcher)"},
    )
    response.raise_for_status()

    parser = LinkParser()
    parser.feed(response.text)

    include = [normalize(x) for x in source.get("include", [])]
    exclude = [normalize(x) for x in source.get("exclude", [])]
    team_page = bool(source.get("team_page"))
    out = []
    used = set()

    for text, href in parser.items:
        title = " ".join(text.split()).strip()
        normalized_title = normalize(title)

        if len(title) < 14 or normalized_title in GENERIC_LINK_TEXT:
            continue
        if include and not any(term in normalized_title for term in include):
            continue
        if exclude and any(term in normalized_title for term in exclude):
            continue

        url = urljoin(source["url"], href).split("#")[0]
        host = canonical_host(url)
        if host != source["host"] and not host.endswith("." + source["host"]):
            continue

        path = urlparse(url).path.rstrip("/")
        if _skip_link_path(path):
            continue

        # General/homepage media sources must explicitly mention the team.
        # Team-specific media pages are already scoped by the publisher.
        if not team_page and include and not any(term in normalized_title for term in include):
            continue

        signature = (normalized_title, url)
        if signature in used:
            continue
        used.add(signature)

        out.append(
            {
                "id": make_id(team["key"], f'media:{source["name"]}', title, url),
                "team": team["name"],
                "kind": f'DIRECT / {source["name"]}',
                "title": title[:500],
                "url": url,
                "publisher": source["name"],
                "published": None,
            }
        )

        if len(out) >= 40:
            break

    print(
        f'Conference Media [{team["name"]}/{source["name"]}]: {len(out)}'
    )
    return out


def direct_official(team):
    response = requests.get(
        team["official"],
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (PAO Conference Watcher)"},
    )
    response.raise_for_status()
    parser = LinkParser()
    parser.feed(response.text)

    out = []
    used = set()
    for text, href in parser.items:
        title = " ".join(text.split()).strip()
        if len(title) < 12:
            continue
        if normalize(title) in GENERIC_LINK_TEXT:
            continue

        url = urljoin(team["official"], href).split("#")[0]
        if canonical_host(url) != team["host"]:
            continue

        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path or path in ("", "/"):
            continue
        if any(
            token in path.lower()
            for token in (
                "/shop", "/ticket", "/contact", "/privacy", "/cookie",
                "/login", "/account", "/membership", "/impressum",
            )
        ):
            continue

        signature = (normalize(title), url)
        if signature in used:
            continue
        used.add(signature)

        out.append(
            {
                "id": make_id(team["key"], "official", title, url),
                "team": team["name"],
                "kind": "DIRECT / OFFICIAL",
                "title": title[:500],
                "url": url,
                "publisher": team["host"],
                "published": None,
            }
        )

        if len(out) >= 80:
            break

    print(f'Conference Direct [{team["name"]}]: {len(out)}')
    return out


def format_alert(item):
    lines = [
        f'⚽ <b>{html.escape(item["team"])}</b>',
        f'📌 {html.escape(item["kind"])}',
        "",
        f'<b>{html.escape(item["title"])}</b>',
    ]
    if item.get("publisher"):
        lines.append(f'Πηγή: {html.escape(item["publisher"])}')
    return "\n".join(lines)


def notify(item):
    return telegram.send(
        "conference_opponents",
        f'CONFERENCE | {item["team"]}',
        format_alert(item),
        item["url"],
    )


def main():
    state = load_state()
    seen = state["ids"]

    if not due(state.get("last_check")):
        print("Conference opponents check not due yet.")
        return

    results = []
    errors = []

    for team in TEAMS:
        for edition in team["editions"]:
            try:
                results.extend(google_news(team, *edition))
            except Exception as exc:
                errors.append(
                    f'Google {team["name"]}/{edition[0]}: {type(exc).__name__}: {exc}'
                )

        try:
            results.extend(direct_official(team))
        except Exception as exc:
            errors.append(f'Direct {team["name"]}: {type(exc).__name__}: {exc}')

        for source in team.get("media", []):
            try:
                results.extend(direct_media(team, source))
            except Exception as exc:
                errors.append(
                    f'Media {team["name"]}/{source["name"]}: '
                    f'{type(exc).__name__}: {exc}'
                )

    unique = {}
    for item in results:
        unique[item["id"]] = item
    results = list(unique.values())

    if errors:
        for error in errors:
            print("Conference source warning:", error)

    if not results:
        if errors:
            raise RuntimeError("All Conference opponent sources returned no usable results")
        save_state(seen)
        return

    # First deployment baselines everything currently visible. No old-news flood.
    if state["engine"] != ENGINE:
        seen.update(item["id"] for item in results)
        save_state(seen)
        print(f"Conference baseline saved: {len(results)} items")
        return

    cutoff = datetime.now(timezone.utc) - FRESH_WINDOW
    unseen = []
    stale_ids = set()

    for item in results:
        if item["id"] in seen:
            continue
        published = item.get("published")
        if published is not None and published < cutoff:
            stale_ids.add(item["id"])
            continue
        unseen.append(item)

    seen.update(stale_ids)

    # Direct official links have no reliable publish timestamp; newly appearing
    # links are safe because the first deployment was baselined.
    unseen = unseen[:MAX_SEND_PER_CHECK]

    sent = 0
    for item in unseen:
        if not notify(item):
            raise RuntimeError(
                f'Telegram Conference delivery failed for {item["team"]}'
            )
        seen.add(item["id"])
        sent += 1
        print(f'Conference sent: {item["team"]} | {item["kind"]} | {item["title"]}')

    save_state(seen)
    print(
        f"Conference cycle complete: sources={len(results)} sent={sent} "
        f"stale_baselined={len(stale_ids)} warnings={len(errors)}"
    )


if __name__ == "__main__":
    main()
