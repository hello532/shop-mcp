#!/usr/bin/env bash
# Mutation tests for the shop-mcp self-test.
#
# TWO RULES, both learned by breaking them:
#   1. NEVER edit the checkout to restore. Copy the tree, mutate the copy.
#   2. A check that never fails is not a check. Every mutation below must make
#      --self-test fail, and the run must say WHICH assertion caught it.
#
# Each mutation is a defect a real implementation ships with. If the suite
# still passes, the corresponding assertion is decoration and is reported as a
# MISS - a suite that cannot fail is worse than no suite, because it is trusted.
#
# QUOTING, the thing that broke this file before:
#   The defect snippet is handed to python3 through the environment, and the
#   heredoc is QUOTED (<<'PY'). Bash therefore performs no parameter expansion
#   and no backslash processing on the snippet: python sees exactly the bytes
#   written at the call site. The previous version interpolated the snippet
#   into an UNQUOTED heredoc, so a "\n" inside a pattern was mangled by bash
#   before python ever saw it, the pattern silently stopped matching, and the
#   mutation reported NO-OP EDIT. The rule that follows from that: patterns
#   contain no backslashes at all. Where a target line contains an escape,
#   anchor on a neighbouring substring that does not.
#
# Call-site convention: the snippet is wrapped in single quotes, so it contains
# no single quote; every python string literal inside is triple-double-quoted,
# which holds embedded " and real newlines without escaping.

set -u

SRC="$(cd "$(dirname "$0")" && pwd)"
PASS=0
MISS=0
COUNT=0
declare -a MISSES=()

run_mutation() {
  local label="$1" file="$2" expect="$3" python_edit="$4"
  local work out rc caught
  COUNT=$((COUNT + 1))

  work="$(mktemp -d)" || {
    echo "  SETUP FAILED  ${COUNT}. $label  (mktemp)"
    MISS=$((MISS + 1)); MISSES+=("$label (mktemp failed)")
    return
  }

  if ! cp "$SRC/shop_mcp.py" "$SRC/self_test.py" "$work/"; then
    echo "  SETUP FAILED  ${COUNT}. $label  (cp)"
    MISS=$((MISS + 1)); MISSES+=("$label (cp failed)")
    rm -rf "$work"; return
  fi

  # Apply the defect to the COPY. The snippet travels in MUT_EDIT so that the
  # heredoc can stay quoted and pass it through byte for byte.
  if ! MUT_EDIT="$python_edit" python3 - "$work/$file" <<'PY'
import os, pathlib, sys

path = pathlib.Path(sys.argv[1])
ns = {"s": path.read_text()}
exec(os.environ["MUT_EDIT"], ns)
if not isinstance(ns["s"], str):
    raise SystemExit("the defect snippet did not leave a string in s")
path.write_text(ns["s"])
PY
  then
    echo "  SETUP FAILED  ${COUNT}. $label"
    echo "                the defect snippet itself raised"
    MISS=$((MISS + 1)); MISSES+=("$label (setup failed)")
    rm -rf "$work"; return
  fi

  # Structural guard: the mutation must have changed something. A no-op edit
  # would make the suite pass for the wrong reason and be counted as a MISS
  # against an assertion that is actually fine.
  if cmp -s "$SRC/$file" "$work/$file"; then
    echo "  NO-OP EDIT    ${COUNT}. $label"
    echo "                pattern did not match; this assertion was NOT tested"
    MISS=$((MISS + 1)); MISSES+=("$label (no-op edit, pattern stale)")
    rm -rf "$work"; return
  fi

  out="$(cd "$work" && python3 shop_mcp.py --self-test 2>&1)"
  rc=$?

  if [ "$rc" -eq 0 ]; then
    echo "  MISS          ${COUNT}. $label"
    echo "                the suite passed with this defect present"
    MISS=$((MISS + 1)); MISSES+=("$label")
  else
    caught="$(printf '%s\n' "$out" | grep -m1 -- '  - ' | sed 's/^  - //' | cut -c1-96)"
    if [ -n "$expect" ] && ! printf '%s\n' "$out" | grep -q -- "$expect"; then
      echo "  WRONG CATCH   ${COUNT}. $label"
      echo "                expected an assertion matching: $expect"
      echo "                first failure was: $caught"
      MISS=$((MISS + 1)); MISSES+=("$label (caught by the wrong assertion)")
    else
      echo "  caught        ${COUNT}. $label"
      echo "                by: $caught"
      PASS=$((PASS + 1))
    fi
  fi
  rm -rf "$work"
}

