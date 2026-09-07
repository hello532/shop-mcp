#!/usr/bin/env python3
"""
Self-test for shop_mcp.

Runs with no credentials and no network: the Shopify transport is replaced at
its seam, so the protocol layer and the tool layer are both exercised for real.

Each assertion pins one rule that, if broken, produces a server that looks
healthy from the inside and misbehaves only once an agent is talking to it.
"""

from __future__ import annotations

import io
import sys
import json
from typing import Any

import shop_mcp as S

PASSED = 0
FAILED: list[str] = []


def ok(cond: bool, label: str) -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def eq(got: Any, want: Any, label: str) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}: got {got!r}, want {want!r}")


def raises(fn: Any, exc: type[BaseException], label: str) -> None:
    try:
        fn()
    except exc:
        ok(True, label)
        return
    except Exception as e:  # noqa: BLE001
        FAILED.append(f"{label}: raised {type(e).__name__}, want {exc.__name__}")
        return
    FAILED.append(f"{label}: nothing raised, want {exc.__name__}")


def completes(fn: Any, label: str) -> None:
    """
    The exact inverse of `raises`: assert fn() returns rather than throwing.

    Needed wherever a mutation can turn a working call into a raise. A bare
    `S.Tools(c).search_products(...)` line whose only purpose is to set up the
    assertion below it will, if it starts raising, abort the whole test before
    that assertion runs - so the suite goes red with a message about the
    exception instead of about the rule that broke.
    """
    global PASSED
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        FAILED.append(f"{label}: raised {type(e).__name__}: {e}")
        return
    PASSED += 1


def payload(r: Any, label: str) -> dict[str, Any]:
    """
    Return a reply's `result`, or record a NAMED failure and hand back a stub.

    Written after a mutation test caught a real weakness in this suite. When a
    defect turned tool failures into JSON-RPC errors, the assertions below
    reached straight into `r["result"]` and died with `KeyError: 'result'`. The
    defect WAS caught - the suite went red - but the message said nothing about
    what was wrong, which is the difference between a test and a tripwire.

    The stub keeps the following assertions running so they each report their
    own verdict, instead of the first one aborting the whole test.
    """
    if not isinstance(r, dict):
        FAILED.append(f"{label}: reply is {type(r).__name__}, want a dict")
    elif "error" in r:
        FAILED.append(f"{label}: reply is a JSON-RPC error {r['error']!r}, want a result")
    elif "result" not in r:
        FAILED.append(f"{label}: reply has no result, keys are {sorted(r)}")
    elif not isinstance(r["result"], dict):
        FAILED.append(f"{label}: result is {type(r['result']).__name__}, want a dict")
    else:
        return r["result"]
    return {"isError": None, "content": [{"type": "text", "text": ""}]}


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class FakeClient(S.ShopifyClient):
    """
    A ShopifyClient with the network seam replaced.

    Subclasses the real class rather than reimplementing it, so retry, throttle
    and error interpretation - the logic most likely to be wrong - is the real
    code under test.
    """

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self.slept: list[float] = []
        super().__init__(
            shop="test.myshopify.com",
            token="shpat_test",
            sleeper=self.slept.append,
        )
        self.responses = list(responses)
        self.sent: list[dict[str, Any]] = []

    def _post(self, payload: bytes) -> tuple[int, bytes]:
        self.sent.append(json.loads(payload))
        if not self.responses:
            raise AssertionError("fake ran out of responses")
        status, body = self.responses.pop(0)
        return status, json.dumps(body).encode()


def product_node(handle: str = "blue-shirt", **over: Any) -> dict[str, Any]:
    node = {
        "id": "gid://shopify/Product/1",
        "handle": handle,
        "title": "Blue Shirt",
        "status": "ACTIVE",
        "vendor": "Acme",
        "productType": "Shirt",
        "totalInventory": 7,
        "onlineStoreUrl": f"https://test.myshopify.com/products/{handle}",
    }
    node.update(over)
    return node


def data(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"data": body}


# --------------------------------------------------------------------------
# 1. Notifications must never be answered
# --------------------------------------------------------------------------


