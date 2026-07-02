import hmac
import hashlib
import logging
import httpx
from typing import Dict, Any, Optional, Tuple, List
from app.config import settings

logger = logging.getLogger(__name__)

def verify_meta_signature(body: bytes, signature_header: Optional[str]) -> bool:
    """
    Validates X-Hub-Signature-256 header sent by Meta Webhooks to verify source authenticity.
    """
    if not signature_header:
        logger.warning("Meta signature validation failed: Signature header missing.")
        return False
        
    try:
        # Signature format is: sha256=hashval
        sha256_sig = signature_header.replace("sha256=", "")
        expected_sig = hmac.new(
            key=settings.WHATSAPP_APP_SECRET.encode("utf-8"),
            msg=body,
            digestmod=hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_sig, sha256_sig)
    except Exception as e:
        logger.error(f"Error validating signature: {str(e)}")
        return False

async def get_meta_media_url(media_id: str) -> str:
    """
    Exchanges a WhatsApp Media ID for its file retrieval URL.
    """
    url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(f"Failed to fetch media metadata from Meta API: {response.text}")
            response.raise_for_status()
        return response.json()["url"]

async def download_meta_media(media_url: str) -> bytes:
    """
    Downloads raw binary bytes of an image from Meta CDN using authenticated redirect headers.
    """
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(media_url, headers=headers)
        if response.status_code != 200:
            logger.error(f"Failed to download media binary stream: {response.text}")
            response.raise_for_status()
        return response.content

async def send_whatsapp_message(to_phone: str, text: str) -> Dict[str, Any]:
    """
    Sends an outbound text message to a user via Meta Graph Cloud API.
    """
    if not settings.WHATSAPP_PHONE_NUMBER_ID or not settings.WHATSAPP_ACCESS_TOKEN:
        logger.warning(f"Meta outbound keys missing. Simulating sending text to {to_phone}: {text}")
        return {"simulated": True, "recipient": to_phone, "content": text}
        
    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code not in [200, 201]:
            logger.error(f"Error sending WhatsApp message to {to_phone}: {response.text}")
            response.raise_for_status()
        return response.json()

def _extract_single_message(
    message: Dict[str, Any]
) -> Optional[Tuple[str, str, str, Optional[str], Optional[str]]]:
    """Parse a single message dict into a structured tuple. Returns None for non-actionable events."""
    msg_type = message.get("type")
    if not msg_type:
        return None
    
    message_id = message.get("id")
    sender_phone = message.get("from")
    
    if not message_id or not sender_phone:
        return None
    
    text_body = None
    media_id = None
    
    if msg_type == "text":
        text_body = message.get("text", {}).get("body")
    elif msg_type == "image":
        image_data = message.get("image", {})
        media_id = image_data.get("id")
        # Caption is the optional text context sent alongside an image
        text_body = image_data.get("caption")
    else:
        # Ignore status updates, reactions, read receipts, etc.
        return None
    
    return message_id, sender_phone, msg_type, text_body, media_id


def parse_whatsapp_webhook(
    payload: Dict[str, Any]
) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    """
    Parses all messages from an incoming Meta WhatsApp webhook payload.
    Iterates over all batched entries and changes to avoid dropping messages.
    Returns a list of (message_id, sender_phone, type, text_body, media_id) tuples.
    """
    parsed_messages = []
    
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    result = _extract_single_message(message)
                    if result:
                        parsed_messages.append(result)
    except Exception as e:
        logger.error(f"Error parsing webhook payload: {str(e)}")
    
    return parsed_messages
