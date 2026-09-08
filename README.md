# shop-mcp

A Model Context Protocol server that lets an LLM agent answer questions about a
Shopify store's catalogue and stock — over stdio, from a single file, using
**only the Python standard library**.

No MCP SDK. No `requests`. No GraphQL client. `python3 shop_mcp.py` is the whole
install.

```
$ python3 shop_mcp.py --self-test
all green: 189 assertions
```

That command needs no credentials and no network. It is the point of the repo:
the protocol layer and the tool layer are both exercised for real, because the
Shopify transport is replaced at a seam rather than mocked at the boundary.

Installed from PyPI, the same command reports **183**, and the six-assertion
difference is a packaging fact rather than a weaker check:

```
$ uvx --from shop-mcp shop-mcp --self-test
all green: 183 assertions
```

`manifest.json` (5 assertions) and `README.md` (1) are deliberately not shipped
into `site-packages` — the manifest's `entry_point` names a bundle path that
does not exist in an installed copy, so packaging it would make a correct
install fail. Both assertions *skip* rather than fail when their file is absent,
which is why the count moves and the verdict does not. Clone the repo to run all
189.

## Why write the protocol by hand

Because the failure modes of a stdio MCP server are all invisible locally and
all fatal in a host. Each one below is a real defect this file is built to not
have, and each has an assertion naming it:

- **A diagnostic on stdout.** One stray `print()` corrupts the client's next
  parse. Nothing looks wrong when you run the server yourself. Every diagnostic
  here goes to stderr, and a test asserts stdout stays byte-empty across a full
  session.
- **Answering a notification.** `notifications/initialized` has no `id`, so a
  reply to it is a message with no pending request. Strict clients treat that as
  a protocol violation and drop the connection.
- **`id: 0` read as a notification.** `if msg.get("id")` is falsy for zero, so a
  client that numbers requests from zero has its first call silently dropped.
  Presence, not truthiness.
- **Tool failures sent as JSON-RPC errors.** A JSON-RPC error is for a malformed
  *request*. A tool that ran and failed must return a normal result with
  `isError: true` and the reason as text — otherwise the model never sees the
  message and cannot correct its own arguments.
- **Echoing an unknown `protocolVersion`.** If a client asks for a revision the
  server does not know, agreeing to it leaves both sides believing a spec is in
  use that neither implements. This falls back to `2025-03-26`, the spec's own
  default, and says so.
- **Pretty-printing the reply.** Indented JSON contains newlines, and newline is
  the frame delimiter. One message becomes several broken ones.

## Tools

| tool | answers |
|---|---|
| `search_products` | "what do we sell that matches X" — identity and total stock |
| `get_product` | one product in full, every variant with SKU, price, stock |
| `check_inventory` | stock for a SKU per location: available, committed, on-hand |
| `low_stock_report` | variants at or below a threshold, lowest first |

Four tools, chosen because each answers a question a shop owner actually asks. A
wider surface would be easy and would make the model worse at picking.

## The correctness that is not protocol

Three of the assertions cover mistakes that produce *confidently wrong answers*,
which are worse than errors:

- **An unquoted SKU.** `sku:SH 1` is a different query from `sku:"SH 1"`. The
  first silently matches the wrong variants and reports their stock as if it
  were yours. SKUs are quoted and internal quotes escaped.
- **A null quantity read as zero.** Shopify returns `null` for a variant that
  does not track inventory. Coerced to `0`, it appears in every restock report
  forever. Untracked and out-of-stock are different facts and stay different.
- **`scan_exhausted`.** `low_stock_report` scans a bounded number of variants. If
  the scan hit its limit, "nothing is low" is indistinguishable from "I did not
  look far enough" — so the result says which it was, and the model can say so
  too.

Plus the transport rules any Shopify client needs and most skip: a `THROTTLED`
GraphQL response is a *200* and must be retried, not read as success; a 401 must
*not* be retried, because waiting will not fix a bad token; backoff must actually
grow.

## Verified, and not verified

**Verified, by the self-test, on every run:** 189 assertions covering the
handshake, framing, notification handling, id presence, error mapping, schema
strictness, retry and backoff policy, SKU quoting, null-quantity handling,
threshold boundaries, and scan exhaustion. Wire shapes were taken from the
official `mcp` Python SDK's `types.py` (`LATEST_PROTOCOL_VERSION`,
`CallToolResult`, `ServerCapabilities`), not from memory.

