# Adding sites to the Python sidecar

Dense reference for non-obvious gotchas. Read before writing a new parser.

## Scrapling response API

- `page.body` is **bytes**, not a Scrapling element. Write it directly to disk for debugging; don't call `.get()` on it.
- `element.text` returns only the **direct text node**, ignoring children. A `<div>` wrapping a `<span>` with the actual text will return whitespace. Use `element.get_all_text()` for recursive text, or target the inner element with a more specific selector.
- `element.attrib` is a dict — use `element.attrib.get("href", "")`, not `element["href"]`.
- `page.css(sel)` always returns a **list-like** (even for zero matches). Index into it; don't treat it as a single element.
- CSS pseudo-selectors (`::text`, `::attr(href)`) work on Scrapling selection results but behave differently from plain `.text`/`.attrib` access. For single-element extraction, `.text` and `.attrib` are more predictable.

## Selector development workflow

1. Set `DEBUG_DUMP=true` and run a single cycle. Raw HTML lands in `data/debug/<name>.html`.
2. Test selectors against cached HTML using `Adaptor(html, url=...)` — avoids re-fetching during iteration.
3. The `Adaptor` class (from `scrapling.parser`) doesn't have a `.body` attribute. If testing `dump_html()` offline, wrap it or skip the dump.

## Common HTML traps

- **Nested price elements**: Pararius puts the price in `.listing-search-item__price-main` (a `<span>`) inside `.listing-search-item__price` (a `<div>`). The parent's `.text` is whitespace. Always inspect the actual HTML before assuming the obvious selector has the text.
- **Icon-only features**: Funda shows rooms as a bare number ("2") next to an SVG bed icon — no "kamer" text. Identify by position or by checking `.isdigit()`. Energy labels ("B", "A++") are also bare text with icons.
- **No semantic containers**: Funda uses Tailwind utility classes, no stable BEM-style class names. The only reliable anchor is `[data-testid="listingDetailsAddress"]`. Walk up with `.parent` (4 levels) to reach the card. This will break if Funda restructures — the `data-testid` is the most stable hook.
- **Attribute naming**: `data-testid` (Funda) vs `data-test-id` — one character difference, completely different selector. Always verify with `page.css('[data-testid]')` first.

## Fetching & anti-bot

- `solve_cloudflare=True` is safe on sites without Cloudflare — it just logs "No Cloudflare challenge found" (misleadingly at ERROR level).
- `network_idle=True` is critical for JS-rendered pages (Funda, most modern sites). Without it you get a skeleton HTML with no listings.
- `headless=True` is required in Docker (no display server). Locally you can set it to `False` to watch the browser for debugging.
- StealthyFetcher launches a full browser per `.fetch()` call. Two sites = two cold browser startups (~10-15s each on the VPS). If adding many sites, consider `StealthySession` to reuse a browser instance across fetches.

## Database contract

- Schema is defined by the Bun app: `houses(url PK, straatnaamHuisnummer, plaats, vraagprijs, oppervlakte, kamers)` — all `TEXT`.
- `url` must be **absolute** (prefix with base URL if the page gives relative hrefs).
- WAL journal mode is set by the sidecar for safe concurrent reads with the Bun app. Don't change it.
- The sidecar uses `INSERT OR REPLACE` — if a listing URL already exists, it overwrites. This means price/feature updates are captured but the listing won't be treated as new again (the URL is already known).

## Telegram

- The sidecar does **not** send listing notifications. Parsers just return the dict that is stored in the `houses` table; the separate responder service watches the DB and handles all listing-facing Telegram messaging.
- The sidecar still sends its own **operational alerts** (fetch timeouts, self-restarts) via `send_telegram_alert`, using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_IDS` (falling back to `TELEGRAM_CHAT_IDS`).
- New-site parsers therefore only need to fill the six DB fields correctly — there is no message format to match.

## Contact detection (responder) — usually nothing to do

Contact detection is **not** part of the parser. The responder service
(`responder/detection.py`) runs `detect()` live on every new listing URL,
regardless of which site it came from or when that site was added — there is
no per-site contact registry or allowlist. So adding a parser does **not**
require wiring up any contact info, and a site added "after" the responder
went live is treated exactly the same as any other.

`detect()` tries, in order: a fillable form on the listing page (a `<textarea>`
plus an email input), a published e-mail / `mailto:` address, a same-domain
contact page, then an apply link to a known external rental platform.

When adding a site, only touch detection in **one** case:

- **The site outsources applications to an external rental platform** not yet
  listed in `EXTERNAL_PLATFORMS` (e.g. eazlee, huurwoningen.nl, ikwilhuren.nu,
  woningnet, huurportaal, leadflow). Add the new platform's domain there so the
  responder reports a "🌐 Reageren via" link instead of "unknown".

For everything else, leave detection alone. In particular, **account-gated
national landlords are expected to come back as "Geen contactmethode
gevonden"**. Vesteda is the canonical example: its "reageren/inschrijven" flow
is an account-gated SPA on its own domain with no published inbox and no plain
contact form, so "unknown" is the correct result, not a bug — the parser author
needs to do nothing about it.

### Form filling is generic too — no per-site fillers

When detection finds a `form`, `form_filler.py` fills it with generic
Dutch/English keyword heuristics (name/email/phone/subject/message, privacy
checkboxes, required selects). You do **not** write a per-site form filler.

The one trap worth knowing: some makelaars (e.g. MVGM / ikwilhuren.nu) use a
**generic contact form with a "which building/complex" `<select>`** holding
hundreds of options. The filler treats a select as a property selector when its
label matches `complex|object|pand|vestiging` *or* it has a very long option
list, and it picks the option whose text contains the listing's street (city as
tie-breaker). If it can't confidently match — the common case, since a street
name rarely equals a complex name — it **bails the whole form to manual**
("reageer handmatig") rather than submit the wrong/"Algemeen" building. That is
the intended behaviour: better a manual nudge than a response about the wrong
listing. New parsers need to do nothing here; this is handled entirely in the
responder.

## Deployment

- The sidecar image is built locally on the VPS (not from GHCR). After code changes: SCP the updated files to `/docker/makelaar-scraper/python-sidecar/`, then `sudo docker compose up -d --build scraper-sidecar`.
- The Bun app image comes from GHCR via CI. The two have independent deployment paths.
- Both containers share the `scraper-data` volume mounted at `/app/data`.
