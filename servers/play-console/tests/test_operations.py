"""RED tests: every operation maps to a real Android Publisher v3 path.

Table-driven so each endpoint's literal upstream path is asserted, and so a new
tool cannot be added without a path assertion.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from play_console_mcp.google_client import GoogleApiClient
from play_console_mcp.operations import Operations
from play_console_mcp.settings import Settings

PKG = "com.example.app"
BASE = "/androidpublisher/v3/applications/com.example.app"


@pytest.fixture
def ops(settings):
    client = GoogleApiClient(settings=settings, credentials=_FakeCreds())
    with respx.mock(base_url="https://androidpublisher.googleapis.com",
                    assert_all_called=False) as router:
        router.route().mock(return_value=Response(200, json={"ok": True}))
        yield Operations(client=client, settings=settings), router


class _FakeCreds:
    token = "t"
    valid = True
    expired = False


CASES = [
    ("list_reviews", {"package_name": PKG}, f"{BASE}/reviews"),
    ("get_review", {"package_name": PKG, "review_id": "r1"}, f"{BASE}/reviews/r1"),
    ("list_device_tier_configs", {"package_name": PKG}, f"{BASE}/deviceTierConfigs"),
    ("get_device_tier_config", {"package_name": PKG, "device_tier_config_id": 3},
     f"{BASE}/deviceTierConfigs/3"),
    ("list_in_app_products", {"package_name": PKG}, f"{BASE}/inappproducts"),
    ("get_in_app_product", {"package_name": PKG, "sku": "sku1"}, f"{BASE}/inappproducts/sku1"),
    ("list_subscriptions", {"package_name": PKG}, f"{BASE}/subscriptions"),
    ("get_subscription", {"package_name": PKG, "product_id": "sub1"}, f"{BASE}/subscriptions/sub1"),
    ("list_one_time_products", {"package_name": PKG}, f"{BASE}/oneTimeProducts"),
    ("get_one_time_product", {"package_name": PKG, "product_id": "otp1"},
     f"{BASE}/oneTimeProducts/otp1"),
    ("list_subscription_offers", {"package_name": PKG, "product_id": "sub1",
                                  "base_plan_id": "bp1"},
     f"{BASE}/subscriptions/sub1/basePlans/bp1/offers"),
    ("get_subscription_purchase_v2", {"package_name": PKG, "token": "tok"},
     f"{BASE}/purchases/subscriptionsv2/tokens/tok"),
    ("get_product_purchase", {"package_name": PKG, "product_id": "pid", "token": "tok"},
     f"{BASE}/purchases/products/pid/tokens/tok"),
    ("list_voided_purchases", {"package_name": PKG}, f"{BASE}/purchases/voidedpurchases"),
    ("get_order", {"package_name": PKG, "order_id": "GPA.1"}, f"{BASE}/orders/GPA.1"),
    ("list_app_recoveries", {"package_name": PKG}, f"{BASE}/appRecoveries"),
]


def test_case_table_covers_every_tool_spec():
    from play_console_mcp.operations import TOOL_SPECS

    assert {name for name, _, _ in CASES} == set(TOOL_SPECS)


@pytest.mark.parametrize("method,args,expected_path", CASES)
def test_operation_uses_expected_path(ops, method, args, expected_path):
    operations, router = ops
    getattr(operations, method)(**args)
    assert router.calls.last.request.url.path == expected_path
    assert router.calls.last.request.method == "GET"


@pytest.mark.parametrize("method,args,expected_path", CASES)
def test_operation_sends_only_get(ops, method, args, expected_path):
    operations, router = ops
    getattr(operations, method)(**args)
    assert {call.request.method for call in router.calls} == {"GET"}


def test_every_operation_refuses_non_allowlisted_package(settings):
    client = GoogleApiClient(settings=settings, credentials=_FakeCreds())
    with respx.mock(base_url="https://androidpublisher.googleapis.com",
                    assert_all_called=False) as router:
        router.route().mock(return_value=Response(200, json={}))
        operations = Operations(client=client, settings=settings)
        for method, args, _ in CASES:
            bad = dict(args, package_name="com.other.app")
            with pytest.raises(Exception, match="allowlist"):
                getattr(operations, method)(**bad)
        assert len(router.calls) == 0


def test_operations_query_parameters_are_correct(settings):
    client = GoogleApiClient(settings=settings, credentials=_FakeCreds())
    with respx.mock(base_url="https://androidpublisher.googleapis.com",
                    assert_all_called=False) as router:
        router.route().mock(return_value=Response(200, json={}))
        operations = Operations(client=client, settings=settings)

        operations.list_reviews(PKG, max_results=10, page_token="TOK")
        request = router.calls.last.request
        q = dict(request.url.params)
        # packageName is a path placeholder, so it must NOT be duplicated in the
        # query string; the API takes it from the path only.
        assert "packageName" not in q
        assert request.url.path.endswith(f"/applications/{PKG}/reviews")
        assert q["maxResults"] == "10"
        assert q["token"] == "TOK"

        operations.list_voided_purchases(PKG, start_time="1000", end_time="2000",
                                         purchase_type=1)
        q = dict(router.calls.last.request.url.params)
        assert q["startTime"] == "1000"
        assert q["endTime"] == "2000"
        assert q["type"] == "1"

        operations.list_subscription_offers(PKG, product_id="s", base_plan_id="b")
        request = router.calls.last.request
        q = dict(request.url.params)
        assert q["pageSize"] == "50"  # default clamped to settings.max_page_size
        assert "productId" not in q and "basePlanId" not in q
        assert request.url.path.endswith("/subscriptions/s/basePlans/b/offers")


def test_page_size_ignores_caller_supplied_oversize(settings):
    settings = Settings(packages=frozenset({PKG}), max_page_size=5)
    client = GoogleApiClient(settings=settings, credentials=_FakeCreds())
    with respx.mock(base_url="https://androidpublisher.googleapis.com",
                    assert_all_called=False) as router:
        router.route().mock(return_value=Response(200, json={}))
        operations = Operations(client=client, settings=settings)
        operations.list_subscriptions(PKG, max_results=1000)
        assert dict(router.calls.last.request.url.params)["pageSize"] == "5"