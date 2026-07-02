import hmac
import hashlib
import io
import pytest
from PIL import Image

# Patch settings before imports to prevent DB/env side effects
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test_secret")
os.environ.setdefault("ENV", "development")

from app.whatsapp import verify_meta_signature, parse_whatsapp_webhook
from app.worker import extract_price_from_text
from app.storage import optimize_image
from app.config import settings


# ---------------------------------------------------------------------------
# Test 1: HMAC Signature Verification
# ---------------------------------------------------------------------------
def test_signature_verification_valid():
    settings.WHATSAPP_APP_SECRET = "test_secret"
    body = b"hello_world_meta_payload"
    expected_hash = hmac.new(
        key=b"test_secret",
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    signature_header = f"sha256={expected_hash}"
    assert verify_meta_signature(body, signature_header) is True

def test_signature_verification_invalid():
    settings.WHATSAPP_APP_SECRET = "test_secret"
    body = b"hello_world_meta_payload"
    assert verify_meta_signature(body, "sha256=invalidhash") is False

def test_signature_verification_missing_header():
    body = b"hello_world_meta_payload"
    assert verify_meta_signature(body, None) is False


# ---------------------------------------------------------------------------
# Test 2: Price Extraction
# ---------------------------------------------------------------------------
def test_price_extraction_dollar_sign():
    assert extract_price_from_text("Vintage leather wallet $45") == 45.0

def test_price_extraction_usd_suffix():
    assert extract_price_from_text("selling for 35 USD") == 35.0

def test_price_extraction_dollars_word():
    assert extract_price_from_text("priced at 120 dollars") == 120.0

def test_price_extraction_standalone_digit():
    assert extract_price_from_text("leather boots 99") == 99.0

def test_price_extraction_decimal():
    assert extract_price_from_text("wallet for 12.50 bucks") == 12.50

def test_price_extraction_small_digit_ignored():
    # Digits < 5 should NOT be treated as price (they're likely quantities like 1, 2, 3)
    assert extract_price_from_text("selling 2 bags") is None

def test_price_extraction_no_price():
    assert extract_price_from_text("Nice brown wallet from the 80s") is None

def test_price_extraction_empty_text():
    assert extract_price_from_text("") is None

def test_price_extraction_none():
    assert extract_price_from_text(None) is None


# ---------------------------------------------------------------------------
# Test 3: Image Preprocessing
# ---------------------------------------------------------------------------
def test_image_resize_large_landscape():
    large_image = Image.new("RGB", (3000, 1500))
    buffer = io.BytesIO()
    large_image.save(buffer, format="JPEG")
    raw_bytes = buffer.getvalue()
    
    optimized = optimize_image(raw_bytes)
    decoded = Image.open(io.BytesIO(optimized))
    
    assert max(decoded.size) == 2048
    assert decoded.size == (2048, 1024)

def test_image_resize_large_portrait():
    large_image = Image.new("RGB", (800, 2400))
    buffer = io.BytesIO()
    large_image.save(buffer, format="JPEG")
    raw_bytes = buffer.getvalue()
    
    optimized = optimize_image(raw_bytes)
    decoded = Image.open(io.BytesIO(optimized))
    
    # Height is dominant, should be capped at 2048
    assert decoded.size[1] == 2048

def test_image_small_unchanged_dimensions():
    small_image = Image.new("RGB", (400, 300))
    buffer = io.BytesIO()
    small_image.save(buffer, format="JPEG")
    raw_bytes = buffer.getvalue()
    
    optimized = optimize_image(raw_bytes)
    decoded = Image.open(io.BytesIO(optimized))
    
    # Image is smaller than 2048, should not be upscaled
    assert decoded.size == (400, 300)


# ---------------------------------------------------------------------------
# Test 4: Webhook Batch Parsing
# ---------------------------------------------------------------------------
def test_webhook_parses_single_image():
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "msg_001",
                        "from": "+1234567890",
                        "type": "image",
                        "image": {"id": "media_abc", "caption": "Blue leather wallet"}
                    }]
                }
            }]
        }]
    }
    results = parse_whatsapp_webhook(payload)
    assert len(results) == 1
    msg_id, sender, msg_type, text, media_id = results[0]
    assert msg_id == "msg_001"
    assert sender == "+1234567890"
    assert msg_type == "image"
    assert media_id == "media_abc"
    assert text == "Blue leather wallet"

def test_webhook_parses_batched_messages():
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [
                        {
                            "id": "msg_001",
                            "from": "+111",
                            "type": "image",
                            "image": {"id": "media_001"}
                        },
                        {
                            "id": "msg_002",
                            "from": "+222",
                            "type": "text",
                            "text": {"body": "55"}
                        }
                    ]
                }
            }]
        }]
    }
    results = parse_whatsapp_webhook(payload)
    assert len(results) == 2
    assert results[0][0] == "msg_001"
    assert results[1][0] == "msg_002"

def test_webhook_ignores_status_events():
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "msg_001",
                        "from": "+1234567890",
                        "type": "read"  # status update, not actionable
                    }]
                }
            }]
        }]
    }
    results = parse_whatsapp_webhook(payload)
    assert len(results) == 0

def test_webhook_handles_empty_payload():
    results = parse_whatsapp_webhook({})
    assert results == []

def test_webhook_handles_no_messages():
    payload = {
        "entry": [{
            "changes": [{
                "value": {}  # no messages key
            }]
        }]
    }
    results = parse_whatsapp_webhook(payload)
    assert results == []


# ---------------------------------------------------------------------------
# Test 5: Production Storage Guardrail
# ---------------------------------------------------------------------------
def test_storage_production_raises_without_r2_config():
    settings.ENV = "production"
    settings.R2_ACCESS_KEY_ID = ""
    settings.R2_SECRET_ACCESS_KEY = ""
    settings.R2_ENDPOINT_URL = ""
    supabase_url = settings.SUPABASE_URL
    supabase_key = settings.SUPABASE_SERVICE_KEY
    settings.SUPABASE_URL = ""
    settings.SUPABASE_SERVICE_KEY = ""

    small_image = Image.new("RGB", (400, 300))
    buffer = io.BytesIO()
    small_image.save(buffer, format="JPEG")
    raw_bytes = buffer.getvalue()

    from app.storage import upload_product_image
    with pytest.raises(RuntimeError, match="credentials must be fully configured"):
        upload_product_image(raw_bytes, "test_product")

    # Restore for subsequent tests
    settings.ENV = "development"
    settings.SUPABASE_URL = supabase_url
    settings.SUPABASE_SERVICE_KEY = supabase_key