def test_notifications() -> None:
    srv = S.Server()

    eq(
        srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        None,
        "initialized notification gets no reply",
    )
    ok(srv.initialized, "initialized notification sets the flag")

    # An id of null is the JSON-RPC marker for "no id", so this is still a
    # notification even though the key is present.
    eq(
        srv.handle({"jsonrpc": "2.0", "id": None, "method": "notifications/initialized"}),
        None,
        "explicit null id is treated as a notification",
    )

    eq(
        srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}),
        None,
        "unknown notification gets no reply, not method-not-found",
    )

    # id=0 is a legitimate id. Testing truthiness instead of presence would
    # misread it as a notification and silently drop the reply.
    r = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "ping"})
    ok(r is not None, "id=0 is a real request, not a notification")
    if r:
        eq(r.get("id"), 0, "id=0 is echoed back as 0")


# --------------------------------------------------------------------------
# 2. initialize and version negotiation
# --------------------------------------------------------------------------


def test_initialize() -> None:
    srv = S.Server()
    r = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "probe", "version": "9.9"},
                "capabilities": {},
            },
        }
    )
    assert r is not None
    res = r["result"]
    eq(res["protocolVersion"], "2025-06-18", "a supported version is agreed to as asked")
    eq(res["serverInfo"]["name"], S.SERVER_NAME, "serverInfo carries the name")
    eq(srv.client_info.get("name"), "probe", "clientInfo is remembered")

    # Only implemented capabilities may be declared: anything advertised here
    # is something a client is entitled to call.
    caps = res["capabilities"]
    eq(list(caps.keys()), ["tools"], "only the tools capability is advertised")
    ok("resources" not in caps and "prompts" not in caps, "no unimplemented capability is advertised")
    ok(bool(res.get("instructions")), "instructions are provided for the model")

    srv2 = S.Server()
    r2 = srv2.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
    )
    assert r2 is not None
    eq(
        r2["result"]["protocolVersion"],
        S.DEFAULT_NEGOTIATED_VERSION,
        "an unknown version falls back to the spec default, and is not echoed",
    )
    ok(
        r2["result"]["protocolVersion"] != "1999-01-01",
        "an unknown version is never echoed back as agreed",
    )

    srv3 = S.Server()
    r3 = srv3.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
    assert r3 is not None
    eq(
        r3["result"]["protocolVersion"],
        S.DEFAULT_NEGOTIATED_VERSION,
        "a missing version falls back rather than crashing",
    )

    ok(
        S.LATEST_PROTOCOL_VERSION in S.SUPPORTED_PROTOCOL_VERSIONS,
        "the advertised latest version is in the supported set",
    )


# --------------------------------------------------------------------------
# 3. JSON-RPC error mapping
# --------------------------------------------------------------------------


def test_jsonrpc_errors() -> None:
    srv = S.Server()

    r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
    assert r is not None
    eq(r["error"]["code"], S.METHOD_NOT_FOUND, "unknown method is -32601")
    ok("result" not in r, "an error reply carries no result")
    eq(r["id"], 5, "an error reply echoes the request id")

    r = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": []})
    assert r is not None
    eq(r["error"]["code"], S.INVALID_PARAMS, "non-object params is -32602")

    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}})
    assert r is not None
    # A missing tool name is a malformed request, not a tool failure: there is
    # no tool to attribute the error to.
    eq(r["error"]["code"], S.INVALID_PARAMS, "tools/call with no name is -32602")

    r = srv.handle(["not", "a", "message"])
    assert r is not None
    eq(r["error"]["code"], S.INVALID_REQUEST, "a non-object message is -32600")

    r = srv.handle({"jsonrpc": "2.0", "id": 8})
    assert r is not None
    eq(r["error"]["code"], S.INVALID_REQUEST, "a request with no method is -32600")

    eq(srv.handle({"jsonrpc": "2.0"}), None, "a notification with no method is still not answered")


# --------------------------------------------------------------------------
# 4. Tool failures are results, not JSON-RPC errors
# --------------------------------------------------------------------------


