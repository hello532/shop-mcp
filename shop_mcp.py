#!/usr/bin/env python3
"""
shop-mcp - a Model Context Protocol server exposing a Shopify catalogue and
inventory to an LLM agent, over stdio.

Standard library only. No MCP SDK, no requests, no GraphQL client.

Why write the protocol by hand: the stdio transport has a small number of ways
to fail that are invisible until an agent is already talking to you, and all of
them live in code an SDK hides. They are enumerated in README.md and each one
has an assertion in --self-test.

Run:
    SHOPIFY_SHOP=your-shop.myshopify.com \
    SHOPIFY_ADMIN_TOKEN=shpat_... \
    python3 shop_mcp.py

Verify without credentials (the whole protocol layer is exercised):
    python3 shop_mcp.py --self-test
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# --------------------------------------------------------------------------
# Protocol constants. Sourced from the MCP specification's own type
# definitions rather than from memory.
# --------------------------------------------------------------------------

SERVER_NAME = "shop-mcp"
SERVER_VERSION = "1.0.0"

# The newest revision this server implements.
LATEST_PROTOCOL_VERSION = "2025-11-25"
# Revisions we will speak if a client asks for them by name.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
# What to answer when a client names a revision we do not know. Answering with
# our own latest is wrong: the client then has to downgrade or fail. The spec's
# default negotiated revision is the safe floor.
DEFAULT_NEGOTIATED_VERSION = "2025-03-26"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SHOPIFY_API_VERSION = "2025-10"

# Process exit codes, distinct so a supervisor can tell configuration
# problems from crashes without parsing output.
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2


class ConfigError(Exception):
    """Missing or unusable configuration. Fatal before the loop starts."""


class ShopifyError(Exception):
    """The Shopify API refused or failed a request."""


# --------------------------------------------------------------------------
# Logging
#
# Every diagnostic goes to stderr. Nothing but JSON-RPC may ever reach stdout:
# on a stdio transport, stdout IS the wire, and one stray print corrupts the
# next message the client tries to parse. This is the single most common way a
# hand-written stdio server fails, and it fails confusingly, because the server
# looks healthy from the inside.
# --------------------------------------------------------------------------


def log(message: str) -> None:
    """Write a diagnostic to stderr. Never to stdout."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Shopify Admin GraphQL transport
# --------------------------------------------------------------------------


