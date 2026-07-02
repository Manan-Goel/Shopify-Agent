import httpx
import logging
import time
import json
import re
import asyncio
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

class LLMResponse:
    def __init__(self, text: str, provider: str, model: str, latency_ms: float):
        self.text = text
        self.provider = provider
        self.model = model
        self.latency_ms = latency_ms

class LLMGateway:
    def __init__(self, groq_key: str, openrouter_key: str):
        self.groq_key = groq_key
        self.openrouter_key = openrouter_key
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

    async def call_llm(
        self,
        prompt: str,
        image_url: Optional[str] = None,
        force_json: bool = False,
        system_prompt: Optional[str] = None
    ) -> LLMResponse:
        """
        Executes a call to Groq as primary with exponential retries, failing over to OpenRouter.
        Supports text-only and multimodal vision inputs.
        """
        # Build message payloads
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
            
        user_content = [{"type": "text", "text": prompt}]
        if image_url:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })
            
        messages.append({"role": "user", "content": user_content})

        # Define payloads
        groq_model = "llama-3.2-11b-vision-preview" if image_url else "llama-3.3-70b-specdec"
        openrouter_model = "meta-llama/llama-3.2-90b-vision-instruct" if image_url else "qwen/qwen-2.5-instruct"

        # Try Groq first with exponential backoff retries
        if self.groq_key:
            for attempt in range(1, 4):
                start_time = time.time()
                try:
                    logger.info(f"Attempting API request to Groq using model: {groq_model} (attempt {attempt}/3)")
                    headers = {
                        "Authorization": f"Bearer {self.groq_key}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": groq_model,
                        "messages": messages,
                        "temperature": 0.2,
                    }
                    if force_json:
                        payload["response_format"] = {"type": "json_object"}
                        
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.post(self.groq_url, json=payload, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            text_response = data["choices"][0]["message"]["content"]
                            latency = (time.time() - start_time) * 1000
                            return LLMResponse(
                                text=text_response,
                                provider="Groq",
                                model=groq_model,
                                latency_ms=latency
                            )
                        elif response.status_code in [429, 500, 502, 503, 504]:
                            logger.warning(f"Groq API returned status {response.status_code}. Retrying...")
                        else:
                            logger.warning(f"Groq API returned non-retryable status {response.status_code}: {response.text}")
                            break
                except (httpx.TimeoutException, httpx.RequestError) as e:
                    logger.warning(f"Groq request failed or timed out: {str(e)}")
                
                # Backoff before next attempt
                if attempt < 3:
                    backoff_sec = 2 ** attempt
                    logger.info(f"Backing off for {backoff_sec}s before next Groq attempt...")
                    await asyncio.sleep(backoff_sec)

        # Fallback to OpenRouter
        if self.openrouter_key:
            start_time = time.time()
            try:
                logger.info(f"Attempting failover to OpenRouter using model: {openrouter_model}")
                headers = {
                    "Authorization": f"Bearer {self.openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://shopify-listing-agent.local",
                    "X-Title": "Shopify Listing Automation"
                }
                payload = {
                    "model": openrouter_model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                if force_json:
                    payload["response_format"] = {"type": "json_object"}

                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(self.openrouter_url, json=payload, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        text_response = data["choices"][0]["message"]["content"]
                        latency = (time.time() - start_time) * 1000
                        return LLMResponse(
                            text=text_response,
                            provider="OpenRouter",
                            model=openrouter_model,
                            latency_ms=latency
                        )
                    else:
                        logger.error(f"OpenRouter API returned status {response.status_code}: {response.text}")
            except Exception as e:
                logger.error(f"OpenRouter failover request failed: {str(e)}")

        raise RuntimeError("All LLM providers (Groq and OpenRouter) failed to return a response.")

    def parse_xml_tag(self, text: str, tag: str) -> str:
        """Helper to extract contents of XML tags like <title>...</title>"""
        # Tolerates spaces inside tag brackets
        pattern = f"<\\s*{tag}\\s*>(.*?)</\\s*{tag}\\s*>"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        
        # Tolerates missing closing tag or open-ended structure
        pattern_open = f"<\\s*{tag}\\s*>(.*)"
        match_open = re.search(pattern_open, text, re.DOTALL | re.IGNORECASE)
        if match_open:
            content = match_open.group(1).strip()
            next_tag_idx = content.find("<")
            if next_tag_idx != -1:
                content = content[:next_tag_idx].strip()
            return content

        # Fallback to general lines matching if XML tag parsing fails
        fallback_pattern = f"{tag}:\\s*(.*)"
        match_fallback = re.search(fallback_pattern, text, re.IGNORECASE)
        if match_fallback:
            return match_fallback.group(1).strip()
            
        return ""