def test_tool_errors_are_results() -> None:
    tools = S.Tools(FakeClient([]))
    srv = S.Server(tools)

    r = srv.handle(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert r is not None
    ok("error" not in r, "an unknown tool is not a JSON-RPC error")
    p = payload(r, "an unknown tool returns a result")
    ok(p["isError"] is True, "an unknown tool sets isError")
    ok(
        "nope" in p["content"][0]["text"],
        "the failure text names the tool, so the model can correct itself",
    )

    # Bad arguments: same rule. The request was well formed; the model needs to
    # read the message and retry.
    r = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "search_products", "arguments": {}},
        }
    )
    assert r is not None
    ok("error" not in r, "a missing argument is not a JSON-RPC error")
    p = payload(r, "a missing argument returns a result")
    ok(p["isError"] is True, "a missing argument sets isError")
    ok("query" in p["content"][0]["text"], "the message names the missing argument")

    # A Shopify refusal must reach the model as text, not vanish into plumbing.
    bad = FakeClient([(200, {"errors": [{"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}]})])
    srv2 = S.Server(S.Tools(bad))
    r = srv2.handle(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "search_products", "arguments": {"query": "x"}},
        }
    )
    assert r is not None
    ok("error" not in r, "a Shopify refusal is not a JSON-RPC error")
    p = payload(r, "a Shopify refusal returns a result")
    ok(p["isError"] is True, "a Shopify refusal sets isError")
    text = p["content"][0]["text"]
    ok("Access denied" in text, "Shopify's own message is passed through verbatim")
    ok("ACCESS_DENIED" in text, "the error code is passed through, so the cause is identifiable")

    # A crash inside a tool must not kill the loop.
    class Boom(S.Tools):
        def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("kaboom")

    srv3 = S.Server(Boom(FakeClient([])))
    r = srv3.handle(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "search_products", "arguments": {"query": "x"}},
        }
    )
    assert r is not None
    p = payload(r, "a tool crash returns a result, so the loop survives")
    ok(p["isError"] is True, "an unexpected crash becomes isError, not a dead server")
    ok("kaboom" in p["content"][0]["text"], "the crash message survives for debugging")


def test_unconfigured_server_still_answers() -> None:
    """
    With no credentials the server must still complete a handshake and list
    tools. A host that cannot read tools/list reports a broken server, which
    sends the operator looking in the wrong place.
    """
    srv = S.Server(None)
    r = srv.handle({"jsonrpc": "2.0", "id": 20, "method": "initialize", "params": {}})
    assert r is not None
    ok("result" in r, "an unconfigured server still completes initialize")

    r = srv.handle({"jsonrpc": "2.0", "id": 21, "method": "tools/list"})
    assert r is not None
    eq(r["result"]["tools"], [], "an unconfigured server lists no tools rather than failing")

    r = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {"name": "search_products", "arguments": {"query": "x"}},
        }
    )
    assert r is not None
    ok(r["result"]["isError"] is True, "an unconfigured call fails as a result")
    ok(
        "SHOPIFY_SHOP" in r["result"]["content"][0]["text"],
        "the message names the missing variable, so it is actionable",
    )


# --------------------------------------------------------------------------
# 5. tools/list descriptors must be usable by a model
# --------------------------------------------------------------------------


def test_tool_descriptors() -> None:
    srv = S.Server(S.Tools(FakeClient([])))
    r = srv.handle({"jsonrpc": "2.0", "id": 30, "method": "tools/list"})
    assert r is not None
    tools = r["result"]["tools"]
    eq(len(tools), 4, "all four tools are listed")

    names = [t["name"] for t in tools]
    eq(
        sorted(names),
        ["check_inventory", "get_product", "low_stock_report", "search_products"],
        "the tool names are the documented set",
    )
    eq(len(set(names)), len(names), "no duplicate tool name")

    for t in tools:
        label = t["name"]
        ok(bool(t.get("description")), f"{label} has a description")
        schema = t.get("inputSchema")
        ok(isinstance(schema, dict), f"{label} has an inputSchema")
        eq(schema.get("type"), "object", f"{label} inputSchema is an object schema")
        ok(isinstance(schema.get("properties"), dict), f"{label} declares properties")
        # Closed schemas: an open one lets a model pass a misspelled argument
        # that is then silently ignored.
        eq(schema.get("additionalProperties"), False, f"{label} rejects unknown properties")
        for prop, spec in schema["properties"].items():
            ok(bool(spec.get("description")), f"{label}.{prop} is documented")
        for req in schema.get("required", []):
            ok(req in schema["properties"], f"{label} required field {req} exists in properties")

    # Every advertised tool must actually dispatch. A descriptor for a tool the
    # dispatcher does not know is a promise the server cannot keep.
    tool_impl = S.Tools(FakeClient([]))
    for name in names:
        raises(
            lambda n=name: tool_impl.call(n, {"__probe__": True}),
            Exception,
            f"{name} dispatches (does not fall through to unknown tool)",
        )
        try:
            tool_impl.call(name, {"__probe__": True})
        except ValueError as e:
            ok("unknown tool" not in str(e), f"{name} is a known tool in the dispatcher")
        except Exception:
            ok(True, f"{name} is a known tool in the dispatcher")


