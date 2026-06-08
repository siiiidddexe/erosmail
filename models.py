from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text, Float
from sqlalchemy.orm import relationship
from database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    role = Column(String, default="admin")  # admin, superadmin
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # Permissions (for superadmin to restrict admin access)
    can_access_campaigns = Column(Boolean, default=True)
    can_access_smtp = Column(Boolean, default=True)
    can_access_api_accounts = Column(Boolean, default=True)
    can_access_logs = Column(Boolean, default=True)
    can_access_reports = Column(Boolean, default=True)
    
    smtp_accounts = relationship("SMTPAccount", back_populates="user")
    api_accounts = relationship("APIAccount", back_populates="user")
    campaigns = relationship("Campaign", back_populates="user")

class SMTPAccount(Base):
    __tablename__ = "smtp_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    host = Column(String)
    port = Column(Integer)
    username = Column(String)
    password = Column(String)
    is_active = Column(Boolean, default=True)
    
    # Rate limiting (emails per hour)
    rate_limit_per_hour = Column(Integer, default=100)
    rate_limit_per_day = Column(Integer, default=1000)
    
    user = relationship("User", back_populates="smtp_accounts")
    campaigns = relationship("CampaignSMTP", back_populates="smtp_account")

class APIAccount(Base):
    __tablename__ = "api_accounts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    provider = Column(String)  # nexomailer, custom
    api_key = Column(String)
    api_url = Column(String, default="https://nexomail.logiclaunch.in")
    is_active = Column(Boolean, default=True)
    
    # Rate limiting
    rate_limit_per_hour = Column(Integer, default=500)
    rate_limit_per_day = Column(Integer, default=5000)
    
    # For custom API
    custom_headers = Column(Text, nullable=True)  # JSON string
    custom_endpoint = Column(String, nullable=True)
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="api_accounts")
    campaigns = relationship("CampaignAPI", back_populates="api_account")

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, index=True)
    subject = Column(String)
    body = Column(Text)
    email_type = Column(String, default="text")  # text, html
    template_file = Column(String, nullable=True)
    status = Column(String, default="draft")  # draft, scheduled, sending, completed, failed, paused
    scheduled_at = Column(DateTime, nullable=True)
    total_recipients = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Repeat scheduling fields
    repeat_enabled = Column(Boolean, default=False)
    repeat_frequency = Column(String, nullable=True)  # daily, weekly, monthly, yearly
    next_repeat_at = Column(DateTime, nullable=True)
    
    # Pause hours (24-hour format, e.g., 22 for 10 PM, 6 for 6 AM)
    pause_start_hour = Column(Integer, nullable=True)
    pause_end_hour = Column(Integer, nullable=True)
    timezone_offset = Column(Integer, default=0)
    
    user = relationship("User", back_populates="campaigns")
    smtp_accounts = relationship("CampaignSMTP", back_populates="campaign")
    api_accounts = relationship("CampaignAPI", back_populates="campaign")
    logs = relationship("EmailLog", back_populates="campaign")

class CampaignSMTP(Base):
    __tablename__ = "campaign_smtp"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    smtp_account_id = Column(Integer, ForeignKey("smtp_accounts.id"))
    
    campaign = relationship("Campaign", back_populates="smtp_accounts")
    smtp_account = relationship("SMTPAccount", back_populates="campaigns")

class CampaignAPI(Base):
    __tablename__ = "campaign_api"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    api_account_id = Column(Integer, ForeignKey("api_accounts.id"))
    
    campaign = relationship("Campaign", back_populates="api_accounts")
    api_account = relationship("APIAccount", back_populates="campaigns")

class EmailLog(Base):
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    recipient_email = Column(String)
    recipient_name = Column(String, nullable=True)
    status = Column(String)  # sent, failed, queued, retrying
    send_method = Column(String)  # smtp, api
    account_used = Column(String, nullable=True)  # SMTP username or API name
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)
    retry_count = Column(Integer, default=0)
    
    campaign = relationship("Campaign", back_populates="logs")

class RateLimitLog(Base):
    __tablename__ = "rate_limit_logs"
    id = Column(Integer, primary_key=True, index=True)
    account_type = Column(String)  # smtp, api
    account_id = Column(Integer)
    emails_sent_hour = Column(Integer, default=0)
    emails_sent_day = Column(Integer, default=0)
    last_reset_hour = Column(DateTime, default=datetime.datetime.utcnow)
    last_reset_day = Column(DateTime, default=datetime.datetime.utcnow)
