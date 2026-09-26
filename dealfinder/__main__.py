"""Run the Deal Finder: `python -m dealfinder` serves the page, `python -m dealfinder hunt` runs one Hunt."""
import logging
import os
import sys

from .app import App
from .costs import DailyFx
from .sources import EbaySource


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sources = []
    if os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET"):
        sources.append(EbaySource(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]))
    else:
        logging.warning("EBAY_CLIENT_ID/EBAY_CLIENT_SECRET not set: eBay UK Source disabled")
    app = App(os.environ["DATABASE_URL"], sources, fx=DailyFx())
    if sys.argv[1:] == ["hunt"]:
        app.hunt()
        return
    server = app.make_server("0.0.0.0", int(os.environ.get("PORT", "8080")))
    app.hunt_in_background()
    logging.info("serving on %s", server.server_address)
    server.serve_forever()


if __name__ == "__main__":
    main()
