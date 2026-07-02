# WhatsApp-to-Shopify Product Listing Automation
## System Design Document

This document outlines the production-ready system architecture, data models, integration workflows, and deployment guides for an automated WhatsApp-to-Shopify product listing pipeline. 

---

## 1. Executive Summary & Constraints Checklist

### Design Philosophy
This system is built as a **single-tenant internal tool** for a single operator/business. It eliminates all multi-tenant overhead (no merchant onboarding, no billing, no token isolation) in favor of high performance, ultra-low latency, and low hosting costs.

### Architectural Decisions at a Glance
| Decision Point | Chosen Solution | Tradeoff/Alternative | Rationale |
| :--- | :--- | :--- | :--- |
| **Orchestration** | FastAPI Async + BackgroundTasks / Celery + Redis | LangGraph / CrewAI | Prevents multi-agent overhead; maintains state via simple PostgreSQL states. |
| **Primary LLM** | Groq (`llama-3.2-11b-vision-preview` / `llama-3.3-70b-specdec`) | Claude Sonnet / GPT-4o | Near-zero latency and high token-per-second rates. |
| **Secondary LLM** | OpenRouter (`meta-llama/llama-3.2-90b-vision-instruct` / `qwen/qwen-2.5-vl-72b-instruct`) | Direct OpenAI API | Uniform API surface for multiple open-source fallbacks. |
| **Database** | PostgreSQL | MongoDB / NoSQL | Clean ACID support for transaction-oriented state tracking and robust auditing. |
| **Image Storage** | Cloudflare R2 (S3-compatible) | Local Disk / Shopify Staged Uploads | Shopify requires a stable public URL for remote image ingest, R2 offers zero egress fees. |

---

## 2. End-to-End Architecture

The system utilizes an event-driven architecture based on webhooks. Incoming messages from Meta Cloud API are processed asynchronously to avoid blockages on Meta’s webhook delivery (which requires a `< 3s` response).

### High-Level Architecture Flow
1. **WhatsApp Hook**: User sends media + optional text.
2. **Ingest Engine**: FastAPI receives Webhook, performs signature check, verifies sender, pushes task to Redis Queue (RQ) or Celery, and responds `200 OK`.
3. **Pipeline Worker**: 
    - Downloads image from Meta's servers.
    - Uploads image to Cloudflare R2.
    - Initiates **Vision Understanding Call** via LLM Gateway.
    - Initiates **Listing Copy Generator Call** via LLM Gateway.
4. **State Decision**:
    - *If price is provided*: Formulates payload, creates product in Shopify, uploads image, sends confirmation.
    - *If price is missing*: Generates market suggestion, posts message to database, sends price query WhatsApp back to the user, changes state to `AWAITING_PRICE_CONFIRMATION`.
5. **Confirmation Hook**: User responds to price suggestion; worker resumes, edits product details, and publishes.

### Component Architecture Diagram
```mermaid
graph TD
    User["WhatsApp Client"] <-->|"Send Media / Confirm Price"| Meta["Meta Cloud API"]
    Meta <-->|"Webhooks / Send Messages"| FastAPI["FastAPI Webhook Server"]
    
    subgraph Core Backend
        FastAPI -->|"Verify Signature & Save"| DB[(PostgreSQL)]
        FastAPI -->|"Enqueue Job"| Redis[(Redis Broker)]
        Redis <--> Worker["Async Task Worker (Celery/RQ)"]
    end
    
    subgraph External Integrations
        Worker -->|"Download Media"| MetaMedia["Meta Media Endpoint"]
        Worker -->|"Upload File"| Storage["Cloudflare R2 Storage"]
        Worker -->|"API Mutations"| Shopify["Shopify GraphQL Admin API"]
    end
    
    subgraph AI Pipeline
        Worker -->|"Analyze & Generate"| Gateway["LLM Gateway"]
        Gateway -->|"Primary (Low Latency)"| Groq["Groq Cloud API"]
        Gateway -->|"Secondary (Failover)"| OpenRouter["OpenRouter API"]
    end
```

