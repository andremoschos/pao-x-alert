import asyncio
import json
import re

from playwright.async_api import async_playwright


PROFILES = [
    ("instagram_pae", "fcpanathinaikos"),
    ("instagram_kae", "paobcgr"),
    ("instagram_ao", "panathinaikos_1908"),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131 Safari/537.36"
)


def extract_urls(text):
    found = set()

    for kind, code in re.findall(
        r'/(p|reel)/([A-Za-z0-9_-]+)',
        text or "",
    ):
        found.add(
            f"https://www.instagram.com/{kind}/{code}/"
        )

    for code in re.findall(
        r'"shortcode"\s*:\s*"([A-Za-z0-9_-]+)"',
        text or "",
    ):
        found.add(
            f"https://www.instagram.com/p/{code}/"
        )

    return found


async def probe(context, label, username):
    profile_url = (
        f"https://www.instagram.com/{username}/"
    )

    page = await context.new_page()

    print("=" * 70)
    print(label, username)

    try:
        response = await page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print(
            "PAGE STATUS:",
            response.status if response else "NONE",
        )

        await page.wait_for_timeout(5000)

        for _ in range(3):
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(1200)

        hrefs = await page.locator(
            'a[href*="/p/"], a[href*="/reel/"]'
        ).evaluate_all(
            "els => els.map(a => a.href)"
        )

        dom_urls = {
            x
            for x in hrefs
            if "instagram.com" in x
        }

        html = await page.content()
        html_urls = extract_urls(html)

        print("DOM URLS:", len(dom_urls))
        print("HTML URLS:", len(html_urls))

        api_url = (
            "https://www.instagram.com/"
            "api/v1/users/web_profile_info/"
            f"?username={username}"
        )

        api_response = await context.request.get(
            api_url,
            headers={
                "User-Agent": UA,
                "Referer": profile_url,
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30000,
        )

        print(
            "PROFILE API STATUS:",
            api_response.status,
        )

        api_urls = set()

        try:
            text = await api_response.text()
            api_urls.update(
                extract_urls(text)
            )

            if api_response.ok:
                data = json.loads(text)

                user = (
                    (data.get("data") or {})
                    .get("user")
                    or {}
                )

                edges = (
                    (
                        user.get(
                            "edge_owner_to_timeline_media"
                        )
                        or {}
                    )
                    .get("edges")
                    or []
                )

                for edge in edges:
                    node = (
                        edge.get("node")
                        or {}
                    )

                    shortcode = node.get(
                        "shortcode"
                    )

                    if shortcode:
                        api_urls.add(
                            "https://www.instagram.com/"
                            f"p/{shortcode}/"
                        )

        except Exception as exc:
            print(
                "API PARSE ERROR:",
                exc,
            )

        all_urls = sorted(
            dom_urls
            | html_urls
            | api_urls
        )

        print(
            "API URLS:",
            len(api_urls),
        )

        print(
            "TOTAL:",
            len(all_urls),
        )

        for url in all_urls[:15]:
            print(
                "FOUND:",
                url,
            )

    except Exception as exc:
        print(
            "ERROR:",
            repr(exc),
        )

    finally:
        await page.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            viewport={
                "width": 1400,
                "height": 1200,
            },
            locale="en-US",
            user_agent=UA,
        )

        for label, username in PROFILES:
            await probe(
                context,
                label,
                username,
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