**Not verified:** this has never been run against a live Shopify store. There is
no credential in this repo and no recorded API session. The Shopify Admin
GraphQL queries are written to the documented schema, and every code path around
them is tested against a transport double — but the round trip against a real
shop is unproven, and the test doubles are my model of Shopify's behaviour, not
Shopify.

That distinction is the honest one, and it is the same line drawn in
[`gpt-ads-feed`](https://github.com/hello532/gpt-ads-feed). A README that blurs
it is asking to be trusted on the wrong thing.

## Assertions that can fail

`mutation_test.sh` injects known defects into *copies* of the source and asserts
`--self-test` goes red for each, naming which assertion caught it. It also flags
a `NO-OP EDIT` when a search pattern has gone stale — because a mutation that
does not apply tests nothing while looking green, which is the failure mode that
makes a suite worse than useless: trusted and empty.

It found real weaknesses in the suite on its first run, and all three were the
same shape: the defect *was* detected, but by an exception rather than by a
named assertion, so the message explained nothing and every assertion after it
never ran.

Two were an unhandled `KeyError: 'result'`, from indexing a reply that the
defect had turned into a JSON-RPC error. Fixed by routing result access through
a shape guard, so the same defect now reports `a tool crash returns a result,
so the loop survives: reply is a JSON-RPC error {'code': -32603, ...}` and the
three following assertions each still report their own verdict.

The third was a bare setup call — `S.Tools(c).search_products(...)`, present
only to make the assertion below it meaningful. When the throttle branch was
disabled it raised, aborting the test before that assertion ran. Fixed with
`completes()`, the exact inverse of `raises()`: the defect now reports
`a 200-with-THROTTLED is survivable, not a hard failure: raised ShopifyError:
Throttled [THROTTLED]`, naming the rule and keeping the cause.

Three more defects surfaced only when the server was packaged as an `.mcpb`
bundle and launched the way a host launches it, which no test had ever done:

1. The code read `SHOPIFY_SHOP`; this README and the bundle manifest both told
   users to export `SHOPIFY_SHOP_DOMAIN`. Anyone following the docs got a
   permanently unconfigured server. Every one of the 180 assertions passed,
   because none of them compared the code against the docs.
2. `tools/list` returned `[]` until credentials existed, so a host saw an empty
   server and reported it broken — and the readable *no store is configured*
   message on `tools/call` was unreachable, since nothing was listed to call.
   The docstring above that code stated the opposite requirement, and the test
   below it asserted the defect: `eq(tools, [], ...)`. The list never depended
   on credentials; `descriptors()` touched no instance state at all, and is now
   a `staticmethod`.
3. `--self-test` was advertised in the module docstring but crashed inside the
   bundle, which shipped only the server file. The bundle now ships the suite.

The first fix then broke the harness in a way worth recording. The new
assertion failed when `README.md` was absent, and the harness copied only two
files, so it fired inside *every* mutant. The run still printed `17 caught`,
but six of those were credited to `README.md is present` instead of their own
labels: six real assertions could have been dead with the suite still green.
A missing README is a packaging fact, not a code defect. The load-bearing
comparison now runs against the module docstring, which travels with the
source, and the harness copies the README so the cross-check is real.

All 23 mutations are caught by an assertion that names what broke, and each is
credited to its own label.

## Use it

Installed from PyPI — nothing to clone:

```bash
export SHOPIFY_SHOP_DOMAIN=your-shop.myshopify.com
export SHOPIFY_ADMIN_TOKEN=shpat_...          # read_products, read_inventory
uvx shop-mcp                                  # or: pip install shop-mcp && shop-mcp
```

Claude Desktop / any MCP host:

```json
{
  "mcpServers": {
    "shop": {
      "command": "uvx",
      "args": ["shop-mcp"],
      "env": {
        "SHOPIFY_SHOP_DOMAIN": "your-shop.myshopify.com",
        "SHOPIFY_ADMIN_TOKEN": "shpat_..."
      }
    }
  }
}
```

From a clone instead, when you want to read the source before running it — which
is the point of a single dependency-free file, and the only way to get the full
189-assertion suite:

```bash
python3 shop_mcp.py --self-test    # 189 here, 183 installed; see above
python3 shop_mcp.py
```

```json
{ "command": "python3", "args": ["/absolute/path/to/shop_mcp.py"] }
```

With no credentials set it still completes a handshake and serves `tools/list`,
then returns `isError` with the missing variable named. A host that cannot read
`tools/list` reports "broken server" and sends you looking in the wrong place.

MIT.

<!-- mcp-name: io.github.hello532/shop-mcp -->