echo "=== Baseline: the unmutated suite must be green ==="
if ! (cd "$SRC" && python3 shop_mcp.py --self-test >/dev/null 2>&1); then
  echo "BASELINE IS RED - fix the suite before trusting any mutation result"
  (cd "$SRC" && python3 shop_mcp.py --self-test 2>&1 | tail -20)
  exit 1
fi
BASE_COUNT="$(cd "$SRC" && python3 shop_mcp.py --self-test 2>&1 | grep -o 'all green: [0-9]*' | grep -o '[0-9]*')"
echo "  baseline green: $BASE_COUNT assertions"
echo

echo "=== Mutations: each must be caught ==="

# 1. The defect that breaks every stdio server: a diagnostic on stdout.
#    Invisible locally, corrupts the client's next parse.
run_mutation "log() writes to stdout instead of stderr" shop_mcp.py \
  "log() writes nothing to stdout" \
  's = s.replace("""print(f"[{stamp}] {message}", file=sys.stderr, flush=True)""", """print(f"[{stamp}] {message}", flush=True)""")'

# 2. Answering a notification. Puts a reply on the wire with no pending
#    request; strict clients treat it as a protocol violation.
run_mutation "notifications receive a reply" shop_mcp.py \
  "initialized notification gets no reply" \
  's = s.replace("""                log("client reported initialized")
            return None""", """                log("client reported initialized")
            return self._result(msg.get("id"), {})""")'

# 3. Tool failures as JSON-RPC errors. The model never sees the text, so it
#    cannot correct its own arguments.
run_mutation "tool failure becomes a JSON-RPC error instead of isError" shop_mcp.py \
  "an unknown tool is not a JSON-RPC error" \
  's = s.replace("""return self._tool_failure(str(exc))""", """raise""")'

# 4. Echoing an unknown protocol version back as agreed. The client then
#    believes a revision is in use that neither side implements.
run_mutation "unknown protocolVersion is echoed back as agreed" shop_mcp.py \
  "never echoed back as agreed" \
  's = s.replace("""self.negotiated_version = DEFAULT_NEGOTIATED_VERSION""", """self.negotiated_version = asked if isinstance(asked, str) else DEFAULT_NEGOTIATED_VERSION""")'

# 5. Off-by-one on the restock boundary: `<` instead of `<=`. Silently omits
#    the items sitting exactly on the reorder point.
run_mutation "low-stock threshold uses < instead of <=" shop_mcp.py \
  "a variant exactly at the threshold is included" \
  's = s.replace("""if not isinstance(qty, int) or qty > threshold:""", """if not isinstance(qty, int) or qty >= threshold:""")'

# 6. An unquoted SKU. A SKU with a space is read as two search terms and
#    matches the wrong variants. Anchored on `{"q": f`, which has no
#    backslash; the tail of the line is pushed into a comment.
run_mutation "SKU is not quoted in the search query" shop_mcp.py \
  "a SKU containing a space is quoted" \
  's = s.replace("""{"q": f""", """{"q": "sku:" + escaped, "n": limit})  # """)'

# 7. Treating an untracked (null) quantity as zero stock. Sends someone to
#    restock something that has no stock concept.
run_mutation "null inventory quantity is coerced to 0" shop_mcp.py \
  "a null quantity is not treated as zero stock" \
  's = s.replace("""qty = v.get("inventoryQuantity")""", """qty = v.get("inventoryQuantity") or 0""")'

