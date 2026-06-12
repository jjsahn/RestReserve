# RestReserve

A Resy reservation sniper: books a table on **your own Resy account** the
instant time slots open. Designed for restaurants where reservations drop at a
fixed time and vanish within seconds.

## How it works

1. **Prewarm** — the moment you start it: logs in, resolves the venue,
   verifies your payment method, measures the clock offset against Resy's own
   servers, and opens a keep-alive HTTP/2 connection so the first real request
   pays no TLS handshake cost.
2. **Wait** — sleeps until `lead_ms` (default 1s) before the drop, pinging the
   connection every 30s to keep it warm, then busy-spins the final 50ms for
   millisecond precision.
3. **Snipe** — polls availability every 250ms (drift-free pacing), ranks slots
   in your time window by closeness to your ideal time, and books the best one.
   If someone steals a slot mid-booking (412), it instantly falls back to the
   next-best. Backs off politely on 429s. Gives up after 3 minutes with a full
   post-mortem of every slot it saw.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain pip).

```sh
uv venv && uv pip install -e .
cp config.example.json config.json   # config.json is gitignored
```

Edit `config.json`:

```json
{
  "resy": {
    "email": "you@example.com",
    "password": "...",
    "api_key": "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
  },
  "target": {
    "venue_slug": "some-restaurant",
    "location": "new-york-ny",
    "date": "2026-07-04",
    "party_size": 2,
    "window_start": "18:30",
    "window_end": "20:00",
    "ideal_time": "19:00"
  },
  "drop": { "time": "2026-06-13T09:00:00", "timezone": "America/New_York" }
}
```

Credentials can instead come from env vars: `RESY_EMAIL`, `RESY_PASSWORD`
(or `RESY_AUTH_TOKEN`, `RESY_API_KEY`).

## Usage

```sh
# 1. verify your credentials and payment method
uv run restreserve auth-test

# 2. find the venue's id/slug
uv run restreserve venue-search "restaurant name"

# 3. ALWAYS dry-run first — full pipeline, never books
uv run restreserve snipe --dry-run --drop-time "$(date -d '+2 minutes' -Iseconds)"

# 4. the real thing: start any time before the drop, it handles the rest
uv run restreserve snipe
```

Anything in the config can be overridden on the command line
(`--date`, `--party-size`, `--window 18:30-20:00`, `--venue-id`, ...).
Exit codes: `0` booked (or dry-run selected), `1` config/auth/venue error,
`2` timed out without booking.

## Notes & caveats

- **This uses Resy's private API** (the same one resy.com calls) with your own
  account. There is no official public API. Endpoints and response fields can
  change without notice — if something breaks, run with `-v` and compare
  against the requests resy.com makes in your browser's network tab.
- **The `api_key` is Resy's public web-client key**, not a personal secret. It
  rotates occasionally; if auth starts failing with 401s despite correct
  credentials, grab the current key from resy.com (DevTools → Network → any
  `api.resy.com` request → `Authorization` header) and update your config.
- **A payment method must be on your Resy account** — most venues require one
  even for free reservations.
- Keep `poll_interval_ms` at a civilized 250ms+. Hammering harder mostly risks
  rate-limiting you during the seconds that matter.
- Test with a real low-stakes booking before trusting it on a hard target.

## Development

```sh
uv pip install -e ".[dev]"
uv run pytest
```

All loop logic (slot ranking, backoff, fallback, re-auth, timing) is covered by
unit tests with mocked HTTP and fake clocks — no network needed.