### State Machine Lifecycle
```mermaid
stateDiagram-v2
    [*] --> RECEIVED : WhatsApp Image Received
    RECEIVED --> DOWNLOADING : Worker Takes Task
    DOWNLOADING --> ANALYZING : Image Saved to R2
    ANALYZING --> PRICE_CHECK : VLM Extraction Complete
    
    PRICE_CHECK --> PUBLISHING : Price in User Hint
    PRICE_CHECK --> AWAITING_PRICE : Price Missing
    
    AWAITING_PRICE --> WAITING_USER_INPUT : Send Suggestion SMS
    WAITING_USER_INPUT --> PRICE_RECEIVED : User Replies with Price
    PRICE_RECEIVED --> PUBLISHING : Extract Price
    
    PUBLISHING --> COMPLETED : Shopify API Success
    PUBLISHING --> FAILED : API Error
    FAILED --> [*]
    COMPLETED --> [*]
```

---

## 3. WhatsApp Integration (Meta Cloud API)

The system relies on the official Meta WhatsApp Business API. Below is the workflow for webhook validation, signature verification, and binary media downloading.

### Webhook Validation
FastAPI exposes `/webhooks/whatsapp` accepting both `GET` (for verification) and `POST` (for event delivery).

```python
from fastapi import FastAPI, Request, Response, HTTPException, status
import hmac
import hashlib
import os

app = FastAPI()

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

@app.get("/webhooks/whatsapp")
def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")
```

### Signature Verification (Security)
To ensure requests originate from Meta, the header `X-Hub-Signature-256` is validated using the system's App Secret.

```python
async def verify_signature(request: Request):
    signature_header = request.headers.get("X-Hub-Signature-256")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Signature missing")
    
    sha256_sig = signature_header.replace("sha256=", "")
    body = await request.body()
    
    expected_sig = hmac.new(
        key=WHATSAPP_APP_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(expected_sig, sha256_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")
```

### Media Retrieval Flow
When a user sends an image, Meta delivers a webhook payload containing a `media_id`. This ID must be exchanged for a source download URL, which requires a Bearer Token.

```mermaid
sequenceDiagram
    autonumber
    Participant User as User
    Participant Webhook as FastAPI Server
    Participant Meta as Meta Graph API
    
    User->>Webhook: Sends Image (contains Media ID)
    Webhook->>Meta: GET /v20.0/{media_id} (Headers: Auth Token)
    Meta-->>Webhook: Returns JSON (contains download URL)
    Webhook->>Meta: GET {download_url} (Headers: Auth Token)
    Meta-->>Webhook: Returns binary media stream
```

**Meta Media Download Snippet:**
```python
import httpx

async def get_media_url(media_id: str, access_token: str) -> str:
    url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()["url"]

async def download_media(media_url: str, access_token: str) -> bytes:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get(media_url, headers=headers)
        response.raise_for_status()
        return response.content
```

---

## 4. Shopify Integration

Because the system is configured as a single-tenant workspace, authentication uses a **Shopify Custom App Admin Access Token** (Admin API) rather than the standard OAuth installation flow. This avoids managing session states for third-party stores.

### Operations Strategy
We prefer Shopify’s **GraphQL Admin API** over the REST API due to payload efficiency, type safety, and the ability to upload images and set SEO attributes in a single request transaction.

### Shopify Product Creation Mutation
Below is the GraphQL payload structure for creating a product draft with media, pricing, attributes, and custom metafields.

```graphql
mutation productCreate($input: ProductInput!, $media: [CreateMediaInput!]) {
  productCreate(input: $input, media: $media) {
    product {
      id
      title
      handle
      onlineStoreUrl
      variants(first: 1) {
        edges {
          node {
            id
            price
            inventoryQuantity
          }
        }
      }
    }
    userErrors {
      field
      message
    }
  }
}
```

