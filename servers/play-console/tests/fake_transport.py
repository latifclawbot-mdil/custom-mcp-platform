"""Deterministic in-process fake of the Android Publisher API — test-only.

This module lives under ``tests/`` on purpose. It is *not* shipped in the
``play_console_mcp`` wheel and the server has no import path to it, so an
installed server can never be coaxed into returning fabricated data by setting
an environment variable. The stdio end-to-end tests import it directly via
``tests/_fake_stdio_server.py``.

It is deliberately strict: unknown paths 404, and it records the request
path/query so tests can assert real API shapes.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

__all__ = ["FakeTransport", "FakeCredentials"]

_ALLOWED_PREFIX = "/androidpublisher/v3/"

# path regex -> stub payload
_ROUTES: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/reviews$"),
     {"reviews": [{"reviewId": "rev-1", "authorName": "A"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/reviews/(?P<rid>[^/]+)$"),
     {"reviewId": "rev-1", "comments": []}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/deviceTierConfigs$"),
     {"deviceTierConfigs": [{"deviceTierConfigId": "1"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/"
                r"deviceTierConfigs/(?P<i>\d+)$"),
     {"deviceTierConfigId": "1"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/inappproducts$"),
     {"inappproduct": [{"sku": "sku-1"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/inappproducts/(?P<sku>[^/]+)$"),
     {"sku": "sku-1"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/subscriptions$"),
     {"subscriptions": [{"productId": "sub-1"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/"
                r"subscriptions/(?P<pid>[^/]+)/basePlans/(?P<bp>[^/]+)/offers$"),
     {"subscriptionOffers": [{"offerId": "offer-1"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/subscriptions/(?P<pid>[^/]+)$"),
     {"productId": "sub-1"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/oneTimeProducts$"),
     {"oneTimeProducts": [{"productId": "otp-1"}]}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/oneTimeProducts/(?P<pid>[^/]+)$"),
     {"productId": "otp-1"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/"
                r"purchases/subscriptionsv2/tokens/(?P<tok>[^/]+)$"),
     {"subscriptionState": "SUBSCRIPTION_STATE_ACTIVE"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/"
                r"purchases/products/(?P<pid>[^/]+)/tokens/(?P<tok>[^/]+)$"),
     {"purchaseState": 0, "consumptionState": 0}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/purchases/voidedpurchases$"),
     {"voidedPurchases": []}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/orders/(?P<oid>[^/]+)$"),
     {"orderId": "GPA.1"}),
    (re.compile(r"^/androidpublisher/v3/applications/(?P<pkg>[^/]+)/appRecoveries$"),
     {"recoveryActions": []}),
]

# Packages whose fake upstream always denies access.
_DENIED_PACKAGES = {"com.example.denied"}


class FakeTransport(httpx.BaseTransport):
    """Strict, recording httpx transport that answers GETs with static JSON."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.requests.append((request.method, path, query))

        if request.method != "GET":
            return httpx.Response(405, json={"error": {"message": "method not allowed"}})
        if not path.startswith(_ALLOWED_PREFIX):
            return httpx.Response(404, json={"error": {"message": "not found"}})

        pkg = _package_of(path)
        if pkg in _DENIED_PACKAGES:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Caller does not have permission",
                        "status": "PERMISSION_DENIED",
                        "details": [{"token": "ya29.SUPERSECRET", "reason": "denied"}],
                    }
                },
            )

        for pattern, payload in _ROUTES:
            if pattern.match(path):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": {"message": "not found"}})


def _package_of(path: str) -> str | None:
    match = re.match(r"^/androidpublisher/v3/applications/([^/]+)/", path)
    return match.group(1) if match else None


class FakeCredentials:
    """Stand-in credential used only with the fake transport in tests."""

    token = "fake-access-token"
    valid = True
    expired = False
    quota_project_id = None
    scopes = ["https://www.googleapis.com/auth/androidpublisher"]

    def refresh(self, request: Any = None) -> None:  # pragma: no cover
        self.token = "fake-access-token"
