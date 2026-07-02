import httpx
import logging
from typing import Dict, Any, List, Optional
from app.config import settings

logger = logging.getLogger(__name__)

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
    headers = {
        "X-Shopify-Access-Token": settings.SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json"
    }

    # Shopify GraphQL productCreate Mutation
    query = """
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

    # Populate inputs
    variables = {
      "input": {
        "title": title,
        "descriptionHtml": description_html,
        "productType": product_type,
        "vendor": vendor,
        "tags": tags,
        "status": "ACTIVE",
        "variants": [
          {
            "price": f"{price:.2f}",
            "inventoryPolicy": "DENY"
          }
        ]
      },
      "media": [
        {
          "mediaContentType": "IMAGE",
          "originalSource": image_url
        }
      ]
    }

    # Execute request
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Sending productCreate mutation to Shopify for title: {title}")
            response = await client.post(
                url, 
                json={"query": query, "variables": variables}, 
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