class ShopifyClient:
    """
    Minimal Admin GraphQL client over urllib.

    Handles the two failure modes that actually happen in production:
    the leaky bucket rate limiter, and 5xx/429 responses that deserve a retry
    rather than an exception.
    """

    def __init__(
        self,
        shop: str,
        token: str,
        api_version: str = SHOPIFY_API_VERSION,
        max_retries: int = 4,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not shop:
            raise ConfigError("SHOPIFY_SHOP is not set (expected your-shop.myshopify.com)")
        if not token:
            raise ConfigError("SHOPIFY_ADMIN_TOKEN is not set")
        self.shop = shop.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        self.token = token.strip()
        self.api_version = api_version
        self.max_retries = max_retries
        # Injectable so the retry path is testable without real waiting.
        self.sleep = sleeper if sleeper is not None else time.sleep
        self.endpoint = f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    # -- transport seam -------------------------------------------------
    # Overridden wholesale in tests. Everything above this line is real
    # network code; everything below the seam is pure logic.

    def _post(self, payload: bytes) -> tuple[int, bytes]:
        """POST the payload, return (status, body). The only network call."""
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": self.token,
                "Accept": "application/json",
                "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except urllib.error.URLError as exc:
            raise ShopifyError(f"cannot reach {self.shop}: {exc.reason}") from exc

    # -- request logic --------------------------------------------------

    def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Run a GraphQL document and return its `data`.

        Raises ShopifyError with the API's own message on a refusal, so the
        tool layer can hand the agent something it can act on.
        """
        payload = json.dumps(
            {"query": document, "variables": variables or {}}, separators=(",", ":")
        ).encode("utf-8")

        attempt = 0
        while True:
            attempt += 1
            status, raw = self._post(payload)

            if status in (429, 500, 502, 503, 504) and attempt <= self.max_retries:
                delay = min(2.0 ** (attempt - 1), 8.0)
                log(f"HTTP {status} from Shopify, retry {attempt}/{self.max_retries} in {delay:.1f}s")
                self.sleep(delay)
                continue

            if status == 401:
                raise ShopifyError(
                    "Shopify rejected the token (401). Check SHOPIFY_ADMIN_TOKEN and "
                    "that the app has the scopes this tool needs."
                )
            if status != 200:
                raise ShopifyError(f"Shopify returned HTTP {status}: {raw[:200].decode('utf-8', 'replace')}")

            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ShopifyError(f"Shopify returned a body that is not JSON: {exc}") from exc

            # A throttled query returns 200 with an error, not 429. Retrying
            # without waiting for the bucket to refill just burns the retry.
            if self._is_throttled(body):
                if attempt > self.max_retries:
                    raise ShopifyError("still throttled after the last retry; lower the page size")
                delay = self._refill_delay(body)
                log(f"throttled by the cost limiter, waiting {delay:.1f}s (retry {attempt}/{self.max_retries})")
                self.sleep(delay)
                continue

            if body.get("errors"):
                raise ShopifyError(self._describe_errors(body["errors"]))

            data = body.get("data")
            if data is None:
                raise ShopifyError("Shopify returned no data and no errors")
            return data

    # -- response interpretation ----------------------------------------

    @staticmethod
    def _is_throttled(body: dict[str, Any]) -> bool:
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                code = (err.get("extensions") or {}).get("code")
                if code == "THROTTLED":
                    return True
        return False

    @staticmethod
    def _refill_delay(body: dict[str, Any]) -> float:
        """
        Work out how long to wait from the cost extension the API sends back,
        rather than guessing a fixed sleep.
        """
        cost = (body.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        available = status.get("currentlyAvailable")
        rate = status.get("restoreRate")
        requested = cost.get("requestedQueryCost")
        if isinstance(available, (int, float)) and isinstance(rate, (int, float)) and rate > 0:
            need = requested if isinstance(requested, (int, float)) else 0
            missing = max(float(need) - float(available), 0.0)
            # A floor of half a second: a zero wait would spin.
            return max(missing / float(rate), 0.5)
        return 2.0

    @staticmethod
    def _describe_errors(errors: Any) -> str:
        """Flatten GraphQL errors into one line an agent can read."""
        if not isinstance(errors, list):
            return str(errors)
        parts = []
        for err in errors:
            if isinstance(err, dict):
                msg = err.get("message", "unknown error")
                code = (err.get("extensions") or {}).get("code")
                parts.append(f"{msg} [{code}]" if code else str(msg))
            else:
                parts.append(str(err))
        return "; ".join(parts) or "unknown GraphQL error"


# --------------------------------------------------------------------------
# GraphQL documents
# --------------------------------------------------------------------------

PRODUCT_FIELDS = """
    id
    handle
    title
    status
    vendor
    productType
    totalInventory
    onlineStoreUrl
"""

SEARCH_PRODUCTS = """
query SearchProducts($q: String, $n: Int!) {
  products(first: $n, query: $q, sortKey: RELEVANCE) {
    edges { node { %s } }
  }
}
""" % PRODUCT_FIELDS

GET_PRODUCT_BY_HANDLE = """
query GetProductByHandle($handle: String!) {
  productByIdentifier(identifier: {handle: $handle}) {
    %s
    description
    variants(first: 100) {
      edges { node { id sku title price inventoryQuantity availableForSale } }
    }
  }
}
""" % PRODUCT_FIELDS

GET_PRODUCT_BY_ID = """
query GetProductById($id: ID!) {
  product(id: $id) {
    %s
    description
    variants(first: 100) {
      edges { node { id sku title price inventoryQuantity availableForSale } }
    }
  }
}
""" % PRODUCT_FIELDS

# quantities(names:) is the current shape. The old `available` scalar on
# InventoryLevel was removed, so an older query silently returns nothing here.
INVENTORY_BY_SKU = """
query InventoryBySku($q: String!, $n: Int!) {
  productVariants(first: $n, query: $q) {
    edges {
      node {
        id
        sku
        displayName
        inventoryQuantity
        inventoryItem {
          id
          tracked
          inventoryLevels(first: 20) {
            edges {
              node {
                location { id name }
                quantities(names: ["available", "committed", "on_hand"]) {
                  name
                  quantity
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

LOW_STOCK = """
query LowStock($n: Int!) {
  productVariants(first: $n, query: "inventory_quantity:<=0 OR inventory_quantity:>0") {
    edges {
      node {
        id
        sku
        displayName
        inventoryQuantity
        product { id handle title status }
      }
    }
  }
}
"""


def _edges(node: Any, *path: str) -> list[dict[str, Any]]:
    """
    Walk `edges[].node` containers defensively.

    A partial GraphQL response is normal when a field group is denied by a
    missing scope, so a missing key is an empty list rather than a crash.
    """
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(key)
    if not isinstance(cur, dict):
        return []
    out = []
    for edge in cur.get("edges") or []:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            out.append(edge["node"])
    return out


# --------------------------------------------------------------------------
# Tools
#
# Each tool returns a plain dict. The protocol layer is what turns that into
# CallToolResult content, so tool code has no protocol knowledge and the
# protocol has no Shopify knowledge.
# --------------------------------------------------------------------------


class Tools:
    def __init__(self, client: ShopifyClient) -> None:
        self.client = client

    # -- descriptors ----------------------------------------------------

    def descriptors(self) -> list[dict[str, Any]]:
        """
        The tools/list payload.

        Descriptions are written for the model, not for a human reading docs:
        they say when to reach for the tool and what it costs, because that is
        what the model is choosing between.
        """
        return [
            {
                "name": "search_products",
                "description": (
                    "Find products in the store by free text, vendor, or status. "
                    "Returns identity and total inventory only, not per-variant "
                    "detail; call get_product for that. Use this first when you "
                    "do not already know a handle."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Shopify search syntax, e.g. 'title:shirt', "
                                "'vendor:Acme', 'status:ACTIVE', or plain words."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum products to return, 1-50.",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_product",
                "description": (
                    "Read one product in full, including every variant with its "
                    "SKU, price and stock level. Identify it by handle or by "
                    "gid://shopify/Product/... id. Exactly one of the two."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string", "description": "URL handle, e.g. 'blue-shirt'."},
                        "id": {"type": "string", "description": "gid://shopify/Product/1234567890"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "check_inventory",
                "description": (
                    "Stock for a SKU broken down by location, with available, "
                    "committed and on-hand counts. Use when the question is "
                    "'can we ship it' rather than 'do we sell it'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string", "description": "Exact SKU to look up."},
                        "limit": {
                            "type": "integer",
                            "description": "Maximum matching variants, 1-50.",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 10,
                        },
                    },
                    "required": ["sku"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "low_stock_report",
                "description": (
                    "Variants at or below a stock threshold, lowest first. Use "
                    "for restock questions. Scans up to `scan` variants and "
                    "filters locally, so raise `scan` for a large catalogue."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "threshold": {
                            "type": "integer",
                            "description": "Report variants with stock <= this. Default 5.",
                            "default": 5,
                        },
                        "scan": {
                            "type": "integer",
                            "description": "Variants to examine, 1-250. Default 100.",
                            "minimum": 1,
                            "maximum": 250,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        ]

    # -- dispatch -------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "search_products": self.search_products,
            "get_product": self.get_product,
            "check_inventory": self.check_inventory,
            "low_stock_report": self.low_stock_report,
        }.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        return handler(args)

    # -- validation helpers ---------------------------------------------

    @staticmethod
    def _clamp_int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
        """
        Coerce and clamp rather than reject.

        A model that sends limit=1000 wants "a lot"; failing the call teaches
        it nothing and costs a turn. Out-of-range is clamped; a non-integer is
        a real mistake and is rejected.
        """
        raw = args.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            if raw is None:
                return default
            raise ValueError(f"{key} must be an integer, got {type(raw).__name__}")
        return max(low, min(high, int(raw)))

    @staticmethod
    def _require_str(args: dict[str, Any], key: str) -> str:
        val = args.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} is required and must be a non-empty string")
        return val.strip()

    # -- implementations -------------------------------------------------

    def search_products(self, args: dict[str, Any]) -> dict[str, Any]:
        query = self._require_str(args, "query")
        limit = self._clamp_int(args, "limit", 10, 1, 50)
        data = self.client.query(SEARCH_PRODUCTS, {"q": query, "n": limit})
        products = _edges(data, "products")
        return {
            "query": query,
            "count": len(products),
            "products": [
                {
                    "id": p.get("id"),
                    "handle": p.get("handle"),
                    "title": p.get("title"),
                    "status": p.get("status"),
                    "vendor": p.get("vendor"),
                    "product_type": p.get("productType"),
                    "total_inventory": p.get("totalInventory"),
                    "url": p.get("onlineStoreUrl"),
                }
                for p in products
            ],
        }

    def get_product(self, args: dict[str, Any]) -> dict[str, Any]:
        handle = args.get("handle")
        pid = args.get("id")
        if bool(handle) == bool(pid):
            raise ValueError("pass exactly one of handle or id")

        if handle:
            data = self.client.query(GET_PRODUCT_BY_HANDLE, {"handle": str(handle).strip()})
            product = data.get("productByIdentifier")
            ref = f"handle={handle}"
        else:
            gid = str(pid).strip()
            if not gid.startswith("gid://shopify/Product/"):
                raise ValueError("id must look like gid://shopify/Product/1234567890")
            data = self.client.query(GET_PRODUCT_BY_ID, {"id": gid})
            product = data.get("product")
            ref = f"id={gid}"

        if not isinstance(product, dict):
            return {"found": False, "looked_up": ref}

        variants = _edges(product, "variants")
        return {
            "found": True,
            "id": product.get("id"),
            "handle": product.get("handle"),
            "title": product.get("title"),
            "status": product.get("status"),
            "vendor": product.get("vendor"),
            "product_type": product.get("productType"),
            "description": product.get("description"),
            "total_inventory": product.get("totalInventory"),
            "url": product.get("onlineStoreUrl"),
            "variant_count": len(variants),
            "variants": [
                {
                    "id": v.get("id"),
                    "sku": v.get("sku"),
                    "title": v.get("title"),
                    "price": v.get("price"),
                    "stock": v.get("inventoryQuantity"),
                    "available_for_sale": v.get("availableForSale"),
                }
                for v in variants
            ],
        }

    def check_inventory(self, args: dict[str, Any]) -> dict[str, Any]:
        sku = self._require_str(args, "sku")
        limit = self._clamp_int(args, "limit", 10, 1, 50)
        # Quote the SKU: an unquoted one containing a space or colon would be
        # read as extra search terms and silently match the wrong variants.
        escaped = sku.replace("\\", "\\\\").replace('"', '\\"')
        data = self.client.query(INVENTORY_BY_SKU, {"q": f'sku:"{escaped}"', "n": limit})
        variants = _edges(data, "productVariants")

        results = []
        for v in variants:
            item = v.get("inventoryItem") or {}
            locations = []
            for level in _edges(item, "inventoryLevels"):
                counts = {
                    q.get("name"): q.get("quantity")
                    for q in (level.get("quantities") or [])
                    if isinstance(q, dict)
                }
                loc = level.get("location") or {}
                locations.append(
                    {
                        "location": loc.get("name"),
                        "location_id": loc.get("id"),
                        "available": counts.get("available"),
                        "committed": counts.get("committed"),
                        "on_hand": counts.get("on_hand"),
                    }
                )
            results.append(
                {
                    "variant_id": v.get("id"),
                    "sku": v.get("sku"),
                    "name": v.get("displayName"),
                    "total_stock": v.get("inventoryQuantity"),
                    "tracked": item.get("tracked"),
                    "locations": locations,
                }
            )

        return {"sku": sku, "count": len(results), "variants": results}

    def low_stock_report(self, args: dict[str, Any]) -> dict[str, Any]:
        threshold = self._clamp_int(args, "threshold", 5, -(10**6), 10**6)
        scan = self._clamp_int(args, "scan", 100, 1, 250)
        data = self.client.query(LOW_STOCK, {"n": scan})
        variants = _edges(data, "productVariants")

        low = []
        for v in variants:
            qty = v.get("inventoryQuantity")
            if not isinstance(qty, int) or qty > threshold:
                continue
            product = v.get("product") or {}
            low.append(
                {
                    "sku": v.get("sku"),
                    "name": v.get("displayName"),
                    "stock": qty,
                    "product_handle": product.get("handle"),
                    "product_title": product.get("title"),
                    "product_status": product.get("status"),
                }
            )
        low.sort(key=lambda r: r["stock"])
        return {
            "threshold": threshold,
            "scanned": len(variants),
            "count": len(low),
            # Say so explicitly: "0 low" and "did not look far enough" are
            # different answers and the model cannot tell them apart otherwise.
            "scan_exhausted": len(variants) >= scan,
            "variants": low,
        }


# --------------------------------------------------------------------------
# MCP protocol layer
# --------------------------------------------------------------------------


class Server:
    """
    JSON-RPC 2.0 / MCP over newline-delimited JSON on stdio.

    Pure logic: `handle` maps one decoded message to one optional reply dict.
    The stdio loop is the only part that touches real streams, so every rule
    below is assertable without spawning a process.
    """

    def __init__(self, tools: Tools | None = None) -> None:
        self.tools = tools
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self.negotiated_version = LATEST_PROTOCOL_VERSION

    # -- framing --------------------------------------------------------

    @staticmethod
    def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    # -- dispatch -------------------------------------------------------

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """
        Handle one message. Return the reply, or None when none is owed.

        Returning None matters: a JSON-RPC notification has no `id` and MUST
        NOT be answered. Answering it puts a reply on the wire the client has
        no pending request for, and strict clients treat that as an error.
        """
        if not isinstance(msg, dict):
            return self._error(None, INVALID_REQUEST, "message must be a JSON object")

        method = msg.get("method")
        has_id = "id" in msg and msg["id"] is not None
        msg_id = msg.get("id")

        if not isinstance(method, str):
            return self._error(msg_id, INVALID_REQUEST, "missing method") if has_id else None

        # Notifications: no id, no reply, whatever the method.
        if not has_id:
            if method == "notifications/initialized":
                self.initialized = True
                log("client reported initialized")
            return None

        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(msg_id, INVALID_PARAMS, "params must be an object")

        try:
            if method == "initialize":
                return self._result(msg_id, self._initialize(params))
            if method == "ping":
                # Spec: an empty result. Used as a liveness probe.
                return self._result(msg_id, {})
            if method == "tools/list":
                return self._result(msg_id, self._tools_list())
            if method == "tools/call":
                return self._result(msg_id, self._tools_call(params))
            return self._error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
        except ValueError as exc:
            # Malformed request at the protocol layer, not a tool failure.
            return self._error(msg_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - the loop must not die
            log(f"internal error handling {method}: {exc!r}")
            return self._error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    # -- methods --------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        if isinstance(asked, str) and asked in SUPPORTED_PROTOCOL_VERSIONS:
            self.negotiated_version = asked
        else:
            # Do not echo an unknown version back, and do not assert our own
            # newest: fall back to the spec's default so an older client can
            # still proceed.
            self.negotiated_version = DEFAULT_NEGOTIATED_VERSION
            if asked is not None:
                log(f"client asked for protocol {asked!r}, falling back to {self.negotiated_version}")

        info = params.get("clientInfo")
        if isinstance(info, dict):
            self.client_info = info
            log(f"initialize from {info.get('name', '?')} {info.get('version', '?')}")

        return {
            "protocolVersion": self.negotiated_version,
            # Declare only what is implemented. Advertising resources or
            # prompts here would make a client call methods that 404.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Read-only access to a Shopify store's catalogue and stock. "
                "search_products to find things, get_product for variant "
                "detail, check_inventory for per-location stock, "
                "low_stock_report for restocking. No tool here writes."
            ),
        }

    def _tools_list(self) -> dict[str, Any]:
        if self.tools is None:
            return {"tools": []}
        return {"tools": self.tools.descriptors()}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call requires a tool name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")

        if self.tools is None:
            return self._tool_failure("no store is configured; set SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN")

        try:
            payload = self.tools.call(name, args)
        except ValueError as exc:
            # Bad arguments, or unknown tool. A tool-level problem, so it
            # belongs in the result with isError, NOT as a JSON-RPC error:
            # the request itself was well formed, and the model needs to read
            # the message and retry. A JSON-RPC error is for the client's
            # plumbing and the model may never see its text.
            return self._tool_failure(str(exc))
        except ShopifyError as exc:
            return self._tool_failure(f"Shopify: {exc}")
        except Exception as exc:  # noqa: BLE001
            log(f"tool {name} crashed: {exc!r}")
            return self._tool_failure(f"{type(exc).__name__}: {exc}")

        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            # Same object as structured data, for clients that can use it
            # without parsing the text block.
            "structuredContent": payload,
            "isError": False,
        }

    @staticmethod
    def _tool_failure(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True}

    # -- stdio loop -----------------------------------------------------

    def serve(self, stdin: Any = None, stdout: Any = None) -> int:
        """
        Read newline-delimited JSON until EOF, writing one reply per request.

        Streams are parameters so the loop itself is testable.
        """
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        log(f"{SERVER_NAME} {SERVER_VERSION} ready on stdio")

        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                # Cannot know the id of something we could not parse, so the
                # spec's null id is correct here.
                self._write(stdout, self._error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue

            # A batch is a JSON array. Notifications inside it still produce
            # no reply, so a batch of only notifications produces nothing at
            # all - not an empty array, which would be a malformed response.
            if isinstance(msg, list):
                replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                for reply in replies:
                    self._write(stdout, reply)
                continue

            reply = self.handle(msg)
            if reply is not None:
                self._write(stdout, reply)

        log("stdin closed, shutting down")
        return EXIT_OK

    @staticmethod
    def _write(stdout: Any, obj: dict[str, Any]) -> None:
        """One compact JSON object per line. No pretty printing on the wire."""
        stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_tools() -> Tools | None:
    """
    Build the Shopify-backed tools, or None if the store is not configured.

    Returning None instead of exiting is deliberate: an agent host launches
    the server and reads tools/list before anyone has checked the environment.
    Dying at startup shows up as an opaque transport failure; staying up and
    failing each call with a readable message is diagnosable.
    """
    shop = os.environ.get("SHOPIFY_SHOP", "")
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
    if not shop or not token:
        log("SHOPIFY_SHOP / SHOPIFY_ADMIN_TOKEN are not both set; tools will refuse to run")
        return None
    try:
        return Tools(ShopifyClient(shop, token))
    except ConfigError as exc:
        log(f"configuration problem: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return EXIT_OK
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return EXIT_OK
    if "--self-test" in argv:
        from self_test import run_self_test

        return run_self_test()

    unknown = [a for a in argv if a.startswith("-")]
    if unknown:
        log(f"unknown option: {unknown[0]} (try --help)")
        return EXIT_CONFIG

    try:
        return Server(build_tools()).serve()
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        log(f"fatal: {type(exc).__name__}: {exc}")
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
