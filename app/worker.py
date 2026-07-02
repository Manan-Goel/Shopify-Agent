import logging
import re
import time
import json
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.config import settings
from app.database import SessionLocal
from app.models import WhatsAppMessage, Listing, AgentExecution, AuditLog, User
from app.storage import upload_product_image
from app.gateway import LLMGateway
from app.shopify import create_shopify_product
from app.whatsapp import (
    download_meta_media,
    get_meta_media_url,
    send_whatsapp_message
)

logger = logging.getLogger(__name__)

# Initialize LLM Gateway
llm_gateway = LLMGateway(
    groq_key=settings.GROQ_API_KEY,
    openrouter_key=settings.OPENROUTER_API_KEY
)

class ObservedAttributes(BaseModel):
    colors: List[str] = Field(default_factory=list)
    condition_indicators: str = ""
    materials_visible: List[str] = Field(default_factory=list)
    branding_labels: List[str] = Field(default_factory=list)

class InferredAttributes(BaseModel):
    product_category: str = ""
    product_type: str = ""
    suggested_price: float = 0.0

class VisionExtractionResult(BaseModel):
    observed: ObservedAttributes
    inferred: InferredAttributes

def extract_price_from_text(text: str) -> Optional[float]:
    """
    Attempts to parse a pricing number from raw text context.
    Matches formats like $45, 45.00, 45 USD, or simple standalone digits '45'.
    """
    if not text:
        return None
        
    # Search for dollar values e.g. $45.99
    dollar_match = re.search(r'\$\s*(\d+(?:\.\d{2})?)', text)
    if dollar_match:
        return float(dollar_match.group(1))
        
    # Search for trailing USD currency designations e.g. 45 usd
    usd_match = re.search(r'(\d+(?:\.\d{2})?)\s*(?:usd|dollars|bucks|rs)', text, re.IGNORECASE)
    if usd_match:
        return float(usd_match.group(1))

    # General extraction of standalone digit sequences (ignore values < 5 to prevent count misinterpretation)
    digits_match = re.search(r'\b(\d+(?:\.\d{2})?)\b', text)
    if digits_match:
        val = float(digits_match.group(1))
        if val >= 5.0:
            return val
        
    return None

async def run_vision_inference(image_url: str, hint: str) -> Dict[str, Any]:
    """
    Calls the vision model to detect product details, categories, colors,
    and returns a structured JSON object.
    """
    prompt = f"""
    [System Role]
    You are an expert e-commerce vision analyzer. Inspect the attached product image. Combine visual observation with the provided user text context: "{hint or 'None'}".

    [Tasks]
    Extract key product attributes. You must return a suggested market price (integer/float) in the "suggested_price" field based on estimated resale value.

    [Output Format]
    Return ONLY a raw JSON object matching this schema. No markdown wrappers.
    {{
      "observed": {{
        "colors": ["list of colors"],
        "condition_indicators": "description of wear/newness",
        "materials_visible": ["list of materials"],
        "branding_labels": ["detected brands"]
      }},
      "inferred": {{
        "product_category": "Standard Shopify category taxonomy",
        "product_type": "Specific item type e.g. Wallet, Shoe, Shirt",
        "suggested_price": 50.00
      }}
    }}
    """
    # Trigger LLM Gateway vision call
    response = await llm_gateway.call_llm(
        prompt=prompt,
        image_url=image_url,
        force_json=True
    )
    
    try:
        # Parse JSON output
        clean_text = response.text.strip()
        # Handle cases where model wraps output in markdown code blocks
        clean_text = re.sub(r'^```json\s*|\s*```$', '', clean_text, flags=re.MULTILINE)
        raw_data = json.loads(clean_text)
        
        # Enforce validation schema using Pydantic
        validated_data = VisionExtractionResult(**raw_data)
        data = validated_data.model_dump()
        
        data["_meta"] = {
            "provider": response.provider,
            "model": response.model,
            "latency_ms": response.latency_ms
        }
        return data
    except Exception as e:
        logger.error(f"Failed to parse or validate Vision output JSON. Raw response: {response.text}")
        raise RuntimeError(f"JSON Parse/Validation Error: {str(e)}")