#### Variables Payload Example:
```json
{
  "input": {
    "title": "Vintage Hand-Burnished Leather Bi-fold Wallet",
    "descriptionHtml": "<p>Expertly hand-stitched from premium full-grain saddle leather...</p>",
    "productType": "Wallet",
    "vendor": "Internal Store",
    "tags": ["leather", "vintage", "bi-fold", "handmade"],
    "status": "ACTIVE",
    "seo": {
      "title": "Vintage Leather Wallet | Hand-Burnished Saddle Leather",
      "description": "Buy our handcrafted vintage leather bi-fold wallet. Full grain saddle leather with 6 card slots and currency compartment. Free shipping."
    },
    "variants": [
      {
        "price": "45.00",
        "sku": "VNTG-WLT-BRN",
        "inventoryPolicy": "DENY",
        "inventoryQuantities": [
          {
            "availableQuantity": 1,
            "locationId": "gid://shopify/Location/123456789"
          }
        ]
      }
    ]
  },
  "media": [
    {
      "mediaContentType": "IMAGE",
      "originalSource": "https://r2.yourdomain.com/listings/abc-123.jpg"
    }
  ]
}
```

---

## 5. AI Product Understanding & LLM Gateway

### VLM Comparison and Evaluation

We analyze four options for image interpretation and structural attribute mapping:

| Metric | Option A: GPT-4o | Option B: Gemini 2.5 Pro | Option C: Claude Sonnet | Option D: OS VLM Stack (Groq/OpenRouter) |
| :--- | :--- | :--- | :--- | :--- |
| **API Costs (1M tokens)** | $5.00 Input / $15.00 Output | $1.25 Input / $5.00 Output | $3.00 Input / $15.00 Output | **$0.15 Input / $0.60 Output** (Groq) |
| **Vision Accuracy** | Exceptionally High | High (Large Context) | Industry-Leading | High (Llama 3.2 90B/Qwen 2.5 VL) |
| **Latency** | 1.8s - 3.0s | 2.5s - 4.5s | 2.0s - 3.5s | **0.4s - 1.2s** (Groq Hardware) |
| **Compliance** | Proprietary | Proprietary | Proprietary | **Strictly Open Source** |

**Recommendation:**
For a rapid-deploy internal solution prioritizing cost and speed, **Option D (Groq + OpenRouter)** is the ideal solution. It respects our open-source constraint and provides sub-second latency.

---

### LLM Gateway Architecture
The system integrates an abstract LLM gateway implementing auto-retries, timeouts, and fallbacks.

```mermaid
graph TD
    App[Worker Process] --> Gateway[LLM Gateway client]
    Gateway -->|"Attempt 1"| GroqClient["Groq Client"]
    GroqClient -->|Success| Return[Return Structured Output]
    GroqClient -->|Timeout / 429 / Fail| Failover["Failover Handler"]
    Failover -->|"Attempt 2"| OpenRouterClient["OpenRouter Client"]
    OpenRouterClient -->|Success| Return
    OpenRouterClient -->|Fail| DLQ["Log to DB Audit & Fail Task"]
```

#### Implementation Code for LLM Gateway:
```python
import time
import httpx
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class LLMGateway:
    def __init__(self, groq_key: str, openrouter_key: str):
        self.groq_key = groq_key
        self.openrouter_key = openrouter_key
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

    async def generate_completion(
        self, 
        prompt: str, 
        image_url: str = None, 
        json_schema: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        
        # Payload Construction for Vision Model
        content = [{"type": "text", "text": prompt}]
        if image_url:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })
            
        # Target Models
        primary_model = "llama-3.2-11b-vision-preview"
        fallback_model = "meta-llama/llama-3.2-90b-vision-instruct"

        # Attempt 1: Groq
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                headers = {
                    "Authorization": f"Bearer {self.groq_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": primary_model,
                    "messages": [{"role": "user", "content": content}],
                    "response_format": {"type": "json_object"} if json_schema else {"type": "text"}
                }
                response = await client.post(self.groq_url, json=payload, headers=headers)
                if response.status_code == 200:
                    return response.json()
                logger.warning(f"Groq API returned error: {response.status_code}. Executing failover.")
            except (httpx.TimeoutException, httpx.RequestError) as e:
                logger.error(f"Groq request failed: {str(e)}. Executing failover.")

        # Attempt 2: Fallback to OpenRouter
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                headers = {
                    "Authorization": f"Bearer {self.openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://internal-shopify-listing.local",
                    "X-Title": "Shopify Listing Automation"
                }
                payload = {
                    "model": fallback_model,
                    "messages": [{"role": "user", "content": content}],
                    "response_format": {"type": "json_object"} if json_schema else {"type": "text"}
                }
                response = await client.post(self.openrouter_url, json=payload, headers=headers)
                if response.status_code == 200:
                    return response.json()
                raise Exception(f"OpenRouter Fallback Failed: {response.text}")
            except Exception as e:
                logger.critical(f"All LLM Providers failed: {str(e)}")
                raise e
```

