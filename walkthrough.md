# Walkthrough — Production-Readiness Refactoring

This walkthrough documents the fixes implemented based on the audit report findings.  
**Test result**: ✅ `21 passed, 18 warnings` in 2.21s

---

## Fixes Applied

### 1. DB Connection Retry on Startup  
**File**: [app/main.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/main.py)  
Added a `for attempt in range(1, 6)` retry loop around `Base.metadata.create_all()`. Waits 2 seconds between attempts with an exponential hold. Raises `RuntimeError` after 5 consecutive failures, preventing silent startup on a missing database.

### 2. Production Storage Guardrails  
**File**: [app/storage.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/storage.py)  
In `upload_product_image()`, a guard now raises `RuntimeError("Cloud storage credentials must be configured in production")` when `settings.ENV == "production"` and R2 credentials are not provided. R2 upload failures in production also raise immediately instead of falling back to ephemeral local storage.

### 3. LLM Gateway Exponential Backoff  
**File**: [app/gateway.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/gateway.py)  
Groq calls now retry 3 times before failing over to OpenRouter:
- Attempt 1 → immediate
- Attempt 2 → wait `2**1 = 2s`
- Attempt 3 → wait `2**2 = 4s`

Only HTTP 429, 500, 502, 503, 504, and network timeouts trigger retries. Non-retryable status codes break early.

### 4. Resilient XML Tag Parser  
**File**: [app/gateway.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/gateway.py)  
`parse_xml_tag()` upgraded to:
- Tolerate spaces inside tag brackets (`< title >`)
- Handle missing closing tags (extracts to next opening tag boundary)
- Three-tier fallback: full match → open-ended match → key-value line

### 5. Pricing Parser Tightening  
**File**: [app/worker.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/worker.py)  
Standalone numeric digit extraction now ignores values `< 5.0`. This prevents misinterpreting inventory counts (e.g., "selling 2 bags") as pricing inputs.

### 6. Pydantic Schema Validation on VLM Output  
**File**: [app/worker.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/worker.py)  
Three new Pydantic models validate vision extraction output before it reaches the copywriting pass:
- `ObservedAttributes` (colors, materials, condition, branding)
- `InferredAttributes` (category, type, suggested_price)
- `VisionExtractionResult` (root wrapper)

If the VLM returns a malformed or partial JSON object, `VisionExtractionResult(**raw_data)` raises a `ValidationError` that's caught and logged before any corrupted data enters the Shopify pipeline.

### 7. Pricing State Race Condition Fix  
**File**: [app/worker.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/worker.py)  
When a new image is received, the system now:
1. Queries any older `AWAITING_PRICE` listings for the same sender
2. Marks them as `FAILED` with audit reason `"SUPERSEDED"`
3. Sends a WhatsApp notification: *"Cancelling previous pending listing..."*

This removes the race condition where sending two images quickly would result in an unresolvable pricing state.

### 8. Webhook Batch Parser  
**File**: [app/whatsapp.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/whatsapp.py)  
`parse_whatsapp_webhook()` rebuilt from the ground up to:
- Return a `List` of parsed message tuples instead of a single optional tuple
- Iterate over **all** entries and changes in the payload (Meta can batch multiple events per delivery)
- Filter out non-actionable events (read receipts, status updates, reactions) silently
- Validate sender and message IDs before yielding any result

### 9. Main Webhook Loop Updated  
**File**: [app/main.py](file:///c:/Users/manan/OneDrive/Desktop/Shopify%20listing%20agent/app/main.py)  
The POST webhook handler now loops over the returned message list. Each message is independently checked for:
- Whitelist authorization
- Idempotency (duplicate `message_id`)
- Routing (image → new listing, text with pending → pricing, text → greeting)

---

## Test Suite Results — 21/21 ✅

| Test Group | Tests | Result |
| :--- | :--- | :--- |
| HMAC Signature Verification | 3 | ✅ Pass |
| Price Extraction Edge Cases | 7 | ✅ Pass |
| Image Preprocessing & Resize | 3 | ✅ Pass |
| Webhook Batch Parsing | 5 | ✅ Pass |
| Production Storage Guardrail | 1 | ✅ Pass |

```bash
$ python -m pytest tests/ -v
======================= 21 passed, 18 warnings in 2.21s =======================
```

---

## Pre-Production Remaining Items

The following items from the audit are non-blocking but recommended before sustained production use:

- [ ] Add Alembic migration tracking (`alembic init`, `alembic revision --autogenerate`)
- [ ] Add `structlog` JSON formatter replacing the current basic logging setup
- [ ] Add `SHOPIFY_PRODUCT_STATUS` env variable toggle (`ACTIVE` vs `DRAFT`) for approval workflows