async def run_copywriting_generation(vision_data: Dict[str, Any], final_price: float) -> Dict[str, str]:
    """
    Calls text model to write conversion-optimized copy, SEO descriptions, and titles.
    """
    prompt = f"""
    [System Role]
    You are a professional conversion-focused e-commerce copywriter.

    [Input Context]
    Vision Analysis Attributes: {json.dumps(vision_data)}
    Final Price to List: ${final_price:.2f}

    [Guidelines]
    Write a persuasive Shopify product listing. Keep the title punchy and under 60 characters.
    Provide an HTML description showing bullet points. Do not mention shipping details.
    Generate a 155-character meta description for search engines.
    Generate 10 relevant comma-separated listing tags.

    [Output Format]
    Provide structured XML response blocks matching this format:
    <title>Product Title</title>
    <desc><p>HTML description here...</p></desc>
    <seotitle>SEO Search Title</seotitle>
    <seodesc>SEO search meta description</seodesc>
    <tags>tag1, tag2, tag3</tags>
    """
    
    response = await llm_gateway.call_llm(
        prompt=prompt,
        system_prompt="You are a professional copywriting assistant. Output structured XML blocks."
    )
    
    # Parse tags using custom XML extractor
    title = llm_gateway.parse_xml_tag(response.text, "title")
    desc = llm_gateway.parse_xml_tag(response.text, "desc")
    seotitle = llm_gateway.parse_xml_tag(response.text, "seotitle")
    seodesc = llm_gateway.parse_xml_tag(response.text, "seodesc")
    tags_raw = llm_gateway.parse_xml_tag(response.text, "tags")
    
    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
    
    return {
        "title": title or "Persuasive Product Listing",
        "description_html": desc or "<p>Handcrafted quality product listing.</p>",
        "seotitle": seotitle,
        "seodesc": seodesc,
        "tags": tags,
        "_meta": {
            "provider": response.provider,
            "model": response.model,
            "latency_ms": response.latency_ms
        }
    }