---

## 6. Vision Interpretation & Listing Generation Prompts

To minimize API execution cost, the system collapses the workflow into two structured LLM passes:
1. **Vision Inference Pass**: Processes the image + user hints to output structured features.
2. **Copywriting & SEO Pass**: Uses these structured features to compose high-conversion HTML listings and tag libraries.

### Pass 1: Vision Model Prompt (JSON-targeted)

```
[System Role]
You are a expert e-commerce vision analyzer. Inspect the attached product image. Combine visual observation with the provided user text context: "{USER_HINT}".

[Tasks]
Extract the key structural specifications. Ensure you classify fields into:
1. Observed Attributes (what is clearly visible).
2. User-provided Attributes (derived from the hint).
3. Inferred Attributes (industry standard deductions).

[Output Schema]
Return ONLY a JSON object:
{
  "observed": {
    "colors": [],
    "condition_indicators": "description of wear/newness",
    "materials_visible": [],
    "branding_labels": []
  },
  "user_provided": {
    "hints_used": []
  },
  "inferred": {
    "product_category": "Standard Shopify category taxonomy",
    "product_type": "Specific item classification",
    "gender_style": "Unisex / Men / Women / Neutral",
    "estimated_retail_range": "e.g., $40-$60"
  }
}
```

### Pass 2: Copywriting Model Prompt (Text-targeted)

```
[System Role]
You are a highly persuasive copywriter specializing in conversion rate optimization (CRO) for Shopify stores. 

[Input Context]
Use the structured data from the vision analysis phase:
{VISION_DATA}

[Guidelines]
1. Write a professional, punchy product title (under 60 characters).
2. Write a comprehensive HTML product description containing:
   - A descriptive hook.
   - An HTML bulleted list highlighting key features.
   - Care instructions or dimensions (inferred).
3. Write an SEO Title and Meta Description (max 155 chars) optimized for Google Search.
4. Output 10 comma-separated product tags.

[Output Format]
Provide structured XML response blocks:
<title>[Product Title]</title>
<desc>[HTML Description]</desc>
<seotitle>[SEO Title]</seotitle>
<seodesc>[SEO Meta Description]</seodesc>
<tags>[Comma-separated tags]</tags>
```

---

## 7. Workflow State Orchestration & Pricing Logic

The execution flow relies on PostgreSQL for robust state persistence and status flags. The logic switches based on whether a valid price value can be extracted from the user's initial incoming payload.

### Logical Routing Sequence Diagram
```mermaid
sequenceDiagram
    autonumber
    actor User
    participant App as FastAPI Webhook Engine
    participant DB as PostgreSQL
    participant VLM as AI Layer
    participant Shopify as Shopify API

    User->>App: Sends Message (Image + Hint)
    App->>DB: Write message to raw logs
    App->>App: Check Hint for price pattern (e.g. '$45', 'price 45')
    
    alt Price Extracted
        App->>VLM: Run Vision & Generation (Combined Pass)
        VLM-->>App: Return Listing Payload + Metadata
        App->>Shopify: GraphQL Mutation: Create Active Product
        Shopify-->>App: Product URL (Published)
        App->>User: "Listed Successfully! URL: {link}"
    else Price Missing
        App->>VLM: Run Vision Analysis
        VLM-->>App: Return Structured Metadata + Suggested Price ($50)
        App->>DB: Save Listing Draft (Status: 'AWAITING_PRICE')
        App->>User: "Suggested Price: $50. Reply with final price to list."
        User->>App: Sends Reply message ("45")
        App->>DB: Query Draft state
        App->>VLM: Run Generation Pass with Final Price ($45)
        VLM-->>App: Return Completed Listing
        App->>Shopify: GraphQL Mutation: Create Active Product
        Shopify-->>App: Product URL (Published)
        App->>DB: Update state to 'PUBLISHED'
        App->>User: "Listed Successfully! URL: {link}"
    end
```

