import asyncio

import fast_runner_v2 as base
import instagram_monitor as instagram


async def _transferfeed_owned_by_railway():
    # TransferFeed direct-news delivery is owned exclusively by the Railway
    # production service. Keeping a second sender here would duplicate alerts.
    print("transferfeed_panathinaikos skipped: owned by Railway DIRECT WEBSITE NEWS", flush=True)


base.transferfeed.main = _transferfeed_owned_by_railway

for state_name in ("instagram_seen.json", "instagram_health.json"):
    if state_name not in base.STATE_FILES:
        base.STATE_FILES.append(state_name)


async def _instagram_loop():
    while True:
        try:
            instagram.main()
        except Exception as exc:
            print(
                f"instagram_pao: ERROR {type(exc).__name__}: {exc}",
                flush=True,
            )
        await asyncio.sleep(120)


async def _main():
    await asyncio.gather(
        base.main(),
        _instagram_loop(),
    )


if __name__ == "__main__":
    asyncio.run(_main())
