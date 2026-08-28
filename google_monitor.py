import os
import json
import html
import hashlib
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs
import xml.etree.ElementTree as ET

import requests


STATE = Path("google_seen.json")
ENGINE = "google_global_news_plus_web_v3_rate_safe"
MAX_SEEN = 12000

# Keep near-real-time alerts, but never replay a full day of stale Google results
# after a restart/state mismatch.
FRESH_WINDOW = timedelta(hours=6)

# ntfy.sh free/public topics are burst-limited. The fast runner gives Google
# ~105 seconds, so 30 paced alerts comfortably fit while avoiding 429 floods.
MAX_SEND_PER_RUN = 30
SEND_GAP_SECONDS = 1.2
MAX_NOTIFY_RETRIES = 3

ALERT_RSS = os.environ["GOOGLE_ALERT_RSS"]
TOPIC = os.environ["NTFY_GOOGLE_TOPIC"]


# These are the exact website domains already monitored DIRECTLY by the
# PAO watcher system. Google News + Web must not repeat those sources.
# Filtering is DOMAIN-BASED only; we do not guess from similar titles/names.
DIRECT_TELEGRAM_DOMAINS = {
    "monobala.gr",
    "sport-fm.gr",
    "sportal.gr",
    "gazzetta.gr",
    "sport24.gr",
    "athletiko.gr",
    "sdna.gr",
    "tanea.gr",
    "in.gr",
    "to10.gr",
    "pickandroll.gr",
    "sportdog.gr",
    "onsports.gr",
    "eurohoops.net",
    "panathinaikos24.gr",
    "novasports.gr",
    "transferfeed.com",
    "filathlos.gr",
    "regista.gr",
    "agrinio24.gr",
    "astratv.gr",
    "paopantou.gr",
    "ole.gr",
    "trifilara.gr",
    "olaprasina1908.gr",
    "pao.gr",
    "paobc.gr",
    "pao1908.com",
    "euroleaguebasketball.net",
    "contra.gr",
    "basketnews.com",
    "beinsports.com",
    "mozzartsport.com",
    "marca.com",
    "espn.com",
    "sportando.basketball",
    "meridiansport.rs",
    "basketinside.com",
    "gigantes.com",
    "basketballsphere.com",
    "mundodeportivo.com",
    "footmercato.net",
    "rmcsport.bfmtv.com",
    "gazzetta.it",
    "tuttosport.com",
    "kicker.de",
    "sportbild.bild.de",
    "abola.pt",
    "record.pt",
    "ojogo.pt",
    "skysports.com",
    "bbc.com",
    "theguardian.com",
    "talksport.com",
    "telegraaf.nl",
    "fanatik.com.tr",
    "sporx.com",
    "fotomac.com.tr",
    "vi.nl",
    "rtbf.be",
    "sporza.be",
    "sportal.bg",
    "hln.be",
    "gol.dnevnik.hr",
    "sportske.jutarnji.hr",
    "index.hr",
    "panorama.com.al",
    "dsport.bg",
    "gong.bg",
    "dzfoot.com",
    "hesport.com",
    "sport.le360.ma",
    "ge.globo.com",
    "lance.com.br",
    "ole.com.ar",
    "sportmedia.mk",
    "nogomania.com",
    "kerkida.net",
    "24sports.com.cy",
}

# Direct sources that share a broad parent domain with unrelated publishers.
# Keep these path-specific so, for example, NYTimes stories are not suppressed
# just because The Athletic now lives under nytimes.com/athletic/.
DIRECT_TELEGRAM_URL_PREFIXES = (
    "https://www.nytimes.com/athletic/",
    "https://nytimes.com/athletic/",
)


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
    ("RS", "sr", "sr"),
    ("HR", "hr", "hr"),
    ("AU", "en-AU", "en"),
    ("CA", "en-CA", "en"),
    ("IN", "en-IN", "en"),
]

QUERY = "(" + " OR ".join(f'\"{t}\"' for t in TERMS) + ") when:1d"


def normalize_for_match(text):
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text.casefold()).strip()
    return text


NORMALIZED_TERMS = {normalize_for_match(term) for term in TERMS}


def title_matches_panathinaikos(title):
    normalized = normalize_for_match(title)
    return bool(normalized) and any(term in normalized for term in NORMALIZED_TERMS)


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "engine": data.get("engine"),
            "ids": {str(x) for x in data.get("ids", [])},
        }
    except Exception:
        return {"engine": None, "ids": set()}