# --------------------------------------------------------------------------
# 6. Tool behaviour against stubbed Shopify data
# --------------------------------------------------------------------------


def test_search_products() -> None:
    c = FakeClient([data({"products": {"edges": [{"node": product_node()}]}})])
    out = S.Tools(c).search_products({"query": "shirt", "limit": 5})
    eq(out["count"], 1, "search reports the result count")
    eq(out["products"][0]["handle"], "blue-shirt", "search returns the handle")
    eq(out["products"][0]["total_inventory"], 7, "search returns total inventory")
    eq(c.sent[0]["variables"], {"q": "shirt", "n": 5}, "search passes query and limit through")

    # Clamping, not rejecting: a model asking for 1000 wants "a lot".
    c = FakeClient([data({"products": {"edges": []}})])
    S.Tools(c).search_products({"query": "x", "limit": 1000})
    eq(c.sent[0]["variables"]["n"], 50, "an oversized limit is clamped to the maximum")

    c = FakeClient([data({"products": {"edges": []}})])
    S.Tools(c).search_products({"query": "x", "limit": 0})
    eq(c.sent[0]["variables"]["n"], 1, "a zero limit is clamped to the minimum")

    c = FakeClient([data({"products": {"edges": []}})])
    S.Tools(c).search_products({"query": "x"})
    eq(c.sent[0]["variables"]["n"], 10, "an absent limit uses the default")

    raises(
        lambda: S.Tools(FakeClient([])).search_products({"query": "  "}),
        ValueError,
        "a whitespace-only query is rejected",
    )
    raises(
        lambda: S.Tools(FakeClient([])).search_products({"query": "x", "limit": "many"}),
        ValueError,
        "a non-numeric limit is rejected rather than silently defaulted",
    )
    # True is an int in Python. Accepting it would send n=1 for limit=true.
    raises(
        lambda: S.Tools(FakeClient([])).search_products({"query": "x", "limit": True}),
        ValueError,
        "a boolean limit is rejected, not read as 1",
    )

    # An empty result set is a valid answer, not an error.
    c = FakeClient([data({"products": {"edges": []}})])
    out = S.Tools(c).search_products({"query": "nothing"})
    eq(out["count"], 0, "no matches is a count of zero, not a failure")


def test_get_product() -> None:
    node = product_node()
    node["description"] = "A shirt."
    node["variants"] = {
        "edges": [
            {"node": {"id": "gid://shopify/ProductVariant/9", "sku": "SH-1", "title": "S",
                      "price": "19.99", "inventoryQuantity": 3, "availableForSale": True}}
        ]
    }
    c = FakeClient([data({"productByIdentifier": node})])
    out = S.Tools(c).get_product({"handle": "blue-shirt"})
    ok(out["found"] is True, "a product found by handle reports found")
    eq(out["variant_count"], 1, "variants are counted")
    eq(out["variants"][0]["sku"], "SH-1", "the variant SKU is returned")
    eq(out["variants"][0]["stock"], 3, "the variant stock is returned")

    # found:false rather than an exception: "no such product" is an answer.
    c = FakeClient([data({"productByIdentifier": None})])
    out = S.Tools(c).get_product({"handle": "ghost"})
    ok(out["found"] is False, "a missing product reports found:false, not an error")
    ok("ghost" in out["looked_up"], "the failed lookup says what it looked for")

    c = FakeClient([data({"product": product_node()})])
    out = S.Tools(c).get_product({"id": "gid://shopify/Product/1"})
    ok(out["found"] is True, "a product found by id reports found")
    eq(c.sent[0]["variables"]["id"], "gid://shopify/Product/1", "the id is passed through unchanged")

    raises(
        lambda: S.Tools(FakeClient([])).get_product({}),
        ValueError,
        "get_product with neither handle nor id is rejected",
    )
    raises(
        lambda: S.Tools(FakeClient([])).get_product({"handle": "a", "id": "gid://shopify/Product/1"}),
        ValueError,
        "get_product with both handle and id is rejected as ambiguous",
    )
    # A bare numeric id is the most likely model mistake; catch it locally
    # rather than spending a round trip on a guaranteed API error.
    raises(
        lambda: S.Tools(FakeClient([])).get_product({"id": "1234567890"}),
        ValueError,
        "a bare numeric id is rejected before a request is made",
    )


