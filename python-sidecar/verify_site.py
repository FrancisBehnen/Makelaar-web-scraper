"""One-off verification: fetch a single site and run its parser.

Driven by .github/workflows/verify-site.yml to check a parser without
deploying to the VPS. Not imported by the scraper runtime.

Usage: python verify_site.py "<site name as in scraper.py SITES>"
"""

import os
import sys

os.environ.setdefault("DEBUG_DUMP", "true")

import scraper

name = sys.argv[1] if len(sys.argv) > 1 else "Nationaal Grondbezit"

site = next((s for s in scraper.SITES if s[0] == name), None)
if site is None:
    print(f"No site named {name!r}. Known sites:")
    for s in scraper.SITES:
        print(f"  - {s[0]}")
    sys.exit(1)

_, url, parser = site
print(f"Fetching {name}: {url}")
page = scraper._fetch_with_timeout(url)
body = page.body or b""
print(f"Fetched {len(body)} bytes")

houses = parser(page)
print(f"\n=== {len(houses)} listing(s) passed the Delft-area / price filter ===")
for h in houses:
    print(h)

# When parsing came up short, surface raw HTML so selectors can be tuned.
if not houses:
    text = body.decode("utf-8", errors="replace")
    marker = "huuraanbod/"
    idx = text.find(marker)
    if idx == -1:
        print("\nNo listing-link marker found. First 4000 chars of HTML:")
        print(text[:4000])
    else:
        print("\n=== HTML around the first listing link ===")
        print(text[max(0, idx - 2000):idx + 2000])