async def handle_new_listing_pipeline(
    sender_phone: str,
    message_id: str,
    media_id: str,
    text_content: Optional[str]
):
    """
    Main background workflow executing image downloads, optimization,
    multimodal analysis, and conditional pricing logic.
    """
    db: Session = SessionLocal()
    try:
        # 1. Log incoming message
        msg_record = WhatsAppMessage(
            message_id=message_id,
            sender_phone=sender_phone,
            text_content=text_content,
            media_id=media_id
        )
        db.add(msg_record)
        db.commit()
        db.refresh(msg_record)

        # Cancel older pending listings in AWAITING_PRICE for this sender to prevent race conditions
        old_listings = db.query(Listing)\
            .join(WhatsAppMessage, Listing.message_id == WhatsAppMessage.id)\
            .filter(WhatsAppMessage.sender_phone == sender_phone)\
            .filter(Listing.status == "AWAITING_PRICE")\
            .all()
            
        for old in old_listings:
            old.status = "FAILED"
            db.add(AuditLog(
                listing_id=old.id,
                action_type="SUPERSEDED",
                details="Superseded by a newer image upload. Discarding pricing wait state."
            ))
        db.commit()
        if old_listings:
            await send_whatsapp_message(
                sender_phone, 
                "⚠️ Cancelling previous pending listing for which price was not confirmed."
            )

        # 2. Inform user analysis has started
        await send_whatsapp_message(sender_phone, "Analyzing your image... please wait a moment.")

        # 3. Retrieve media from Meta CDN
        media_url = await get_meta_media_url(media_id)
        raw_image_bytes = await download_meta_media(media_url)

        # 4. Process and upload image (R2 or local static server)
        public_image_url = upload_product_image(raw_image_bytes, filename_prefix=f"msg_{message_id}")
        
        # If stored locally (e.g. /static/foo.jpg), ensure we form absolute URL
        if public_image_url.startswith("/static/"):
            # Resolve against local host when running Shopify queries (local endpoints will need ngrok)
            logger.info("Local storage fallback URL detected.")
            # Default placeholder context for local hosting.
            # In production, R2 endpoint will form absolute links.
            
        msg_record.local_media_path = public_image_url
        db.commit()

        # 5. Extract price from text hints
        price = extract_price_from_text(text_content)

        # 6. Run VLM Inference
        start_time = time.time()
        vision_result = await run_vision_inference(public_image_url, text_content)
        vision_latency = (time.time() - start_time) * 1000

        # Create Listing draft object
        listing = Listing(
            message_id=msg_record.id,
            suggested_price=vision_result.get("inferred", {}).get("suggested_price", 0.0),
            status="DRAFT"
        )
        db.add(listing)
        db.commit()
        db.refresh(listing)

        # Write execution audit
        exec_record = AgentExecution(
            listing_id=listing.id,
            stage_name="VISION_EXTRACTION",
            provider_name=vision_result.get("_meta", {}).get("provider", "Unknown"),
            duration_ms=vision_latency,
            payload_sent=f"Image URL: {public_image_url}, Hint: {text_content}",
            payload_received=json.dumps(vision_result)
        )
        db.add(exec_record)
        db.commit()

        # Route dynamically depending on price availability
        if price is not None:
            # Direct listing creation flow
            listing.final_price = price
            db.commit()
            
            await publish_listing_to_shopify(db, listing, vision_result, public_image_url, sender_phone)
        else:
            # Ask user for pricing confirmation
            suggested = listing.suggested_price
            listing.status = "AWAITING_PRICE"
            # Serialize vision result inside DB (use tags field or audit logs to hold draft payload)
            listing.tags = json.dumps(vision_result) # Temporary storage of attributes
            db.commit()

            audit = AuditLog(
                listing_id=listing.id,
                action_type="AWAITING_PRICE_CONFIRMATION",
                details=f"Suggested market price determined: ${suggested:.2f}. Prompting user."
            )
            db.add(audit)
            db.commit()

            message_prompt = (
                f"I've processed the item. Suggested retail price is: *${suggested:.2f}*.\n\n"
                "To list this product, please reply directly with the final listing price (digits only, e.g. '45' or '50')."
            )
            await send_whatsapp_message(sender_phone, message_prompt)

    except Exception as e:
        logger.error(f"Error running listing pipeline: {str(e)}", exc_info=True)
        await send_whatsapp_message(sender_phone, "Oops, I ran into a technical issue processing this item. Please try again.")
    finally:
        db.close()

async def handle_price_reply_pipeline(
    sender_phone: str,
    message_id: str,
    text_content: str
):
    """
    Resumes listing workflow for drafts holding in AWAITING_PRICE state.
    """
    db: Session = SessionLocal()
    try:
        # Find latest pending draft
        listing = db.query(Listing)\
            .join(WhatsAppMessage, Listing.message_id == WhatsAppMessage.id)\
            .filter(WhatsAppMessage.sender_phone == sender_phone)\
            .filter(Listing.status == "AWAITING_PRICE")\
            .order_by(Listing.created_at.desc())\
            .first()

        if not listing:
            # No active workflow, suggest user upload an image first
            await send_whatsapp_message(
                sender_phone, 
                "No pending listing found. Please send a product image first to begin."
            )
            return

        final_price = extract_price_from_text(text_content)
        if final_price is None:
            await send_whatsapp_message(
                sender_phone,
                "I couldn't parse that price. Please reply with just the listing digits (e.g. '35')."
            )
            return

        # Advance state
        listing.final_price = final_price
        listing.status = "GENERATING_COPY"
        db.commit()

        # Inform user
        await send_whatsapp_message(sender_phone, f"Setting price to ${final_price:.2f}. Generating copy and publishing...")

        # Re-load stored VLM result from temporary tags column
        try:
            vision_result = json.loads(listing.tags)
        except Exception:
            vision_result = {"observed": {}, "inferred": {}}

        # Log new inbound message metadata
        msg_record = WhatsAppMessage(
            message_id=message_id,
            sender_phone=sender_phone,
            text_content=text_content
        )
        db.add(msg_record)
        db.commit()

        # Get image URL from historical message
        original_msg = db.query(WhatsAppMessage).filter(WhatsAppMessage.id == listing.message_id).first()
        image_url = original_msg.local_media_path if original_msg else ""

        await publish_listing_to_shopify(db, listing, vision_result, image_url, sender_phone)

    except Exception as e:
        logger.error(f"Error handling pricing response: {str(e)}", exc_info=True)
        await send_whatsapp_message(sender_phone, "Error finalizing your listing. Please try again.")
    finally:
        db.close()

