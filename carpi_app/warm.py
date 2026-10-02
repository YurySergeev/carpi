"""Pre-build the summary index and the prepared-frame cache so a hosted copy starts fast.

    python -m carpi_app.warm        (the Dockerfile runs this at build time)
"""
import time

from . import config
from .data import Store


def main():
    t = time.time()
    store = Store(config.LOG_DIRS)
    for did in store.drives:
        store.frame(did)
    print(f"Warmed {len(store.drives)} drives into {config.CACHE_DIR} in {time.time() - t:.1f} s")


if __name__ == "__main__":
    main()