def test_check_inventory() -> None:
    c = FakeClient(
        [
            data(
                {
                    "productVariants": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/ProductVariant/9",
                                    "sku": "SH-1",
                                    "displayName": "Blue Shirt - S",
                                    "inventoryQuantity": 5,
                                    "inventoryItem": {
                                        "id": "gid://shopify/InventoryItem/3",
                                        "tracked": True,
                                        "inventoryLevels": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "location": {"id": "gid://shopify/Location/1",
                                                                     "name": "Main"},
                                                        "quantities": [
                                                            {"name": "available", "quantity": 4},
                                                            {"name": "committed", "quantity": 1},
                                                            {"name": "on_hand", "quantity": 5},
                                                        ],
                                                    }
                                                }
                                            ]
                                        },
                                    },
                                }
                            }
                        ]
                    }
                }
            )
        ]
    )
    out = S.Tools(c).check_inventory({"sku": "SH-1"})
    eq(out["count"], 1, "inventory reports one matching variant")
    loc = out["variants"][0]["locations"][0]
    eq(loc["location"], "Main", "the location name is returned")
    eq(loc["available"], 4, "available is read from the quantities list")
    eq(loc["committed"], 1, "committed is read from the quantities list")
    eq(loc["on_hand"], 5, "on_hand is read from the quantities list")
    ok(out["variants"][0]["tracked"] is True, "whether the item is tracked is reported")

    # An untracked item has no levels. Reporting stock for it would be a lie,
    # so the caller needs `tracked` to interpret an empty location list.
    c = FakeClient(
        [data({"productVariants": {"edges": [{"node": {
            "id": "gid://shopify/ProductVariant/10", "sku": "NT-1", "displayName": "Untracked",
            "inventoryQuantity": None,
            "inventoryItem": {"id": "x", "tracked": False, "inventoryLevels": {"edges": []}},
        }}]}})]
    )
    out = S.Tools(c).check_inventory({"sku": "NT-1"})
    ok(out["variants"][0]["tracked"] is False, "an untracked item is reported as untracked")
    eq(out["variants"][0]["locations"], [], "an untracked item has no locations")

    # SKU quoting: an unquoted SKU with a space would be parsed as two search
    # terms and match the wrong variants.
    c = FakeClient([data({"productVariants": {"edges": []}})])
    S.Tools(c).check_inventory({"sku": "SH 1"})
    eq(c.sent[0]["variables"]["q"], 'sku:"SH 1"', "a SKU containing a space is quoted")

    c = FakeClient([data({"productVariants": {"edges": []}})])
    S.Tools(c).check_inventory({"sku": 'SH"1'})
    eq(c.sent[0]["variables"]["q"], 'sku:"SH\\"1"', "a quote inside a SKU is escaped, not left to break the query")

    c = FakeClient([data({"productVariants": {"edges": []}})])
    S.Tools(c).check_inventory({"sku": "SH\\1"})
    eq(c.sent[0]["variables"]["q"], 'sku:"SH\\\\1"', "a backslash inside a SKU is escaped")

    raises(
        lambda: S.Tools(FakeClient([])).check_inventory({}),
        ValueError,
        "check_inventory without a sku is rejected",
    )


