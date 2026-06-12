import datetime as dt

import httpx
import pytest
import respx

from restreserve.resy_client import (
    AuthError,
    PaymentRequiredError,
    RateLimitedError,
    ResyError,
    SlotGoneError,
    VenueNotFoundError,
)
from conftest import (
    AUTH_RESPONSE,
    BOOK_RESPONSE,
    DETAILS_RESPONSE,
    FIND_RESPONSE,
    VENUE_RESPONSE,
    VENUE_SEARCH_RESPONSE,
)

BASE = "https://api.resy.com"


@respx.mock
def test_authenticate_sets_token_and_payment(client):
    route = respx.post(f"{BASE}/3/auth/password").respond(json=AUTH_RESPONSE)
    session = client.authenticate("a@b.com", "pw")
    assert session.token == "auth-token-abc123"
    assert session.payment_method_id == 4242
    assert client.payment_method_id == 4242
    # subsequent requests carry the token header
    respx.get(f"{BASE}/3/venue").respond(json=VENUE_RESPONSE)
    client.resolve_venue("x", "y")
    sent = respx.calls.last.request
    assert sent.headers["x-resy-auth-token"] == "auth-token-abc123"
    assert 'api_key="test-api-key"' in route.calls[0].request.headers["authorization"]


@respx.mock
def test_authenticate_bad_credentials(client):
    respx.post(f"{BASE}/3/auth/password").respond(status_code=401, text="bad")
    with pytest.raises(AuthError):
        client.authenticate("a@b.com", "wrong")


@respx.mock
def test_authenticate_no_token_in_body(client):
    respx.post(f"{BASE}/3/auth/password").respond(json={"weird": True})
    with pytest.raises(AuthError, match="no token"):
        client.authenticate("a@b.com", "pw")


@respx.mock
def test_resolve_venue(client):
    respx.get(f"{BASE}/3/venue").respond(json=VENUE_RESPONSE)
    assert client.resolve_venue("test-restaurant", "ny") == 1234


@respx.mock
def test_resolve_venue_not_found(client):
    respx.get(f"{BASE}/3/venue").respond(status_code=404)
    with pytest.raises(VenueNotFoundError, match="venue-search"):
        client.resolve_venue("nope", "ny")


@respx.mock
def test_venue_search(client):
    respx.post(f"{BASE}/3/venuesearch/search").respond(json=VENUE_SEARCH_RESPONSE)
    hits = client.venue_search("test")
    assert hits == [
        {
            "name": "Test Restaurant",
            "venue_id": 1234,
            "slug": "test-restaurant",
            "location": "ny",
            "neighborhood": "SoHo",
        }
    ]


@respx.mock
def test_find_slots(client):
    respx.get(f"{BASE}/4/find").respond(json=FIND_RESPONSE)
    slots = client.find_slots(1234, dt.date(2026, 7, 4), 2)
    assert [s.config_token for s in slots] == ["cfg-1830", "cfg-1900", "cfg-1930"]
    assert slots[0].start == dt.datetime(2026, 7, 4, 18, 30)
    assert slots[2].table_type == "Patio"


@respx.mock
def test_find_slots_empty_venues(client):
    respx.get(f"{BASE}/4/find").respond(json={"results": {"venues": []}})
    assert client.find_slots(1234, dt.date(2026, 7, 4), 2) == []


@respx.mock
def test_find_slots_skips_malformed(client):
    payload = {
        "results": {"venues": [{"slots": [
            {"config": {}, "date": {"start": "2026-07-04 18:00:00"}},  # no token
            {"config": {"token": "ok"}, "date": {"start": "garbage"}},  # bad date
            {"config": {"token": "good"}, "date": {"start": "2026-07-04 19:00:00"}},
        ]}]}
    }
    respx.get(f"{BASE}/4/find").respond(json=payload)
    slots = client.find_slots(1234, dt.date(2026, 7, 4), 2)
    assert [s.config_token for s in slots] == ["good"]


@respx.mock
def test_get_book_token(client):
    respx.post(f"{BASE}/3/details").respond(json=DETAILS_RESPONSE)
    assert client.get_book_token("cfg-1900", dt.date(2026, 7, 4), 2) == "book-token-xyz"


@respx.mock
def test_get_book_token_slot_gone(client):
    respx.post(f"{BASE}/3/details").respond(status_code=412)
    with pytest.raises(SlotGoneError):
        client.get_book_token("cfg-1900", dt.date(2026, 7, 4), 2)


@respx.mock
def test_book_success(client):
    route = respx.post(f"{BASE}/3/book").respond(json=BOOK_RESPONSE)
    payload = client.book("book-token-xyz", 4242)
    assert payload["reservation_id"] == 555
    body = route.calls[0].request.content.decode()
    assert "book_token=book-token-xyz" in body
    assert "4242" in body


@respx.mock
def test_book_slot_gone(client):
    respx.post(f"{BASE}/3/book").respond(status_code=412)
    with pytest.raises(SlotGoneError):
        client.book("book-token-xyz", 4242)


@respx.mock
def test_book_payment_error(client):
    respx.post(f"{BASE}/3/book").respond(status_code=400,
                                         text="payment method required")
    with pytest.raises(PaymentRequiredError):
        client.book("book-token-xyz", None)


@respx.mock
def test_rate_limit_carries_retry_after(client):
    respx.get(f"{BASE}/4/find").respond(status_code=429,
                                        headers={"Retry-After": "2.5"})
    with pytest.raises(RateLimitedError) as exc:
        client.find_slots(1, dt.date(2026, 7, 4), 2)
    assert exc.value.retry_after == 2.5


@respx.mock
def test_rate_limit_without_retry_after(client):
    respx.get(f"{BASE}/4/find").respond(status_code=429)
    with pytest.raises(RateLimitedError) as exc:
        client.find_slots(1, dt.date(2026, 7, 4), 2)
    assert exc.value.retry_after is None


@respx.mock
def test_5xx_maps_to_resy_error(client):
    respx.get(f"{BASE}/4/find").respond(status_code=503)
    with pytest.raises(ResyError):
        client.find_slots(1, dt.date(2026, 7, 4), 2)


@respx.mock
def test_server_date(client):
    respx.get(f"{BASE}/4/find").respond(
        json={}, headers={"Date": "Fri, 12 Jun 2026 12:00:00 GMT"}
    )
    server, t_sent, t_received = client.server_date()
    assert server == dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert t_received >= t_sent
