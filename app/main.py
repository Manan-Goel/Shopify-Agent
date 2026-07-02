import logging
import os
import time
from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, engine, SessionLocal
from app.models import WhatsAppMessage, Listing
from app.whatsapp import (
    verify_meta_signature,
    parse_whatsapp_webhook,
    send_whatsapp_message
)
from app.worker import handle_new_listing_pipeline, handle_price_reply_pipeline

# Initialize Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("app.main")

# Auto-Create database tables on startup (Simple single-tenant initialization)
logger.info("Initializing database schemas...")
db_initialized = False
for attempt in range(1, 6):
    try:
        Base.metadata.create_all(bind=engine)
        db_initialized = True
        logger.info("Database schemas initialized successfully.")
        break
    except Exception as e:
        logger.warning(f"Database connection attempt {attempt} failed: {str(e)}. Retrying in 2 seconds...")
        time.sleep(2)

if not db_initialized:
    logger.critical("Could not connect to database after 5 attempts. Exiting.")
    raise RuntimeError("Database connection failure")

app = FastAPI(
    title="WhatsApp-to-Shopify Listing Automation Backend",
    version="1.0.0"
)

# CORS configurations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount local static files directory for local image fallback hosting
static_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
os.makedirs(static_path, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_path), name="static")

@app.get("/health")
def health_check():
    return {"status": "healthy", "env": settings.ENV}

@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(request: Request):
    """
    Validation handshake endpoint queried by Meta Graph API during subscription.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Meta Webhook verified successfully.")
        return Response(content=challenge, media_type="text/plain")
        
    logger.warning("Meta Webhook verification check failed.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, 
        detail="Verify token mismatch"
    )

@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(
    request: Request, 
    background_tasks: BackgroundTasks
):
    """
    Receives real-time user message events from Meta Graph API.
    """
    # 1. Read request body bytes for signature check
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    
    # Verify Meta signatures in production environments
    if settings.ENV == "production":
        if not verify_meta_signature(body, signature):
            logger.warning("Signature validation failed. Request unauthorized.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail="Invalid request signature"
            )
            
    # 2. Parse payload data
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid JSON payload"
        )
        
    logger.debug(f"Webhook received payload: {payload}")
    
    # Parse all messages from the payload (Meta may batch multiple in one delivery)
    parsed_messages = parse_whatsapp_webhook(payload)
    if not parsed_messages:
        # Return success to let Meta know we received the event (e.g. status updates, read receipts)
        return {"status": "skipped", "reason": "non-message-event"}

    enqueued_count = 0
    db: Session = SessionLocal()
    try:
        for message_id, sender_phone, msg_type, text_content, media_id in parsed_messages:
            # 3. Check Admin Phone Whitelist (Access Control) per sender
            sender_clean = sender_phone.strip().replace("+", "")
            whitelisted = [p.replace("+", "").strip() for p in settings.admin_phone_whitelist]
            
            if whitelisted and (sender_clean not in whitelisted):
                logger.warning(f"Ignored message from unauthorized phone number: {sender_phone}")
                continue  # Skip this message, process next in batch

            # 4. Idempotency check per message_id
            existing_msg = db.query(WhatsAppMessage).filter(
                WhatsAppMessage.message_id == message_id
            ).first()
            if existing_msg:
                logger.info(f"Duplicate message_id received: {message_id}. Skipping.")
                continue

            # 5. Route task to background queue
            if msg_type == "image" and media_id:
                logger.info(f"New image listing request enqueued for: {sender_phone}")
                background_tasks.add_task(
                    handle_new_listing_pipeline,
                    sender_phone=sender_phone,
                    message_id=message_id,
                    media_id=media_id,
                    text_content=text_content
                )
                enqueued_count += 1
                
            elif msg_type == "text" and text_content:
                # Verify if this sender has a listing awaiting price input
                pending_listing = db.query(Listing)\
                    .join(WhatsAppMessage, Listing.message_id == WhatsAppMessage.id)\
                    .filter(WhatsAppMessage.sender_phone == sender_phone)\
                    .filter(Listing.status == "AWAITING_PRICE")\
                    .first()
                    
                if pending_listing:
                    logger.info(f"Price reply message enqueued for: {sender_phone}")
                    background_tasks.add_task(
                        handle_price_reply_pipeline,
                        sender_phone=sender_phone,
                        message_id=message_id,
                        text_content=text_content
                    )
                    enqueued_count += 1
                else:
                    # General greeting response
                    logger.info(f"Non-workflow text from {sender_phone}: {text_content}")
                    background_tasks.add_task(
                        send_whatsapp_message,
                        to_phone=sender_phone,
                        text="Hello! To list an item on Shopify, send a photo of the product with an optional description context."
                    )
                    enqueued_count += 1
                    
    except Exception as e:
        logger.error(f"Error handling webhook request routing: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    finally:
        db.close()

    return {"status": "received", "enqueued": enqueued_count}