def test_low_stock() -> None:
    def variant(sku: str, qty: Any) -> dict[str, Any]:
        return {
            "node": {
                "id": f"gid://shopify/ProductVariant/{sku}",
                "sku": sku,
                "displayName": f"Item {sku}",
                "inventoryQuantity": qty,
                "product": {"id": "gid://shopify/Product/1", "handle": "h",
                            "title": "T", "status": "ACTIVE"},
            }
        }

    c = FakeClient(
        [data({"productVariants": {"edges": [
            variant("A", 0), variant("B", 12), variant("C", 3), variant("D", None),
        ]}})]
    )
    out = S.Tools(c).low_stock_report({"threshold": 5, "scan": 100})
    eq(out["count"], 2, "only variants at or below the threshold are reported")
    eq([v["sku"] for v in out["variants"]], ["A", "C"], "results are sorted by stock, lowest first")
    eq(out["scanned"], 4, "the number of variants examined is reported")
    ok(out["scan_exhausted"] is False, "a partial scan is reported as not exhausted")

    # A null quantity means "not tracked", which is not the same as zero.
    # Reporting it as low stock would send someone to restock a service.
    ok(all(v["sku"] != "D" for v in out["variants"]), "a null quantity is not treated as zero stock")

    # The boundary itself: `<=` not `<`.
    c = FakeClient([data({"productVariants": {"edges": [variant("E", 5)]}})])
    out = S.Tools(c).low_stock_report({"threshold": 5})
    eq(out["count"], 1, "a variant exactly at the threshold is included")

    c = FakeClient([data({"productVariants": {"edges": [variant("F", 6)]}})])
    out = S.Tools(c).low_stock_report({"threshold": 5})
    eq(out["count"], 0, "a variant just above the threshold is excluded")

    # "nothing is low" and "I did not look far enough" are different answers.
    c = FakeClient([data({"productVariants": {"edges": [variant(str(i), 99) for i in range(2)]}})])
    out = S.Tools(c).low_stock_report({"threshold": 5, "scan": 2})
    ok(out["scan_exhausted"] is True, "hitting the scan limit is reported, so zero results are not misread")

    c = FakeClient([data({"productVariants": {"edges": []}})])
    S.Tools(c).low_stock_report({"scan": 9999})
    eq(c.sent[0]["variables"]["n"], 250, "an oversized scan is clamped to the API maximum")


# --------------------------------------------------------------------------
# 7. Transport: retries, throttling, partial responses
# --------------------------------------------------------------------------


