"""Retired TransferFeed shim.

TransferFeed Panathinaikos direct monitoring now lives exclusively in the
Railway pao-news-watcher and delivers to Newspao News Alert. Keeping this tiny
module preserves PAO Fast imports/health compatibility without duplicate
polling or duplicate Telegram delivery.
"""


def main():
    print(
        "TransferFeed PAO Fast lane retired; Railway direct watcher is authoritative",
        flush=True,
    )


if __name__ == "__main__":
    main()
