# Installing shop-mcp (for coding agents)

Read-only Shopify catalogue and stock over MCP. One stdlib-only Python file, no
dependencies. This page is the install contract: every command below was run on
a clean machine and its real output is quoted.

## 1. Preflight — no credentials needed

```bash
uvx --from shop-mcp shop-mcp --self-test
```

Expected, exactly:

```
all green: 183 assertions
```

Exit code `0`. If you get this, the package installs and runs on this machine
and you can stop debugging the transport. (A clone of the repo reports `200`;
the seventeen-assertion difference is `manifest.json`, `llms-install.md` and
`README.md` not being shipped into `site-packages`, and those assertions skip
rather than fail.)

If `uvx` is missing, either is fine:

```bash
pip install shop-mcp && shop-mcp --self-test
```

## 2. Credentials

Two environment variables. Nothing else is read.

| Variable | Value | Notes |
|---|---|---|
| `SHOPIFY_SHOP_DOMAIN` | `your-shop.myshopify.com` | The `.myshopify.com` host, not a custom domain, no `https://` |
| `SHOPIFY_ADMIN_TOKEN` | `shpat_...` | Admin API access token |

Required scopes: `read_products`, `read_inventory`. Nothing here writes, so do
not grant write scopes.

Where the token comes from: Shopify admin → Settings → Apps and sales channels
→ Develop apps → create an app → Configuration → Admin API integration → select
those two scopes → Install → reveal the Admin API access token.

**Do not ask the user to paste the token into chat.** Write it into the MCP
config file yourself, or have them set it in their shell profile.

## 3. MCP config

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

Transport is stdio. There is no HTTP mode, no port, no OAuth flow.

## 4. Verify the server, not the config

After connecting, `tools/list` must return exactly these four:

| Tool | Required argument |
|---|---|
| `search_products` | `query` |
| `get_product` | — (`id` or `handle`) |
| `check_inventory` | `sku` |
| `low_stock_report` | — |

`initialize` reports `serverInfo` `{"name":"shop-mcp","version":"1.0.1"}` and
capabilities `{"tools":{"listChanged":false}}`.

## 5. Failure modes, and which one you are looking at

The point of the design: **an unconfigured server still completes the handshake
and still serves `tools/list`.** Only the tool call fails. Every row below is a
real run on this machine, not a reading of the source.

| What you see | What it means | What to do |
|---|---|---|
| `all green: 183 assertions` | install is fine | move on |
| stderr `SHOPIFY_SHOP_DOMAIN / SHOPIFY_ADMIN_TOKEN are not both set; tools will refuse to run`, then handshake OK and `tools/list` returns 4 | neither variable reached the process | set both in the MCP config's `env`, not just your shell |
| `tools/call` → `Error: no store is configured; set SHOPIFY_SHOP_DOMAIN and SHOPIFY_ADMIN_TOKEN` | same cause, seen from the call side | as above |
| `tools/call` → `Error: Shopify: Shopify rejected the token (401). Check SHOPIFY_ADMIN_TOKEN and that the app has the scopes this tool needs.` | the variables arrived; the token is invalid, revoked, or lacks scopes | regenerate with `read_products` + `read_inventory` |
| exit code `2`, stderr `unknown option: --nope (try --help)` | a bad CLI flag — the **only** thing that exits 2 | fix `args` in the config |
| no `tools/list` response at all | transport problem — wrong command, `uvx` not on PATH | re-run step 1 |

Two things this server deliberately does **not** do, so do not wait for them:
it never exits non-zero for missing credentials (that path logs one line and
serves anyway, exit `0`), and it does not validate the domain's shape at
startup — a malformed `SHOPIFY_SHOP_DOMAIN` surfaces on the first call, not at
launch.

Do not report "broken server" when `tools/call` returns `isError`. That path is
deliberate: the error text names what to fix, which is why `tools/list` is
served unconditionally.

## 6. Protocol versions

Speaks `2025-11-25`, `2025-06-18`, `2025-03-26`. A client naming anything else
gets `2025-03-26` back — the spec's default negotiated revision — rather than
this server's newest, so an older client can still proceed. Verified against the
official TypeScript SDK client 1.30.0: connect, `tools/list`, `tools/call`, and
clean shutdown all succeed.

MIT. Source is one file, `shop_mcp.py`; reading it before running it is the
intended workflow.