def test_transport() -> None:
    # A 429 is retried, and the retry succeeds.
    c = FakeClient([(429, {}), data({"products": {"edges": []}})])
    S.Tools(c).search_products({"query": "x"})
    eq(len(c.sent), 2, "a 429 is retried")
    eq(len(c.slept), 1, "the retry waits before trying again")

    for status in (500, 502, 503, 504):
        c = FakeClient([(status, {}), data({"products": {"edges": []}})])
        S.Tools(c).search_products({"query": "x"})
        eq(len(c.sent), 2, f"HTTP {status} is retried")

    # 401 is not retried: the token will not become valid by waiting.
    c = FakeClient([(401, {})])
    raises(lambda: S.Tools(c).search_products({"query": "x"}), S.ShopifyError, "a 401 raises")
    eq(len(c.sent), 1, "a 401 is not retried")
    try:
        FakeClient([(401, {})]).query("query{x}")
    except S.ShopifyError as e:
        ok("SHOPIFY_ADMIN_TOKEN" in str(e), "the 401 message names the variable to check")

    # A throttled query returns HTTP 200 with an error, not 429. Treating it as
    # success would return "no data"; retrying without waiting burns the retry.
    throttled = (
        200,
        {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 100,
                    "throttleStatus": {"currentlyAvailable": 20, "restoreRate": 50.0},
                }
            },
        },
    )
    c = FakeClient([throttled, data({"products": {"edges": []}})])
    completes(
        lambda: S.Tools(c).search_products({"query": "x"}),
        "a 200-with-THROTTLED is survivable, not a hard failure",
    )
    eq(len(c.sent), 2, "a 200-with-THROTTLED is retried, not read as success")
    # (100 - 20) / 50 = 1.6s, computed from the bucket rather than guessed.
    eq(round(c.slept[0], 2), 1.6, "the wait is computed from the cost extension")

    # No usable cost data: fall back rather than divide by zero.
    c = FakeClient(
        [(200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}),
         data({"products": {"edges": []}})]
    )
    completes(
        lambda: S.Tools(c).search_products({"query": "x"}),
        "a throttle with no cost data is survivable",
    )
    ok(bool(c.slept) and c.slept[0] > 0, "a throttle with no cost data still waits")

    # Retries are finite.
    c = FakeClient([(429, {})] * 10)
    raises(lambda: S.Tools(c).search_products({"query": "x"}), S.ShopifyError, "retries eventually give up")
    ok(len(c.sent) <= 5, "the retry count is bounded")

    # Backoff must grow, or a bounded retry is just five rapid failures.
    c = FakeClient([(429, {})] * 10)
    try:
        S.Tools(c).search_products({"query": "x"})
    except S.ShopifyError:
        pass
    ok(c.slept == sorted(c.slept) and c.slept[-1] > c.slept[0], "backoff increases between retries")

    # A 200 with a non-JSON body must be a clear error, not a crash.
    class Garbage(FakeClient):
        def _post(self, payload: bytes) -> tuple[int, bytes]:
            return 200, b"<html>maintenance</html>"

    raises(
        lambda: Garbage([]).query("query{x}"),
        S.ShopifyError,
        "a non-JSON 200 body raises a readable error",
    )

    # data:null with no errors is a protocol violation on their side.
    c = FakeClient([(200, {})])
    raises(lambda: c.query("query{x}"), S.ShopifyError, "a body with neither data nor errors raises")

    # Partial responses are normal when a scope is missing: a denied field
    # group comes back absent, and the tool must degrade rather than crash.
    c = FakeClient([data({})])
    out = S.Tools(c).search_products({"query": "x"})
    eq(out["count"], 0, "a response missing the whole container degrades to empty")

    c = FakeClient([data({"products": None})])
    out = S.Tools(c).search_products({"query": "x"})
    eq(out["count"], 0, "a null container degrades to empty")

    c = FakeClient([data({"products": {"edges": [{"node": None}, {"node": product_node()}]}})])
    out = S.Tools(c).search_products({"query": "x"})
    eq(out["count"], 1, "a null node is skipped rather than crashing the whole page")

    # Config errors are caught at construction, before any request.
    raises(lambda: S.ShopifyClient("", "t"), S.ConfigError, "an empty shop is a ConfigError")
    raises(lambda: S.ShopifyClient("s.myshopify.com", ""), S.ConfigError, "an empty token is a ConfigError")

    c = S.ShopifyClient("https://s.myshopify.com/", "  t  ")
    eq(c.shop, "s.myshopify.com", "the shop domain is normalised")
    eq(c.token, "t", "the token is stripped, so a copy-paste newline does not break auth")
    ok(c.endpoint.startswith("https://s.myshopify.com/admin/api/"), "the endpoint is built from the shop")


# --------------------------------------------------------------------------
# 8. stdio transport
# --------------------------------------------------------------------------


