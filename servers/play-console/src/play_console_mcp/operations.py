"""Read-only Play Console operations.

Every function in this module is a thin, explicit read against a real Android
Publisher v3 endpoint. Tools take a ``package_name`` that is validated and
allowlisted before any request; nothing enumerates apps, because the Play API
has no "list my apps" endpoint that this server is permitted to call.
"""
from __future__ import annotations

from typing import Any, Callable

from .errors import LocalError
from .google_client import GoogleApiClient
from .settings import Settings
from .validation import (
    validate_integer_id,
    validate_page_token,
    validate_path_segment,
    validate_package_name,
)

__all__ = ["Operations", "TOOL_SPECS"]

_BASE = "/androidpublisher/v3/applications/{packageName}"


class Operations:
    """Read-only operations bound to a configured client and settings."""

    def __init__(self, *, client: GoogleApiClient, settings: Settings):
        self._client = client
        self._settings = settings

    # -- helpers -----------------------------------------------------------

    def _pkg(self, package_name: str) -> str:
        return self._settings.require_allowed_package(package_name)

    # -- reviews -----------------------------------------------------------

    def list_reviews(self, package_name: str, max_results: int | None = None,
                     page_token: str | None = None,
                     translation_language: str | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "maxResults": self._settings.clamp_page_size(max_results),
        }
        token = validate_page_token(page_token, "token")
        if token:
            params["token"] = token
        if translation_language:
            params["translationLanguage"] = validate_path_segment(
                translation_language, "translation_language"
            )
        return self._client.get(f"{_BASE}/reviews", params=params)

    def get_review(self, package_name: str, review_id: str,
                   translation_language: str | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        rid = validate_path_segment(review_id, "review_id")
        params: dict[str, Any] = {"packageName": pkg, "reviewId": rid}
        if translation_language:
            params["translationLanguage"] = validate_path_segment(
                translation_language, "translation_language"
            )
        return self._client.get(f"{_BASE}/reviews/{{reviewId}}", params=params)

    # -- store listings / tracks -------------------------------------------

    def list_device_tier_configs(self, package_name: str, max_results: int | None = None,
                                 page_token: str | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "pageSize": self._settings.clamp_page_size(max_results),
        }
        token = validate_page_token(page_token)
        if token:
            params["pageToken"] = token
        return self._client.get(f"{_BASE}/deviceTierConfigs", params=params)

    def get_device_tier_config(self, package_name: str, device_tier_config_id: int) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        cfg_id = validate_integer_id(device_tier_config_id, "device_tier_config_id")
        return self._client.get(
            f"{_BASE}/deviceTierConfigs/{{deviceTierConfigId}}",
            params={"packageName": pkg, "deviceTierConfigId": cfg_id},
        )

    # -- in-app products / subscriptions / one-time products ----------------

    def list_in_app_products(self, package_name: str, max_results: int | None = None,
                             token: str | None = None,
                             start_index: int | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "maxResults": self._settings.clamp_page_size(max_results),
        }
        page_token = validate_page_token(token, "token")
        if page_token:
            params["token"] = page_token
        if start_index is not None:
            params["startIndex"] = validate_integer_id(start_index, "start_index")
        return self._client.get(f"{_BASE}/inappproducts", params=params)

    def get_in_app_product(self, package_name: str, sku: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        validated = validate_path_segment(sku, "sku")
        return self._client.get(
            f"{_BASE}/inappproducts/{{sku}}",
            params={"packageName": pkg, "sku": validated},
        )

    def list_subscriptions(self, package_name: str, max_results: int | None = None,
                           page_token: str | None = None,
                           show_archived: bool = False) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "pageSize": self._settings.clamp_page_size(max_results),
            "showArchived": "true" if show_archived else "false",
        }
        token = validate_page_token(page_token)
        if token:
            params["pageToken"] = token
        return self._client.get(f"{_BASE}/subscriptions", params=params)

    def get_subscription(self, package_name: str, product_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        validated = validate_path_segment(product_id, "product_id")
        return self._client.get(
            f"{_BASE}/subscriptions/{{productId}}",
            params={"packageName": pkg, "productId": validated},
        )

    def list_one_time_products(self, package_name: str, max_results: int | None = None,
                               page_token: str | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "pageSize": self._settings.clamp_page_size(max_results),
        }
        token = validate_page_token(page_token)
        if token:
            params["pageToken"] = token
        return self._client.get(f"{_BASE}/oneTimeProducts", params=params)

    def get_one_time_product(self, package_name: str, product_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        validated = validate_path_segment(product_id, "product_id")
        return self._client.get(
            f"{_BASE}/oneTimeProducts/{{productId}}",
            params={"packageName": pkg, "productId": validated},
        )

    def list_subscription_offers(self, package_name: str, product_id: str,
                                 base_plan_id: str, max_results: int | None = None,
                                 page_token: str | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        product = validate_path_segment(product_id, "product_id")
        plan = validate_path_segment(base_plan_id, "base_plan_id")
        params: dict[str, Any] = {
            "packageName": pkg,
            "productId": product,
            "basePlanId": plan,
            "pageSize": self._settings.clamp_page_size(max_results),
        }
        token = validate_page_token(page_token)
        if token:
            params["pageToken"] = token
        return self._client.get(
            f"{_BASE}/subscriptions/{{productId}}/basePlans/{{basePlanId}}/offers",
            params=params,
        )

    # -- purchases / orders ------------------------------------------------
    #
    # NOTE: the v1 ``purchases.subscriptions.get`` endpoint this server used to
    # call (GET .../purchases/subscriptions/{subscriptionId}/tokens/{token}) is
    # no longer part of the official Android Publisher v3 discovery document.
    # Subscription purchase lookups are served by ``purchases.subscriptionsv2``
    # and one-time products by ``purchases.products``; both remain below.

    def get_subscription_purchase_v2(self, package_name: str, token: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        purchase_token = validate_path_segment(token, "token")
        return self._client.get(
            f"{_BASE}/purchases/subscriptionsv2/tokens/{{token}}",
            params={"packageName": pkg, "token": purchase_token},
        )

    def get_product_purchase(self, package_name: str, product_id: str,
                             token: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        product = validate_path_segment(product_id, "product_id")
        purchase_token = validate_path_segment(token, "token")
        return self._client.get(
            f"{_BASE}/purchases/products/{{productId}}/tokens/{{token}}",
            params={"packageName": pkg, "productId": product, "token": purchase_token},
        )

    def list_voided_purchases(self, package_name: str, max_results: int | None = None,
                              token: str | None = None,
                              start_time: str | None = None,
                              end_time: str | None = None,
                              purchase_type: int | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {
            "packageName": pkg,
            "maxResults": self._settings.clamp_page_size(max_results),
        }
        page_token = validate_page_token(token, "token")
        if page_token:
            params["token"] = page_token
        for key, value in (("startTime", start_time), ("endTime", end_time)):
            if value is not None:
                params[key] = validate_path_segment(value, key)
        if purchase_type is not None:
            params["type"] = validate_integer_id(purchase_type, "purchase_type")
        return self._client.get(f"{_BASE}/purchases/voidedpurchases", params=params)

    def get_order(self, package_name: str, order_id: str) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        validated = validate_path_segment(order_id, "order_id")
        return self._client.get(
            f"{_BASE}/orders/{{orderId}}",
            params={"packageName": pkg, "orderId": validated},
        )

    # -- app recovery ------------------------------------------------------

    def list_app_recoveries(self, package_name: str, version_code: int | None = None) -> dict[str, Any]:
        pkg = self._pkg(package_name)
        params: dict[str, Any] = {"packageName": pkg}
        if version_code is not None:
            params["versionCode"] = validate_integer_id(version_code, "version_code")
        return self._client.get(f"{_BASE}/appRecoveries", params=params)


# ---------------------------------------------------------------------------
# Tool specifications: name -> (callable attr, description, JSON schema)
# ---------------------------------------------------------------------------

_PKG = {
    "type": "string",
    "description": "Android package name, e.g. com.example.app. Must be allowlisted.",
}
_MAX = {
    "type": "integer",
    "description": "Maximum results per page. Bounded by the server's page-size cap.",
    "minimum": 1,
}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_reviews": {
        "attr": "list_reviews",
        "description": (
            "List user reviews for an app (GET applications/{packageName}/reviews). "
            "Read-only; paginated."
        ),
        "schema": _schema(
            {
                "package_name": _PKG,
                "max_results": _MAX,
                "page_token": {"type": "string", "description": "Opaque token from a prior page."},
                "translation_language": {
                    "type": "string",
                    "description": "BCP-47 language to translate review text into.",
                },
            },
            ["package_name"],
        ),
    },
    "get_review": {
        "attr": "get_review",
        "description": "Fetch a single review by id (GET .../reviews/{reviewId}).",
        "schema": _schema(
            {
                "package_name": _PKG,
                "review_id": {"type": "string", "description": "Review id from list_reviews."},
                "translation_language": {"type": "string"},
            },
            ["package_name", "review_id"],
        ),
    },
    "list_device_tier_configs": {
        "attr": "list_device_tier_configs",
        "description": "List device tier configs (GET .../deviceTierConfigs).",
        "schema": _schema(
            {"package_name": _PKG, "max_results": _MAX, "page_token": {"type": "string"}},
            ["package_name"],
        ),
    },
    "get_device_tier_config": {
        "attr": "get_device_tier_config",
        "description": "Fetch one device tier config (GET .../deviceTierConfigs/{id}).",
        "schema": _schema(
            {
                "package_name": _PKG,
                "device_tier_config_id": {"type": "integer", "minimum": 0},
            },
            ["package_name", "device_tier_config_id"],
        ),
    },
    "list_in_app_products": {
        "attr": "list_in_app_products",
        "description": "List legacy in-app products (GET .../inappproducts).",
        "schema": _schema(
            {
                "package_name": _PKG,
                "max_results": _MAX,
                "token": {"type": "string"},
                "start_index": {"type": "integer", "minimum": 0},
            },
            ["package_name"],
        ),
    },
    "get_in_app_product": {
        "attr": "get_in_app_product",
        "description": "Fetch one in-app product SKU (GET .../inappproducts/{sku}).",
        "schema": _schema({"package_name": _PKG, "sku": {"type": "string"}},
                          ["package_name", "sku"]),
    },
    "list_subscriptions": {
        "attr": "list_subscriptions",
        "description": "List subscription products (GET .../subscriptions).",
        "schema": _schema(
            {
                "package_name": _PKG,
                "max_results": _MAX,
                "page_token": {"type": "string"},
                "show_archived": {"type": "boolean", "default": False},
            },
            ["package_name"],
        ),
    },
    "get_subscription": {
        "attr": "get_subscription",
        "description": "Fetch one subscription product (GET .../subscriptions/{productId}).",
        "schema": _schema({"package_name": _PKG, "product_id": {"type": "string"}},
                          ["package_name", "product_id"]),
    },
    "list_one_time_products": {
        "attr": "list_one_time_products",
        "description": "List one-time products (GET .../oneTimeProducts).",
        "schema": _schema(
            {"package_name": _PKG, "max_results": _MAX, "page_token": {"type": "string"}},
            ["package_name"],
        ),
    },
    "get_one_time_product": {
        "attr": "get_one_time_product",
        "description": "Fetch one one-time product (GET .../oneTimeProducts/{productId}).",
        "schema": _schema({"package_name": _PKG, "product_id": {"type": "string"}},
                          ["package_name", "product_id"]),
    },
    "list_subscription_offers": {
        "attr": "list_subscription_offers",
        "description": (
            "List offers on a base plan "
            "(GET .../subscriptions/{productId}/basePlans/{basePlanId}/offers)."
        ),
        "schema": _schema(
            {
                "package_name": _PKG,
                "product_id": {"type": "string"},
                "base_plan_id": {"type": "string"},
                "max_results": _MAX,
                "page_token": {"type": "string"},
            },
            ["package_name", "product_id", "base_plan_id"],
        ),
    },
    "get_subscription_purchase_v2": {
        "attr": "get_subscription_purchase_v2",
        "description": "Verify a subscription purchase with the v2 API (GET .../subscriptionsv2/tokens/{token}).",
        "schema": _schema(
            {"package_name": _PKG, "token": {"type": "string"}},
            ["package_name", "token"],
        ),
    },
    "get_product_purchase": {
        "attr": "get_product_purchase",
        "description": (
            "Verify a one-time product purchase "
            "(GET .../purchases/products/{productId}/tokens/{token})."
        ),
        "schema": _schema(
            {
                "package_name": _PKG,
                "product_id": {"type": "string"},
                "token": {"type": "string"},
            },
            ["package_name", "product_id", "token"],
        ),
    },
    "list_voided_purchases": {
        "attr": "list_voided_purchases",
        "description": "List refunded/voided purchases (GET .../purchases/voidedpurchases).",
        "schema": _schema(
            {
                "package_name": _PKG,
                "max_results": _MAX,
                "token": {"type": "string"},
                "start_time": {"type": "string", "description": "Unix ms timestamp."},
                "end_time": {"type": "string", "description": "Unix ms timestamp."},
                "purchase_type": {"type": "integer", "minimum": 0},
            },
            ["package_name"],
        ),
    },
    "get_order": {
        "attr": "get_order",
        "description": "Fetch one order (GET .../orders/{orderId}).",
        "schema": _schema({"package_name": _PKG, "order_id": {"type": "string"}},
                          ["package_name", "order_id"]),
    },
    "list_app_recoveries": {
        "attr": "list_app_recoveries",
        "description": "List app recovery actions (GET .../appRecoveries).",
        "schema": _schema(
            {"package_name": _PKG, "version_code": {"type": "integer", "minimum": 0}},
            ["package_name"],
        ),
    },
}