---

## 8. Database Design

Since this is a single-tenant deployment, we avoid multi-client workspace partitions. PostgreSQL is selected as the primary relational persistence tier.

### Entity Relationship Diagram (ERD)
```mermaid
erDiagram
    USERS ||--o{ WHATSAPP_MESSAGES : sends
    WHATSAPP_MESSAGES ||--o| LISTINGS : generates
    LISTINGS ||--o{ AUDIT_LOGS : records
    LISTINGS ||--o{ AGENT_EXECUTIONS : logs
    
    USERS {
        int id PK
        varchar phone_number UK
        varchar display_name
        boolean is_admin
        timestamp created_at
    }

    WHATSAPP_MESSAGES {
        int id PK
        varchar message_id UK
        varchar sender_phone
        varchar text_content
        varchar media_id
        varchar local_media_path
        timestamp received_at
    }

    LISTINGS {
        int id PK
        int message_id FK
        varchar title
        text description_html
        numeric suggested_price
        numeric final_price
        varchar tags
        varchar shopify_product_id
        varchar shopify_url
        varchar status "Draft / Awaiting Price / Published / Failed"
        timestamp created_at
    }

    AGENT_EXECUTIONS {
        int id PK
        int listing_id FK
        varchar stage_name "Vision / Copy / Publish"
        varchar provider_name "Groq / OpenRouter"
        numeric duration_ms
        text payload_sent
        text payload_received
        timestamp executed_at
    }

    AUDIT_LOGS {
        int id PK
        int listing_id FK
        varchar action_type
        varchar details
        timestamp logged_at
    }
```

### PostgreSQL DDL Schema Script
```sql
-- PostgreSQL DDL Script

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(30) UNIQUE NOT NULL,
    display_name VARCHAR(100),
    is_admin BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE whatsapp_messages (
    id SERIAL PRIMARY KEY,
    message_id VARCHAR(150) UNIQUE NOT NULL,
    sender_phone VARCHAR(30) NOT NULL,
    text_content TEXT,
    media_id VARCHAR(150),
    local_media_path VARCHAR(512),
    received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE listings (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES whatsapp_messages(id) ON DELETE SET NULL,
    title VARCHAR(255),
    description_html TEXT,
    suggested_price NUMERIC(10, 2),
    final_price NUMERIC(10, 2),
    tags TEXT,
    shopify_product_id VARCHAR(100),
    shopify_url VARCHAR(512),
    status VARCHAR(50) NOT NULL DEFAULT 'DRAFT', -- DRAFT, AWAITING_PRICE, PUBLISHED, FAILED
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_executions (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER REFERENCES listings(id) ON DELETE CASCADE,
    stage_name VARCHAR(100) NOT NULL, -- VISION_EXTRACTION, COPY_GENERATION, SHOPIFY_PUBLISH
    provider_name VARCHAR(100),
    duration_ms NUMERIC,
    payload_sent TEXT,
    payload_received TEXT,
    executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    action_type VARCHAR(100) NOT NULL,
    details TEXT,
    logged_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indices for performance tuning
CREATE INDEX idx_whatsapp_message_id ON whatsapp_messages(message_id);
CREATE INDEX idx_listings_status ON listings(status);
```

---

## 9. Product Image Processing Pipeline

Images sent to WhatsApp are highly compressed JPEGs. The pipeline must ingest, optimize, clean, and stage these media objects before pushing to Shopify.

### Processing Pipeline Stages
1. **Quality Check**: Run a check on the file dimensions and size. Reject low-resolution uploads (< 400px width/height).
2. **Metadata Stripping**: Strip EXIF tags (GPS, camera profiles) via Pillow to preserve uploader privacy.
3. **Resizing**: Resize images exceeding `2048px` on the longest edge while maintaining aspect ratio (Shopify optimizes for square `2048x2048` representations).
4. **Compression**: Re-compress to JPEG at quality 85% to balance file weight with visual clarity.
5. **Storage**: Write the optimized buffer to Cloudflare R2 and generate a public URL.