# 8. Pretty-printing on the wire. Breaks newline framing, so the client reads
#    one message as many broken ones.
run_mutation "replies are pretty-printed onto the wire" shop_mcp.py \
  "still exactly one line" \
  's = s.replace("""ensure_ascii=False, separators=(",", ":")""", """ensure_ascii=False, indent=2""")'

# 9. Advertising a capability that is not implemented. The client calls
#    resources/list and gets method-not-found.
run_mutation "server advertises capabilities it does not implement" shop_mcp.py \
  "no unimplemented capability is advertised" \
  's = s.replace(""" "capabilities": {"tools": {"listChanged": False}},""", """ "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},""")'

# 10. A throttle not retried. Shopify answers 200 with a THROTTLED error, so
#     skipping that branch means the wait never happens.
run_mutation "THROTTLED response is not retried" shop_mcp.py \
  "a 200-with-THROTTLED is survivable" \
  's = s.replace("""if self._is_throttled(body):""", """if False and self._is_throttled(body):""")'

# 11. Retrying a 401. Waits and hammers on a token that will never work.
run_mutation "401 is retried like a transient error" shop_mcp.py \
  "a 401 is not retried" \
  's = s.replace("""if status in (429, 500, 502, 503, 504) and attempt <= self.max_retries:""", """if status in (401, 429, 500, 502, 503, 504) and attempt <= self.max_retries:""")'

# 12. Fixed backoff. A bounded retry count with no growth is just N rapid
#     failures against a limiter that needed time.
run_mutation "backoff is constant instead of increasing" shop_mcp.py \
  "backoff increases between retries" \
  's = s.replace("""delay = min(2.0 ** (attempt - 1), 8.0)""", """delay = 1.0""")'

# 13. id=0 read as a notification. Truthiness instead of presence: the first
#     request of a client that counts from zero is silently dropped.
run_mutation "id=0 is treated as a missing id" shop_mcp.py \
  "id=0 is a real request" \
  's = s.replace("""has_id = "id" in msg and msg["id"] is not None""", """has_id = bool(msg.get("id"))""")'

# 14. A crash inside a tool kills the loop instead of becoming isError.
#     Narrowing the catch lets RuntimeError escape to the protocol layer.
run_mutation "a tool crash propagates instead of becoming isError" shop_mcp.py \
  "a tool crash returns a result" \
  's = s.replace("""        except Exception as exc:  # noqa: BLE001
            log(f"tool {name} crashed: {exc!r}")""", """        except ConfigError as exc:  # noqa: BLE001
            log(f"tool {name} crashed: {exc!r}")""")'

# 15. An open input schema. A model that misspells an argument gets it
#     silently ignored instead of being told.
run_mutation "input schemas accept unknown properties" shop_mcp.py \
  "rejects unknown properties" \
  's = s.replace(""" "additionalProperties": False,""", """ "additionalProperties": True,""")'

# 16. Dropping the scan_exhausted signal. "Nothing is low" becomes
#     indistinguishable from "I did not look far enough".
run_mutation "scan_exhausted is hardcoded False" shop_mcp.py \
  "hitting the scan limit is reported" \
  's = s.replace(""" "scan_exhausted": len(variants) >= scan,""", """ "scan_exhausted": False,""")'

# 17. A self-test guard: if the suite stops finding its own subject, it must
#     fail rather than pass vacuously.
run_mutation "a tool is removed from the dispatch table" shop_mcp.py \
  "is a known tool in the dispatcher" \
  's = s.replace(""" "low_stock_report": self.low_stock_report,""", """ """)'

echo
echo "=== Result ==="
echo "  mutations: $COUNT"
echo "  caught: $PASS"
echo "  missed: $MISS"
if [ "$MISS" -gt 0 ]; then
  echo
  echo "These defects were NOT caught. The matching assertions are decoration:"
  for m in "${MISSES[@]}"; do echo "  - $m"; done
  exit 1
fi
echo "  every injected defect was caught by a named assertion"
exit 0
