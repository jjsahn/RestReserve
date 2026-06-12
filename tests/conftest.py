"""Shared fixtures: canned Resy API payloads and a ready ResyClient."""

import pytest

from restreserve.resy_client import ResyClient

API_KEY = "test-api-key"

AUTH_RESPONSE = {
    "token": "auth-token-abc123",
    "payment_methods": [{"id": 4242, "is_default": True}],
}

VENUE_RESPONSE = {
    "id": {"resy": 1234},
    "name": "Test Restaurant",
}

VENUE_SEARCH_RESPONSE = {
    "search": {
        "hits": [
            {
                "name": "Test Restaurant",
                "id": {"resy": 1234},
                "url_slug": "test-restaurant",
                "location": {"code": "ny", "name": "New York"},
                "neighborhood": "SoHo",
            }
        ]
    }
}


def make_slot(token: str, start: str, table_type: str = "Dining Room") -> dict:
    return {
        "config": {"token": token, "type": table_type},
        "date": {"start": start, "end": start},
    }


FIND_RESPONSE = {
    "results": {
        "venues": [
            {
                "slots": [
                    make_slot("cfg-1830", "2026-07-04 18:30:00"),
                    make_slot("cfg-1900", "2026-07-04 19:00:00"),
                    make_slot("cfg-1930", "2026-07-04 19:30:00", "Patio"),
                ]
            }
        ]
    }
}

DETAILS_RESPONSE = {"book_token": {"value": "book-token-xyz"}}

BOOK_RESPONSE = {"resy_token": "resy-token-999", "reservation_id": 555}


@pytest.fixture
def client():
    c = ResyClient(API_KEY, timeout=1.0)
    yield c
    c.close()
