from pathlib import Path

path = Path("google_monitor.py")
text = path.read_text(encoding="utf-8")

if "DIRECT_TELEGRAM_URL_PREFIXES" not in text:
    anchor = '}\n\n\nTERMS = ['
    block = '''}\n\n# Direct sources that share a broad parent domain with unrelated publishers.\n# Keep these path-specific so, for example, NYTimes stories are not suppressed\n# just because The Athletic now lives under nytimes.com/athletic/.\nDIRECT_TELEGRAM_URL_PREFIXES = (\n    "https://www.nytimes.com/athletic/",\n    "https://nytimes.com/athletic/",\n)\n\n\nTERMS = ['''
    if anchor not in text:
        raise SystemExit("Domain-set anchor not found")
    text = text.replace(anchor, block, 1)

old = '''    for candidate in candidates:\n        host = canonical_host(candidate)\n        if host_matches_direct_domain(host):'''
new = '''    for candidate in candidates:\n        unwrapped = unwrap_google_url(candidate)\n        lowered = (unwrapped or "").lower()\n        if any(lowered.startswith(prefix) for prefix in DIRECT_TELEGRAM_URL_PREFIXES):\n            return "nytimes.com/athletic"\n\n        host = canonical_host(candidate)\n        if host_matches_direct_domain(host):'''

if "return \"nytimes.com/athletic\"" not in text:
    if old not in text:
        raise SystemExit("direct_source_domain loop anchor not found")
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Athletic path-specific direct filter installed")
