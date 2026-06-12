"""All HTTP communication with the Resy private API lives here.

Every public method maps Resy status codes onto the exception hierarchy below,
so callers (the sniper loop) branch on exception type, never on raw codes.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from email.utils import parsedate_to_datetime

import httpx

from restreserve.models import AuthSession, BookingResult, Slot

log = logging.getLogger(__name__)

# Mimic the web client; Resy rejects requests with no/odd User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ResyError(Exception):
    """Base for all Resy API errors."""


class AuthError(ResyError):
    """401: bad credentials or stale auth token."""


class VenueNotFoundError(ResyError):
    """Venue slug/location didn't resolve."""


class SlotGoneError(ResyError):
    """412 from /3/details or /3/book: someone else took the slot."""


class RateLimitedError(ResyError):
    """429; carries the server's Retry-After if present."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PaymentRequiredError(ResyError):
    """Booking rejected because a payment method is required/missing."""


class ResyClient:
    BASE = "https://api.resy.com"

    def __init__(self, api_key: str, timeout: float = 4.0) -> None:
        self._client = httpx.Client(
            base_url=self.BASE,
            http2=True,
            timeout=timeout,
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "X-Origin": "https://resy.com",
            },
            limits=httpx.Limits(max_keepalive_connections=2),
        )
        self.payment_method_id: int | None = None

    def close(self) -> None:
        self._client.close()

    # -- request plumbing ---------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        t0 = time.monotonic()
        response = self._client.request(method, url, **kwargs)
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.debug("%s %s -> %d (%.0fms)", method, url, response.status_code, elapsed_ms)
        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        code = response.status_code
        if code < 400:
            return
        body = response.text[:300]
        log.debug("error body: %s", body)
        if code == 401:
            raise AuthError(f"unauthorized (401): {body}")
        if code == 412:
            raise SlotGoneError(f"slot no longer available (412): {body}")
        if code == 429:
            retry_after = None
            header = response.headers.get("Retry-After")
            if header is not None:
                try:
                    retry_after = float(header)
                except ValueError:
                    pass
            raise RateLimitedError("rate limited (429)", retry_after=retry_after)
        if code == 404:
            raise VenueNotFoundError(f"not found (404): {body}")
        raise ResyError(f"Resy API error {code}: {body}")

    # -- auth ---------------------------------------------------------------

    def authenticate(self, email: str, password: str) -> AuthSession:
        response = self._request(
            "POST", "/3/auth/password", data={"email": email, "password": password}
        )
        data = response.json()
        token = data.get("token")
        if not token:
            raise AuthError("login succeeded but no token in response")
        session = AuthSession(
            token=token, payment_method_id=_extract_payment_method(data)
        )
        self.set_auth_token(session.token, session.payment_method_id)
        return session

    def set_auth_token(self, token: str, payment_method_id: int | None = None) -> None:
        self._client.headers["X-Resy-Auth-Token"] = token
        self._client.headers["X-Resy-Universal-Auth"] = token
        if payment_method_id is not None:
            self.payment_method_id = payment_method_id

    def fetch_payment_method(self) -> int | None:
        """Best-effort lookup when an auth token was supplied directly."""
        try:
            response = self._request("GET", "/2/user")
        except ResyError as e:
            log.debug("payment method lookup failed: %s", e)
            return None
        self.payment_method_id = _extract_payment_method(response.json())
        return self.payment_method_id

    # -- venue --------------------------------------------------------------

    def resolve_venue(self, url_slug: str, location: str) -> int:
        try:
            response = self._request(
                "GET", "/3/venue", params={"url_slug": url_slug, "location": location}
            )
        except VenueNotFoundError:
            raise VenueNotFoundError(
                f"venue not found for slug={url_slug!r} location={location!r}; "
                "try `restreserve venue-search`"
            ) from None
        data = response.json()
        venue_id = (data.get("id") or {}).get("resy")
        if venue_id is None:
            raise VenueNotFoundError(f"no venue id in response for {url_slug!r}")
        return int(venue_id)

    def venue_search(self, query: str) -> list[dict]:
        response = self._request(
            "POST",
            "/3/venuesearch/search",
            json={"query": query, "per_page": 10, "types": ["venue"]},
        )
        hits = response.json().get("search", {}).get("hits", [])
        results = []
        for hit in hits:
            results.append(
                {
                    "name": hit.get("name"),
                    "venue_id": (hit.get("id") or {}).get("resy"),
                    "slug": hit.get("url_slug"),
                    "location": hit.get("location", {}).get("code")
                    or hit.get("location", {}).get("name"),
                    "neighborhood": hit.get("neighborhood"),
                }
            )
        return results

    # -- slots & booking ----------------------------------------------------

    def find_slots(self, venue_id: int, day: dt.date, party_size: int) -> list[Slot]:
        response = self._request(
            "GET",
            "/4/find",
            params={
                "lat": 0,
                "long": 0,
                "day": day.isoformat(),
                "party_size": party_size,
                "venue_id": venue_id,
            },
        )
        venues = response.json().get("results", {}).get("venues", [])
        if not venues:
            return []
        slots = []
        for raw in venues[0].get("slots", []):
            config = raw.get("config", {})
            token = config.get("token")
            start_str = raw.get("date", {}).get("start")
            if not token or not start_str:
                continue
            try:
                start = dt.datetime.fromisoformat(start_str)
            except ValueError:
                log.debug("unparseable slot start %r", start_str)
                continue
            slots.append(
                Slot(
                    config_token=token,
                    start=start,
                    table_type=config.get("type") or "",
                    raw=raw,
                )
            )
        return slots

    def get_book_token(
        self, config_token: str, day: dt.date, party_size: int
    ) -> str:
        response = self._request(
            "POST",
            "/3/details",
            json={
                "commit": 1,
                "config_id": config_token,
                "day": day.isoformat(),
                "party_size": party_size,
            },
        )
        token = (response.json().get("book_token") or {}).get("value")
        if not token:
            raise SlotGoneError("no book_token in details response")
        return token

    def book(self, book_token: str, payment_method_id: int | None) -> dict:
        data = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if payment_method_id is not None:
            data["struct_payment_method"] = json.dumps({"id": payment_method_id})
        try:
            response = self._request("POST", "/3/book", data=data)
        except ResyError as e:
            text = str(e).lower()
            if "payment" in text and not isinstance(e, (SlotGoneError, AuthError)):
                raise PaymentRequiredError(str(e)) from e
            raise
        return response.json()

    def make_booking_result(self, payload: dict, slot: Slot) -> BookingResult:
        return BookingResult(
            resy_token=payload.get("resy_token", ""),
            reservation_id=payload.get("reservation_id"),
            slot=slot,
        )

    # -- connection warmth & clock sync --------------------------------------

    def warm(self) -> float:
        """Cheap request that opens/refreshes the TLS+H2 connection. Returns RTT seconds."""
        t0 = time.monotonic()
        try:
            self._client.get("/4/find", params={"lat": 0, "long": 0, "day": "2000-01-01",
                                                "party_size": 2, "venue_id": 1})
        except httpx.HTTPError as e:
            log.debug("warm request failed: %s", e)
        return time.monotonic() - t0

    def server_date(self) -> tuple[dt.datetime | None, float, float]:
        """One timestamped request -> (server Date header, t_sent, t_received).

        Times are local wall-clock (time.time()) so the caller can compare
        against the server's notion of UTC.
        """
        t_sent = time.time()
        response = self._client.get(
            "/4/find",
            params={"lat": 0, "long": 0, "day": "2000-01-01", "party_size": 2,
                    "venue_id": 1},
        )
        t_received = time.time()
        header = response.headers.get("Date")
        if not header:
            return None, t_sent, t_received
        try:
            server = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None, t_sent, t_received
        return server, t_sent, t_received


def _extract_payment_method(data: dict) -> int | None:
    """Pull the first payment method id from an auth or user payload."""
    methods = data.get("payment_methods") or []
    if methods and isinstance(methods, list):
        pm_id = methods[0].get("id")
        if pm_id is not None:
            return int(pm_id)
    pm = data.get("payment_method_id")
    if pm is not None:
        return int(pm)
    return None