def save_state(ids):
    ordered = sorted({str(x) for x in ids})
    STATE.write_text(
        json.dumps(
            {
                "engine": ENGINE,
                "initialized": True,
                "ids": ordered[-MAX_SEEN:],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def clean_text(value):
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(value.split())


def unwrap_google_url(url):
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in ("url", "q"):
            if key in qs and qs[key]:
                return qs[key][0]
    except Exception:
        pass
    return url


def canonical_host(url):
    """Return a lowercase registrable-looking host for exact domain matching."""
    if not url:
        return ""
    try:
        value = unwrap_google_url(url)
        host = (urlparse(value).hostname or "").lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def host_matches_direct_domain(host):
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in DIRECT_TELEGRAM_DOMAINS
    )


def direct_source_domain(entry):
    """
    Return the blocked direct domain if this Google result is already covered
    directly by another PAO watcher. Prefer Google News <source url=...>, then
    the actual Web URL. If neither exposes a reliable domain, keep the result
    rather than guessing.
    """
    candidates = [
        entry.get("publisher_url", ""),
        entry.get("url", "") if entry.get("source") == "WEB" else "",
    ]

    for candidate in candidates:
        unwrapped = unwrap_google_url(candidate)
        lowered = (unwrapped or "").lower()
        if any(lowered.startswith(prefix) for prefix in DIRECT_TELEGRAM_URL_PREFIXES):
            return "nytimes.com/athletic"

        host = canonical_host(candidate)
        if host_matches_direct_domain(host):
            for domain in DIRECT_TELEGRAM_DOMAINS:
                if host == domain or host.endswith("." + domain):
                    return domain
    return ""


def canonical_title(title):
    value = clean_text(title).lower()
    value = re.sub(r"\s+-\s+[^-]{1,100}$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def make_key(title, url, fallback=""):
    basis = canonical_title(title) or unwrap_google_url(url) or fallback
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()


def parse_published(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def published_sort_key(entry):
    dt = entry.get("published_dt")
    return dt.timestamp() if dt else 0.0


def is_recent(entry, now_utc):
    dt = entry.get("published_dt")
    if dt is None:
        return True
    return dt >= now_utc - FRESH_WINDOW


def parse_google_alert_atom(xml_text):
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []

    for node in root.findall(".//a:entry", ns):
        eid = (node.findtext("a:id", default="", namespaces=ns) or "").strip()
        title = clean_text(node.findtext("a:title", default="", namespaces=ns))
        link_el = node.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        updated = (node.findtext("a:updated", default="", namespaces=ns) or "").strip()
        url = unwrap_google_url(link)

        if title and not title_matches_panathinaikos(title):
            continue

        entries.append(
            {
                "id": make_key(title, url, eid or updated),
                "title": title or "(χωρίς τίτλο)",
                "url": url,
                "source": "WEB",
                "publisher": "",
                "publisher_url": "",
                "edition": "Google Alerts / Web",
                "published": updated,
                "published_dt": parse_published(updated),
            }
        )
    return entries


def fetch_web_alert():
    response = requests.get(
        ALERT_RSS,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    entries = parse_google_alert_atom(response.text)
    print(f"Google Web/Alerts results: {len(entries)}")
    return entries


def fetch_google_news_edition(country, hl, lang):
    q = quote(QUERY)
    url = (
        "https://news.google.com/rss/search"
        f"?q={q}&hl={quote(hl)}&gl={country}&ceid={country}:{lang}"
    )
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    entries = []
    raw_count = 0
    rejected_count = 0

    for item in root.findall(".//item"):
        raw_count += 1
        title = clean_text(item.findtext("title") or "")
        if not title_matches_panathinaikos(title):
            rejected_count += 1
            continue

        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()

        source_el = item.find("source")
        publisher = clean_text(source_el.text if source_el is not None else "")
        publisher_url = (
            (source_el.get("url", "") or "").strip()
            if source_el is not None
            else ""
        )

        entries.append(
            {
                "id": make_key(title, link, guid or pubdate),
                "title": title or "(χωρίς τίτλο)",
                "url": link,
                "source": "NEWS",
                "publisher": publisher,
                "publisher_url": publisher_url,
                "edition": country,
                "published": pubdate,
                "published_dt": parse_published(pubdate),
            }
        )

    print(
        f"Google News [{country}]: {len(entries)} accepted / "
        f"{raw_count} raw ({rejected_count} filtered)"
    )
    return entries


def notify(entry):
    endpoint = f"https://ntfy.sh/{TOPIC}"

    if entry["source"] == "NEWS":
        alert_title = "NEO GOOGLE NEWS: PANATHINAIKOS"
        tags = "newspaper"
    else:
        alert_title = "NEO GOOGLE WEB: PANATHINAIKOS"
        tags = "mag"

    headers = {
        "Title": alert_title,
        "Priority": "high",
        "Tags": tags,
    }
    if entry["url"]:
        headers["Click"] = entry["url"]

    body = entry["title"]
    if entry.get("publisher"):
        body += f"\nΠηγή: {entry['publisher']}"
    if entry["source"] == "NEWS":
        body += f"\nGoogle News edition: {entry['edition']}"
    if entry["url"]:
        body += "\n" + entry["url"]

    for attempt in range(1, MAX_NOTIFY_RETRIES + 1):
        response = requests.post(
            endpoint,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=20,
        )

        if 200 <= response.status_code < 300:
            return True

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except Exception:
                delay = 4.0 * attempt
            delay = max(2.0, min(delay, 10.0))
            print(
                f"ntfy 429 for Google alert; retry {attempt}/"
                f"{MAX_NOTIFY_RETRIES} in {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        if 500 <= response.status_code < 600:
            delay = 2.0 * attempt
            print(
                f"ntfy {response.status_code}; retry {attempt}/"
                f"{MAX_NOTIFY_RETRIES} in {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        response.raise_for_status()

    print("ntfy still rate-limited/unavailable; leaving item unseen for next cycle.")
    return False


def main():
    state = load_state()
    seen = state["ids"]
    merged = {}

    try:
        for entry in fetch_web_alert():
            merged[entry["id"]] = entry
    except Exception as exc:
        print(f"Google Web/Alerts error: {exc}")

    successful_editions = 0
    for country, hl, lang in NEWS_EDITIONS:
        try:
            entries = fetch_google_news_edition(country, hl, lang)
            successful_editions += 1
            for entry in entries:
                merged[entry["id"]] = entry
        except Exception as exc:
            print(f"Google News error [{country}]: {exc}")

    print(
        f"Successful Google News editions: "
        f"{successful_editions}/{len(NEWS_EDITIONS)}"
    )
    print(f"Combined unique Google results: {len(merged)}")

    all_entries = list(merged.values())
    if not all_entries:
        print("No Google results right now.")
        return

    # Remove only sources whose exact domain is already monitored directly in
    # the PAO watcher system. Suppressed IDs are saved as seen, so they can never
    # backlog and suddenly flood ntfy after a later restart.
    suppressed_direct = []
    entries = []

    for entry in all_entries:
        blocked_domain = direct_source_domain(entry)
        if blocked_domain:
            suppressed_direct.append((entry, blocked_domain))
        else:
            entries.append(entry)

    if suppressed_direct:
        seen.update(entry["id"] for entry, _domain in suppressed_direct)
        examples = ", ".join(
            f"{domain}:{entry.get('publisher') or entry.get('title', '')[:35]}"
            for entry, domain in suppressed_direct[:8]
        )
        print(
            f"Direct PAO sources suppressed from Google: "
            f"{len(suppressed_direct)} | {examples}"
        )

    # v3 intentionally baselines once so deployment of the anti-flood fix
    # cannot replay whatever Google currently exposes.
    if state["engine"] != ENGINE:
        seen.update(entry["id"] for entry in entries)
        save_state(seen)
        print(
            f"RATE-SAFE Google baseline saved: {len(entries)} kept, "
            f"{len(suppressed_direct)} direct-suppressed"
        )
        return

    unseen = [entry for entry in entries if entry["id"] not in seen]
    now_utc = datetime.now(timezone.utc)

    recent = [entry for entry in unseen if is_recent(entry, now_utc)]
    stale = [entry for entry in unseen if not is_recent(entry, now_utc)]

    # Old unseen results are deliberately baselined, not alerted.
    seen.update(entry["id"] for entry in stale)

    news_recent = [e for e in recent if e["source"] == "NEWS"]
    web_recent = [e for e in recent if e["source"] == "WEB"]

    print(f"Recent unseen NEWS: {len(news_recent)}")
    print(f"Recent unseen WEB: {len(web_recent)}")
    print(f"Direct sources suppressed: {len(suppressed_direct)}")
    print(f"Stale unseen baselined: {len(stale)}")

    # Prioritize the newest stories if a rare burst exceeds the safe batch.
    recent.sort(key=published_sort_key, reverse=True)
    batch = recent[:MAX_SEND_PER_RUN]

    sent = 0
    for entry in batch:
        if not notify(entry):
            break

        seen.add(entry["id"])
        sent += 1
        print(f"Google {entry['source']} sent: {entry['title']}")
        time.sleep(SEND_GAP_SECONDS)

    save_state(seen)

    pending = max(0, len(recent) - sent)
    print(
        f"Google rate-safe cycle: sent={sent}, "
        f"pending_recent={pending}, "
        f"direct_suppressed={len(suppressed_direct)}, "
        f"stale_baselined={len(stale)}"
    )


if __name__ == "__main__":
    main()