```python
from PIL import Image
import io

def optimize_product_image(raw_bytes: bytes) -> bytes:
    image = Image.open(io.BytesIO(raw_bytes))
    
    # Standardize image orientation via EXIF
    try:
        exif = image._getexif()
        if exif:
            # 274 is the orientation key
            orientation = exif.get(274)
            if orientation == 3:
                image = image.rotate(180, expand=True)
            elif orientation == 6:
                image = image.rotate(270, expand=True)
            elif orientation == 8:
                image = image.rotate(90, expand=True)
    except Exception:
        pass  # Skip if no orientation info exists
        
    # Resize if resolution exceeds limit
    max_dimension = 2048
    width, height = image.size
    if max(width, height) > max_dimension:
        if width > height:
            new_width = max_dimension
            new_height = int(height * (max_dimension / width))
        else:
            new_height = max_dimension
            new_width = int(width * (max_dimension / height))
        image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
    # Save optimized JPEG output
    output_buffer = io.BytesIO()
    image.convert("RGB").save(output_buffer, format="JPEG", quality=85)
    return output_buffer.getvalue()
```

---

## 10. Reliability, Production Queueing & Failover

To handle rate limits, network outages, and timeouts, we employ a queueing system with a Redis backend.

### Task Routing Architecture
FastAPI adds operations to the **Redis Queue (RQ)**. A detached service daemon processes jobs sequentially.

```mermaid
graph LR
    API[FastAPI Server] -->|Enqueue Job| Queue[Redis Task Queue]
    Queue -->|Process| Worker1[Worker Process]
    Worker1 -->|Success| Complete[Store/Client Update]
    Worker1 -->|Failure / Retry 3x| RetryHandler[Retry Logic]
    RetryHandler -->|Dead after 3 attempts| DLQ[(Dead Letter Queue DB)]
```

### Queue Configurations (Redis + Celery/RQ)
- **Retry Strategy**: Exponential backoff with jitter (`initial_delay = 5s, backoff_factor = 2`).
- **Maximum Retry Limit**: 3 attempts.
- **Task Timeout limit**: 60 seconds (prevents hangs during third-party media retrieval).
- **Dead-Letter Queue (DLQ)**: Failed listings are updated in the database with status `FAILED` alongside the traceback. A notification is sent to the admin's WhatsApp number.

---

## 11. Security Design

1. **Webhook Authentication**: Custom App Secret validation (HMAC SHA256) on every incoming Meta POST.
2. **Access Control**: Hardcoded Admin Phone whitelist inside the PostgreSQL database. Non-whitelist phone numbers triggering webhooks are ignored (or logged as security warnings in `audit_logs`).
3. **Token Management**: Meta Access tokens, Groq keys, and Shopify API keys are injected at runtime via Environment Variables. No secret configurations are committed to repository files.
4. **Storage Access**: Cloudflare R2 bucket is configured with private write permissions, but open read capabilities for files stored under the path prefix `/listings/*` to allow Shopify's media ingestion engine to fetch images.

---

## 12. Transaction Cost Analysis

The cost model below evaluates using **Llama 3.2 11B Vision** (via Groq) as the primary option and **Llama 3.2 90B Vision** (via OpenRouter) as the fallback option.

*Assumption: 1 Vision API call (approx. 1000 input tokens + image payload) + 1 Text generation API call (approx. 500 input tokens + 800 output tokens) = 2,300 tokens per listing.*

### Cost Matrix Table (USD)

| Operational Volume | API Token Costs (Groq) | Hosting Cost (Railway/Render) | Cloudflare R2 Storage (100MB/day) | Total Monthly Estimate |
| :--- | :--- | :--- | :--- | :--- |
| **100 Listings / Day** | $0.06 / day ($1.80/mo) | $7.00 / month (Free/Eco tier) | Free (within 10GB limit) | **$8.80 / mo** |
| **1,000 Listings / Day** | $0.60 / day ($18.00/mo) | $15.00 / month (Standard tier) | $0.40 / month | **$33.40 / mo** |
| **10,000 Listings / Day**| $6.00 / day ($180.00/mo) | $45.00 / month (Multi-instance) | $4.00 / month | **$229.00 / mo** |

### API Cost Optimizations
1. **Prompt Compaction**: Restrict output lengths for copy generation fields.
2. **Local Caching**: Check duplicate images (using hash comparisons) to skip duplicate vision classification calls.

