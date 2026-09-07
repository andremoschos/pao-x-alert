import asyncio

import fast_runner_v2 as base


async def _transferfeed_owned_by_direct_news():
    # TransferFeed is now owned exclusively by direct_news_free.  Keeping a
    # second sender here creates duplicate Telegram alerts for the same transfer.
    print("transferfeed_panathinaikos skipped: owned by PAO Direct News Free", flush=True)


base.transferfeed.main = _transferfeed_owned_by_direct_news


if __name__ == "__main__":
    asyncio.run(base.main())