def test_stdio_loop() -> None:
    def run(lines: str, tools: S.Tools | None = None) -> list[str]:
        out = io.StringIO()
        S.Server(tools).serve(io.StringIO(lines), out)
        return [ln for ln in out.getvalue().split("\n") if ln]

    # Every line on stdout must be one complete JSON object. Anything else
    # means something printed to stdout and corrupted the stream.
    lines = run(
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    eq(len(lines), 2, "a notification in the middle of the stream produces no output line")
    for ln in lines:
        try:
            json.loads(ln)
            ok(True, "each stdout line parses as standalone JSON")
        except json.JSONDecodeError:
            FAILED.append(f"stdout line is not valid JSON: {ln[:80]!r}")
        ok("\n" not in ln.strip(), "no reply spans multiple lines")

    # Pretty-printed JSON on the wire would break newline framing. The tool
    # payload is indented inside its text field, so this is a real risk.
    tools = S.Tools(FakeClient([data({"products": {"edges": [{"node": product_node()}]}})]))
    lines = run(
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"search_products","arguments":{"query":"shirt"}}}\n',
        tools,
    )
    eq(len(lines), 1, "a reply containing an indented payload is still exactly one line")
    payload = json.loads(lines[0])
    inner = payload["result"]["content"][0]["text"]
    ok("\n" in inner, "the text block is still human readable inside the escaped string")
    eq(json.loads(inner)["products"][0]["handle"], "blue-shirt", "the text block is parseable JSON")
    eq(
        payload["result"]["structuredContent"]["products"][0]["handle"],
        "blue-shirt",
        "structuredContent carries the same data without parsing text",
    )

    # Malformed input must not kill the loop, and the reply needs a null id
    # because the id could not be recovered.
    lines = run('not json at all\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
    eq(len(lines), 2, "a malformed line is answered and the loop continues")
    first = json.loads(lines[0])
    eq(first["error"]["code"], S.PARSE_ERROR, "unparseable input is -32700")
    ok(first["id"] is None, "a parse error carries a null id")
    eq(json.loads(lines[1])["id"], 9, "the next request after a parse error is still served")

    # Blank lines are framing artefacts, not messages.
    lines = run('\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n')
    eq(len(lines), 1, "blank lines are skipped without a reply")

    # A batch of only notifications must produce no output at all. Emitting an
    # empty array would itself be a malformed response.
    lines = run('[{"jsonrpc":"2.0","method":"notifications/initialized"}]\n')
    eq(len(lines), 0, "a batch of only notifications produces no output")

    lines = run('[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"ping"}]\n')
    eq(len(lines), 2, "a batch of two requests produces two replies")

    # EOF is a clean shutdown, not an error.
    out = io.StringIO()
    eq(S.Server(None).serve(io.StringIO(""), out), S.EXIT_OK, "EOF on stdin exits zero")
    eq(out.getvalue(), "", "an empty stream writes nothing to stdout")


def test_log_never_touches_stdout() -> None:
    """
    The rule the whole transport depends on: diagnostics go to stderr.

    Asserted by capturing both streams while calling log() directly, because
    a single stray stdout write corrupts the next message a client parses.
    """
    import contextlib

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        S.log("diagnostic probe")
    eq(out.getvalue(), "", "log() writes nothing to stdout")
    ok("diagnostic probe" in err.getvalue(), "log() writes to stderr")

    # The same guarantee while the loop is running: a request that logs must
    # not put its diagnostic on the wire.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        wire = io.StringIO()
        S.Server(None).serve(
            io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize",'
                        '"params":{"protocolVersion":"1999-01-01"}}\n'),
            wire,
        )
    eq(out.getvalue(), "", "the serve loop writes nothing to the real stdout")
    ok("1999-01-01" in err.getvalue(), "the version fallback is logged to stderr")


def test_cli() -> None:
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        eq(S.main(["--version"]), S.EXIT_OK, "--version exits zero")
    ok(S.SERVER_VERSION in out.getvalue(), "--version prints the version")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        eq(S.main(["--help"]), S.EXIT_OK, "--help exits zero")
    ok("stdio" in out.getvalue(), "--help explains the transport")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        eq(S.main(["--bogus"]), S.EXIT_CONFIG, "an unknown option exits with the config code")
    ok("--bogus" in err.getvalue(), "the unknown option is named on stderr")

    ok(S.EXIT_OK != S.EXIT_CONFIG != S.EXIT_RUNTIME, "the exit codes are distinct")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

TESTS = [
    test_notifications,
    test_initialize,
    test_jsonrpc_errors,
    test_tool_errors_are_results,
    test_unconfigured_server_still_answers,
    test_tool_descriptors,
    test_search_products,
    test_get_product,
    test_check_inventory,
    test_low_stock,
    test_transport,
    test_stdio_loop,
    test_log_never_touches_stdout,
    test_cli,
]


def run_self_test() -> int:
    for t in TESTS:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")

    if FAILED:
        print(f"FAILED: {len(FAILED)}", file=sys.stderr)
        for f in FAILED:
            print(f"  - {f}", file=sys.stderr)
        print(f"passed {PASSED}", file=sys.stderr)
        return 1

    print(f"all green: {PASSED} assertions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import sys as _s

    _s.exit(run_self_test())