---

## 13. MVP Implementation Roadmap

```mermaid
gantt
    title MVP Development Phases (4-Week Timeline)
    dateFormat  YYYY-MM-DD
    section Backend Setup
    DB Init & Webhook Handler        :active, des1, 2026-07-03, 5d
    Image Processing & R2 Uploader   :        des2, after des1, 4d
    section AI & API Gateway
    LLM Gateway Integration          :        des3, after des2, 4d
    Shopify GraphQL Integration      :        des4, after des3, 5d
    section Verification & Test
    UAT Testing & Flow Tuning        :        des5, after des4, 4d
    Deployment to VPS/Railway        :        des6, after des5, 3d
```

### Team Requirements
- **1 Lead Developer**: Competent with FastAPI, Python PIL, PostgreSQL, and LLM orchestration/integration.

---

## 14. Deployment & Infrastructure Setup Guide

This guide describes how to run and deploy the system on **Railway** (nearly free/low cost) or a standard Linux VPS.

### Environment Configuration (.env)
```bash
# General
ENV=production
DATABASE_URL=postgresql://user:password@host:port/database

# Security & Access
ADMIN_PHONE_WHITELIST=+12345678901,+19876543210
WHATSAPP_VERIFY_TOKEN=custom_random_token_string
WHATSAPP_APP_SECRET=meta_app_secret_value

# Meta APIs
WHATSAPP_ACCESS_TOKEN=meta_permanent_page_access_token
WHATSAPP_PHONE_NUMBER_ID=whatsapp_phone_number_id_from_console

# Shopify API
SHOPIFY_API_VERSION=2024-07
SHOPIFY_STORE_URL=your-store-name.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_custom_app_admin_access_token

# AI Providers
GROQ_API_KEY=gsk_groq_key_here
OPENROUTER_API_KEY=sk-or-openrouter_key_here

# Storage
R2_ACCESS_KEY_ID=r2_aws_key_id
R2_SECRET_ACCESS_KEY=r2_aws_secret_key
R2_BUCKET_NAME=shopify-listing-media
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
```

### Step-by-Step Deployment Flow

#### Step 1: Database Setup
Provision a PostgreSQL database. Execute the SQL statements from **Section 8** using `psql` or a migration framework like Alembic.

#### Step 2: Storage Configuration
1. Log in to Cloudflare and create an R2 bucket named `shopify-listing-media`.
2. Configure **Public Access** for the bucket, and point a custom domain or use the Cloudflare dev domain (e.g., `https://pub-xxx.r2.dev`).
3. Set the CORS policy to allow reads from Shopify:
   ```json
   [
     {
       "AllowedHeaders": ["*"],
       "AllowedMethods": ["GET"],
       "AllowedOrigins": ["*.myshopify.com", "*.shopify.com"],
       "MaxAgeSeconds": 3000
     }
   ]
   ```

#### Step 3: Meta App Setup
1. Log in to the Meta Developer Portal and create a Business App.
2. Add **WhatsApp** product.
3. Configure the webhook URL to point to `https://your-server.up.railway.app/webhooks/whatsapp` and use your `WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the webhook topic `messages`.

#### Step 4: Deployment to Railway
1. Click **New Project** -> **GitHub Repo**.
2. Bind the PostgreSQL service plugin to the project.
3. Bind a Redis service plugin to the project.
4. Input all variables in the **Variables** configuration tab.
5. Create a `Procfile` containing:
   ```yaml
   web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
   worker: celery -A app.worker worker --loglevel=info
   ```
6. Deploy the project. Railway automatically provisions SSL and assigns a public domain name.

---

## 15. Operational Readiness Checklist

- [ ] Confirm HMAC signature verification is active for `/webhooks/whatsapp`.
- [ ] Confirm incoming phone numbers are checked against the whitelisted administrators list.
- [ ] Set up alert notifications via WhatsApp for tasks routed to the Dead-Letter Queue (DLQ).
- [ ] Verify CORS configurations for the Cloudflare R2 bucket.
- [ ] Test the LLM Gateway failover flow by temporarily inputting an invalid Groq key and checking if the OpenRouter path executes.
