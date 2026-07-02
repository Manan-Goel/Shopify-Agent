import time
import asyncio
import httpx
import logging
from typing import Dict, Any, List, Optional
from app.config import settings

logger = logging.getLogger(__name__)

_token_cache = {"token": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()

async def get_access_token() -> str:
    """
    Returns a valid Shopify Admin API access token.
    If a static SHOPIFY_ACCESS_TOKEN is configured, uses it directly. Otherwise,
    fetches a token via the client_credentials grant (Dev Dashboard app flow) and
    caches it in memory, refreshing 5 minutes before its ~24h expiry.
    """
    if settings.SHOPIFY_ACCESS_TOKEN:
        return settings.SHOPIFY_ACCESS_TOKEN

    async with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]

        token_url = f"https://{settings.SHOPIFY_STORE_URL}/admin/oauth/access_token"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                token_url,
                json={
                    "client_id": settings.SHOPIFY_CLIENT_ID,
                    "client_secret": settings.SHOPIFY_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()

        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"] - 300
        logger.info("Fetched new Shopify Admin API access token via client_credentials grant.")
        return _token_cache["token"]

async def create_shopify_product(
    title: str,
    description_html: str,
    price: float,
    tags: List[str],
    image_url: str,
    product_type: str = "",
    vendor: str = "WhatsApp Agent"
) -> Dict[str, Any]:
    """
    Creates an ACTIVE product on Shopify using the Admin GraphQL API.
    Injects pricing, descriptions, and sets a remote R2 image source.
    """
    url = f"https://{settings.SHOPIFY_STORE_URL}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
    access_token = await get_access_token()
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }

    # ProductInput no longer accepts variants directly as of this API version - the
    # product is created first (with an auto-generated default variant), then the
    # default variant's price is set via a separate productVariantsBulkUpdate call.
    create_query = """
    mutation productCreate($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
      productCreate(product: $product, media: $media) {
        product {
          id
          title
          handle
          onlineStoreUrl
          variants(first: 1) {
            edges {
              node {
                id
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
    """

    create_variables = {
      "product": {
        "title": title,
        "descriptionHtml": description_html,
        "productType": product_type,
        "vendor": vendor,
        "tags": tags,
        "status": "ACTIVE"
      },
      "media": [
        {
          "mediaContentType": "IMAGE",
          "originalSource": image_url
        }
      ]
    }

    update_variant_query = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants {
          id
          price
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Sending productCreate mutation to Shopify for title: {title}")
            response = await client.post(
                url,
                json={"query": create_query, "variables": create_variables},
                headers=headers,
                timeout=15.0
            )

            if response.status_code != 200:
                logger.error(f"Shopify Admin API returned status {response.status_code}: {response.text}")
                response.raise_for_status()

            res_data = response.json()
            errors = res_data.get("errors")
            if errors:
                raise RuntimeError(f"GraphQL Endpoint Error: {errors}")

            result = res_data.get("data", {}).get("productCreate", {})
            user_errors = result.get("userErrors", [])
            if user_errors:
                raise RuntimeError(f"Shopify User Errors: {user_errors}")

            product = result.get("product")
            if not product:
                raise RuntimeError("Shopify creation response did not return a valid product object.")

            logger.info(f"Successfully created Shopify product. ID: {product['id']}")

            # Set price on the auto-created default variant
            variant_edges = product.get("variants", {}).get("edges", [])
            if variant_edges:
                default_variant_id = variant_edges[0]["node"]["id"]
                variant_response = await client.post(
                    url,
                    json={
                        "query": update_variant_query,
                        "variables": {
                            "productId": product["id"],
                            "variants": [
                                {
                                    "id": default_variant_id,
                                    "price": f"{price:.2f}",
                                    "inventoryPolicy": "DENY"
                                }
                            ]
                        }
                    },
                    headers=headers,
                    timeout=15.0
                )
                variant_response.raise_for_status()
                variant_res_data = variant_response.json()
                variant_errors = variant_res_data.get("errors")
                if variant_errors:
                    raise RuntimeError(f"GraphQL Endpoint Error (variant price update): {variant_errors}")
                variant_user_errors = variant_res_data.get("data", {}).get("productVariantsBulkUpdate", {}).get("userErrors", [])
                if variant_user_errors:
                    raise RuntimeError(f"Shopify User Errors (variant price update): {variant_user_errors}")

            # Resolve product URL (use onlineStoreUrl if populated, fallback to standard handles)
            shopify_url = product.get("onlineStoreUrl")
            if not shopify_url:
                shopify_url = f"https://{settings.SHOPIFY_STORE_URL}/products/{product['handle']}"

            return {
                "id": product["id"],
                "url": shopify_url,
                "title": product["title"]
            }

        except Exception as e:
            logger.error(f"Failed to create Shopify product: {str(e)}")
            raise e
