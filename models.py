from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)

    smtp_accounts = relationship("SMTPAccount", back_populates="user")
    campaigns = relationship("Campaign", back_populates="user")
    contact_bases = relationship("ContactBase", back_populates="user")
    settings = relationship("AppSetting", back_populates="user")

class SMTPAccount(Base):
    __tablename__ = "smtp_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    provider = Column(String, default="smtp")  # 'smtp' or 'nexomail'
    host = Column(String, nullable=True)
    port = Column(Integer, nullable=True)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="smtp_accounts")
    campaigns = relationship("CampaignSMTP", back_populates="smtp_account")

class ContactBase(Base):
    __tablename__ = "contact_bases"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="contact_bases")
    contacts = relationship("Contact", back_populates="contact_base", cascade="all, delete-orphan")

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, index=True)
    contact_base_id = Column(Integer, ForeignKey("contact_bases.id"))
    name = Column(String, nullable=True)
    email = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint('contact_base_id', 'email', name='uq_contact_base_email'),)

    contact_base = relationship("ContactBase", back_populates="contacts")

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    subject = Column(String)
    body = Column(Text)
    template_file = Column(String, nullable=True)
    contact_base_id = Column(Integer, ForeignKey("contact_bases.id"), nullable=True)
    status = Column(String, default="draft")  # draft, scheduled, sending, completed, failed
    scheduled_at = Column(DateTime, nullable=True)
    total_recipients = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="campaigns")
    smtp_accounts = relationship("CampaignSMTP", back_populates="campaign", cascade="all, delete-orphan")
    logs = relationship("EmailLog", back_populates="campaign", cascade="all, delete-orphan")
    contact_base = relationship("ContactBase")

class CampaignSMTP(Base):
    __tablename__ = "campaign_smtp"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    smtp_account_id = Column(Integer, ForeignKey("smtp_accounts.id"))

    campaign = relationship("Campaign", back_populates="smtp_accounts")
    smtp_account = relationship("SMTPAccount", back_populates="campaigns")

class EmailLog(Base):
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    recipient_email = Column(String)
    recipient_name = Column(String, nullable=True)
    status = Column(String)  # sent, failed
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)

    campaign = relationship("Campaign", back_populates="logs")

class AppSetting(Base):
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    key = Column(String, index=True)
    value = Column(Text, nullable=True)

    user = relationship("User", back_populates="settings")
