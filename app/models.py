from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, Text, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(30), unique=True, nullable=False, index=True)
    display_name = Column(String(100))
    is_admin = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(150), unique=True, nullable=False, index=True)
    sender_phone = Column(String(30), nullable=False)
    text_content = Column(Text, nullable=True)
    media_id = Column(String(150), nullable=True)
    local_media_path = Column(String(512), nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())

    listing = relationship("Listing", uselist=False, back_populates="message")

class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("whatsapp_messages.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(255), nullable=True)
    description_html = Column(Text, nullable=True)
    suggested_price = Column(Numeric(10, 2), nullable=True)
    final_price = Column(Numeric(10, 2), nullable=True)
    tags = Column(Text, nullable=True)
    shopify_product_id = Column(String(100), nullable=True)
    shopify_url = Column(String(512), nullable=True)
    status = Column(String(50), nullable=False, default="DRAFT")  # DRAFT, AWAITING_PRICE, PUBLISHED, FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    message = relationship("WhatsAppMessage", back_populates="listing")
    executions = relationship("AgentExecution", back_populates="listing", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="listing", cascade="all, delete-orphan")

class AgentExecution(Base):
    __tablename__ = "agent_executions"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    stage_name = Column(String(100), nullable=False) # VISION_EXTRACTION, COPY_GENERATION, SHOPIFY_PUBLISH
    provider_name = Column(String(100), nullable=True)
    duration_ms = Column(Numeric, nullable=True)
    payload_sent = Column(Text, nullable=True)
    payload_received = Column(Text, nullable=True)
    executed_at = Column(DateTime(timezone=True), server_default=func.now())

    listing = relationship("Listing", back_populates="executions")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id", ondelete="SET NULL"), nullable=True)
    action_type = Column(String(100), nullable=False)
    details = Column(Text, nullable=True)
    logged_at = Column(DateTime(timezone=True), server_default=func.now())

    listing = relationship("Listing", back_populates="audit_logs")
