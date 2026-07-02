from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    ENV: str = Field(default="development", env="ENV")
    DATABASE_URL: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/shopify_listing", 
        env="DATABASE_URL"
    )
    REDIS_URL: str = Field(default="redis://localhost:6379/0", env="REDIS_URL")

    # Security & Access
    ADMIN_PHONE_WHITELIST_RAW: str = Field(default="+1234567890", env="ADMIN_PHONE_WHITELIST")
    WHATSAPP_VERIFY_TOKEN: str = Field(default="dev_verify_token", env="WHATSAPP_VERIFY_TOKEN")
    WHATSAPP_APP_SECRET: str = Field(default="dev_app_secret", env="WHATSAPP_APP_SECRET")

    # Meta Webhook & Send APIs
    WHATSAPP_ACCESS_TOKEN: str = Field(default="", env="WHATSAPP_ACCESS_TOKEN")
    WHATSAPP_PHONE_NUMBER_ID: str = Field(default="", env="WHATSAPP_PHONE_NUMBER_ID")

    # Shopify Configuration
    SHOPIFY_STORE_URL: str = Field(default="your-store-name.myshopify.com", env="SHOPIFY_STORE_URL")
    SHOPIFY_ACCESS_TOKEN: str = Field(default="", env="SHOPIFY_ACCESS_TOKEN")
    SHOPIFY_API_VERSION: str = Field(default="2024-07", env="SHOPIFY_API_VERSION")

    # AI Keys
    GROQ_API_KEY: str = Field(default="", env="GROQ_API_KEY")
    OPENROUTER_API_KEY: str = Field(default="", env="OPENROUTER_API_KEY")

    # Cloudflare R2 / AWS S3
    R2_ACCESS_KEY_ID: str = Field(default="", env="R2_ACCESS_KEY_ID")
    R2_SECRET_ACCESS_KEY: str = Field(default="", env="R2_SECRET_ACCESS_KEY")
    R2_BUCKET_NAME: str = Field(default="shopify-listing-media", env="R2_BUCKET_NAME")
    R2_ENDPOINT_URL: str = Field(default="", env="R2_ENDPOINT_URL")
    R2_PUBLIC_URL_PREFIX: str = Field(default="", env="R2_PUBLIC_URL_PREFIX") # e.g. https://pub-xxx.r2.dev

    @property
    def admin_phone_whitelist(self) -> List[str]:
        return [p.strip() for p in self.ADMIN_PHONE_WHITELIST_RAW.split(",") if p.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
