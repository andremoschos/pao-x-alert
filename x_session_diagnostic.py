import asyncio
import os
from urllib.parse import quote

from playwright.async_api import async_playwright

AUTH = os.environ.get("X_AUTH_TOKEN", "")
CT0 = os.environ.get("X_CT0", "")


async def inspect_page(page, label, url):
    api_errors = []

    def on_response(response):
        if "/i/api/" in response.url and response.status >= 400:
            clean = response.url.split("?", 1)[0]
            api_errors.append((response.status, clean[-180:]))

    page.on("response", on_response)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(8000)

    article_count = await page.locator("article").count()
    status_link_count = await page.locator('a[href*="/status/"]').count()
    home_link_count = await page.locator('a[href="/home"]').count()
    login_link_count = await page.locator('a[href="/login"]').count()
    primary_count = await page.locator('[data-testid="primaryColumn"]').count()
    body = (await page.locator("body").inner_text(timeout=5000)).lower()

    markers = {
        "panathinaikos": "panathinaikos" in body or "παναθηνα" in body,
        "log_in": "log in" in body or "sign in" in body,
        "something_wrong": "something went wrong" in body,
        "rate_limit": "rate limit" in body or "too many requests" in body,
        "try_again": "try again" in body,
    }

    print(
        "X_DIAG",
        label,
        {
            "http_status": response.status if response else None,
            "final_url": page.url.split("?", 1)[0],
            "title": await page.title(),
            "articles": article_count,
            "status_links": status_link_count,
            "home_link": home_link_count,
            "login_link": login_link_count,
            "primary_column": primary_count,
            "body_len": len(body),
            "markers": markers,
            "api_errors": api_errors[:12],
        },
        flush=True,
    )


async def main():
    if not AUTH or not CT0:
        raise RuntimeError("X_AUTH_TOKEN or X_CT0 is missing")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1400, "height": 1100},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
            ),
        )
        await context.add_cookies(
            [
                {
                    "name": "auth_token",
                    "value": AUTH,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
                {
                    "name": "ct0",
                    "value": CT0,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                },
            ]
        )

        cookie_names = sorted(
            {cookie.get("name") for cookie in await context.cookies("https://x.com")}
        )
        print(
            "X_DIAG cookies_loaded=",
            [name for name in cookie_names if name in {"auth_token", "ct0"}],
            flush=True,
        )

        page = await context.new_page()
        await inspect_page(page, "home", "https://x.com/home")
        await inspect_page(
            page,
            "search_live",
            f"https://x.com/search?q={quote('Panathinaikos')}&src=typed_query&f=live",
        )
        await inspect_page(page, "paofc_profile", "https://x.com/paofc_")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