async def publish_listing_to_shopify(
    db: Session,
    listing: Listing,
    vision_result: Dict[str, Any],
    image_url: str,
    recipient_phone: str
):
    """
    Subroutine executing copywriting agent pass and publishing mutations to Shopify.
    """
    start_time = time.time()
    copy_result = await run_copywriting_generation(vision_result, float(listing.final_price))
    copy_latency = (time.time() - start_time) * 1000

    # Log copywriting step
    exec_record = AgentExecution(
        listing_id=listing.id,
        stage_name="COPY_GENERATION",
        provider_name=copy_result.get("_meta", {}).get("provider", "Unknown"),
        duration_ms=copy_latency,
        payload_sent=json.dumps(vision_result),
        payload_received=json.dumps(copy_result)
    )
    db.add(exec_record)
    db.commit()

    # Publish to Shopify
    try:
        # Format tags as plain string for SQL persistence, save array for API
        tags_list = copy_result.get("tags", [])
        listing.title = copy_result.get("title")
        listing.description_html = copy_result.get("description_html")
        listing.tags = ", ".join(tags_list)
        db.commit()

        # Standardize local static URLs for Shopify (requires public access tunnels like ngrok for testing)
        shopify_media_url = image_url
        if image_url.startswith("/static/") and settings.R2_PUBLIC_URL_PREFIX:
            shopify_media_url = f"{settings.R2_PUBLIC_URL_PREFIX.rstrip('/')}{image_url}"

        shopify_prod = await create_shopify_product(
            title=listing.title,
            description_html=listing.description_html,
            price=float(listing.final_price),
            tags=tags_list,
            image_url=shopify_media_url,
            product_type=vision_result.get("inferred", {}).get("product_type", "General")
        )

        # Update final state
        listing.shopify_product_id = shopify_prod["id"]
        listing.shopify_url = shopify_prod["url"]
        listing.status = "PUBLISHED"
        db.commit()

        audit = AuditLog(
            listing_id=listing.id,
            action_type="PUBLISHED_SUCCESSFULLY",
            details=f"Product published on Shopify: {shopify_prod['url']}"
        )
        db.add(audit)
        db.commit()

        # Send confirmation to user
        success_message = (
            "🎉 Your product has been listed successfully!\n\n"
            f"🛒 *Product Title*: {listing.title}\n"
            f"💰 *Price*: ${float(listing.final_price):.2f}\n\n"
            f"Product Link:\n{shopify_prod['url']}"
        )
        await send_whatsapp_message(recipient_phone, success_message)

    except Exception as e:
        listing.status = "FAILED"
        db.commit()
        
        audit = AuditLog(
            listing_id=listing.id,
            action_type="PUBLISH_FAILED",
            details=f"Shopify publish failure details: {str(e)}"
        )
        db.add(audit)
        db.commit()
        raise e
