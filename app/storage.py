import io
import os
import uuid
import logging
import boto3
import httpx
from PIL import Image
from botocore.exceptions import ClientError
from app.config import settings

logger = logging.getLogger(__name__)

# Ensure local static directory exists for local development fallback
LOCAL_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
os.makedirs(LOCAL_STATIC_DIR, exist_ok=True)

def optimize_image(raw_bytes: bytes) -> bytes:
    """
    Strips EXIF data, corrects orientation, downsamples long-edges to max 2048px, 
    and returns compressed JPEG bytes.
    """
    image = Image.open(io.BytesIO(raw_bytes))
    
    # Correct orientation using EXIF information
    try:
        exif = image._getexif()
        if exif:
            orientation = exif.get(274)
            if orientation == 3:
                image = image.rotate(180, expand=True)
            elif orientation == 6:
                image = image.rotate(270, expand=True)
            elif orientation == 8:
                image = image.rotate(90, expand=True)
    except Exception as e:
        logger.warning(f"Could not read/apply EXIF orientation: {str(e)}")
        
    # Resize keeping aspect ratio if max dimension exceeds 2048px
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
        logger.info(f"Image resized from {width}x{height} to {new_width}x{new_height}")
        
    output_buffer = io.BytesIO()
    # Convert to RGB to ensure saving as JPEG is successful
    image.convert("RGB").save(output_buffer, format="JPEG", quality=85)
    return output_buffer.getvalue()

def upload_to_supabase(optimized_bytes: bytes, filename: str) -> str:
    """
    Uploads optimized image bytes to a Supabase Storage bucket via its REST API
    and returns the public object URL.
    """
    upload_url = f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/{settings.SUPABASE_STORAGE_BUCKET}/{filename}"
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_KEY,
        "Content-Type": "image/jpeg",
    }
    response = httpx.post(upload_url, headers=headers, content=optimized_bytes, timeout=15.0)
    response.raise_for_status()

    logger.info(f"Uploaded image to Supabase Storage: {filename}")
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{settings.SUPABASE_STORAGE_BUCKET}/{filename}"

def upload_product_image(image_bytes: bytes, filename_prefix: str = "product") -> str:
    """
    Uploads optimized image to Supabase Storage or Cloudflare R2, preferring Supabase
    when configured. If neither is set, falls back to saving locally and returns a
    local server path.
    """
    optimized_bytes = optimize_image(image_bytes)
    filename = f"{filename_prefix}_{uuid.uuid4().hex}.jpg"

    # Prefer Supabase Storage when configured
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY:
        try:
            return upload_to_supabase(optimized_bytes, filename)
        except Exception as e:
            logger.error(f"Supabase Storage upload failed: {str(e)}")
            if settings.ENV == "production":
                raise RuntimeError(f"Failed to upload product image to Supabase Storage: {str(e)}")
            # Fall through to local storage below

    # Fallback to local files if credentials are not configured
    if not (settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY and settings.R2_ENDPOINT_URL):
        if settings.ENV == "production":
            raise RuntimeError("Cloud storage (R2/S3) credentials must be fully configured in production environment.")
        logger.info("R2 Credentials not fully configured. Storing image locally.")
        local_path = os.path.join(LOCAL_STATIC_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(optimized_bytes)
        
        # Local development fallback URL structure.
        # This will be resolved based on the host URL in main.py, e.g. http://localhost:8000/static/{filename}
        return f"/static/{filename}"

    # Setup S3 Client pointing to Cloudflare R2
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        endpoint_url=settings.R2_ENDPOINT_URL,
    )

    try:
        # Upload file to R2
        s3_client.put_object(
            Bucket=settings.R2_BUCKET_NAME,
            Key=filename,
            Body=optimized_bytes,
            ContentType="image/jpeg"
        )
        logger.info(f"Uploaded image to R2: {filename}")
        
        # Resolve public URL prefix
        public_prefix = settings.R2_PUBLIC_URL_PREFIX.rstrip("/")
        if not public_prefix:
            public_prefix = f"https://{settings.R2_BUCKET_NAME}.r2.cloudflarestorage.com"
            
        return f"{public_prefix}/{filename}"
        
    except Exception as e:
        logger.error(f"S3/R2 upload failed: {str(e)}")
        if settings.ENV == "production":
            raise RuntimeError(f"Failed to upload product image to R2: {str(e)}")
        # Double fallback to local for dev/test
        local_path = os.path.join(LOCAL_STATIC_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(optimized_bytes)
        return f"/static/{filename}"
