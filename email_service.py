"""
Email Service Module
Handles SMTP and API-based email sending with rate limiting and queue management
"""
import aiosmtplib
from email.message import EmailMessage
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
import json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from models import EmailLog, RateLimitLog, SMTPAccount, APIAccount, Campaign
import asyncio

class EmailService:
    def __init__(self, db: Session):
        self.db = db
    
    async def fetch_nexomail_logs(self, api_account: APIAccount, campaign_id: int = None) -> list:
        """
        Fetch email logs from NexoMail API
        Returns list of log entries with delivery status
        """
        if api_account.provider != "nexomailer":
            return []
        
        try:
            async with httpx.AsyncClient() as client:
                # Fetch logs from NexoMail API
                response = await client.get(
                    f"{api_account.api_url}/api/logs",
                    headers={"X-API-Key": api_account.api_key},
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    logs_data = response.json()
                    return logs_data.get("logs", [])
                else:
                    print(f"Failed to fetch NexoMail logs: {response.status_code}")
                    return []
        except Exception as e:
            print(f"Error fetching NexoMail logs: {e}")
            return []
    
    async def sync_nexomail_status(self, api_account: APIAccount, campaign_id: int = None):
        """
        Sync email delivery status from NexoMail API to local database
        Updates EmailLog entries with latest status from NexoMail
        """
        logs = await self.fetch_nexomail_logs(api_account, campaign_id)
        
        updated_count = 0
        for log_entry in logs:
            # Find matching local log by recipient email and campaign
            recipient_email = log_entry.get("to")
            nexomail_status = log_entry.get("status")  # sent, delivered, failed, bounced
            
            # Map NexoMail status to our status
            status_map = {
                "sent": "sent",
                "delivered": "sent",
                "queued": "sent",
                "failed": "failed",
                "bounced": "failed"
            }
            
            mapped_status = status_map.get(nexomail_status, "sent")
            
            # Find and update local log
            query = self.db.query(EmailLog).filter(
                EmailLog.recipient_email == recipient_email,
                EmailLog.send_method == "api",
                EmailLog.account_used == api_account.name
            )
            
            if campaign_id:
                query = query.filter(EmailLog.campaign_id == campaign_id)
            
            local_log = query.order_by(EmailLog.sent_at.desc()).first()
            
            if local_log and local_log.status != mapped_status:
                local_log.status = mapped_status
                if nexomail_status in ["failed", "bounced"]:
                    local_log.error_message = log_entry.get("error", f"Status: {nexomail_status}")
                updated_count += 1
        
        if updated_count > 0:
            self.db.commit()
        
        return updated_count
    
    def check_rate_limit(self, account_type: str, account_id: int, limit_per_hour: int, limit_per_day: int) -> tuple[bool, str]:
        """
        Check if account has reached rate limits
        Returns: (can_send: bool, reason: str)
        """
        now = datetime.utcnow()
        
        # Get or create rate limit log
        rate_log = self.db.query(RateLimitLog).filter(
            RateLimitLog.account_type == account_type,
            RateLimitLog.account_id == account_id
        ).first()
        
        if not rate_log:
            rate_log = RateLimitLog(
                account_type=account_type,
                account_id=account_id,
                emails_sent_hour=0,
                emails_sent_day=0,
                last_reset_hour=now,
                last_reset_day=now
            )
            self.db.add(rate_log)
            self.db.commit()
            self.db.refresh(rate_log)
        
        # Reset hourly counter if needed
        if (now - rate_log.last_reset_hour).total_seconds() >= 3600:
            rate_log.emails_sent_hour = 0
            rate_log.last_reset_hour = now
        
        # Reset daily counter if needed
        if (now - rate_log.last_reset_day).total_seconds() >= 86400:
            rate_log.emails_sent_day = 0
            rate_log.last_reset_day = now
        
        # Check limits
        if rate_log.emails_sent_hour >= limit_per_hour:
            return False, f"Hourly limit reached ({rate_log.emails_sent_hour}/{limit_per_hour})"
        
        if rate_log.emails_sent_day >= limit_per_day:
            return False, f"Daily limit reached ({rate_log.emails_sent_day}/{limit_per_day})"
        
        return True, "OK"
    
    def increment_rate_limit(self, account_type: str, account_id: int):
        """Increment rate limit counters"""
        rate_log = self.db.query(RateLimitLog).filter(
            RateLimitLog.account_type == account_type,
            RateLimitLog.account_id == account_id
        ).first()
        
        if rate_log:
            rate_log.emails_sent_hour += 1
            rate_log.emails_sent_day += 1
            self.db.commit()
    
    async def send_via_smtp(self, smtp_account: SMTPAccount, to_email: str, to_name: str, 
                           subject: str, body: str, email_type: str, campaign_id: int) -> EmailLog:
        """
        Send email via SMTP
        """
        # Check rate limit
        can_send, reason = self.check_rate_limit(
            "smtp", 
            smtp_account.id, 
            smtp_account.rate_limit_per_hour, 
            smtp_account.rate_limit_per_day
        )
        
        if not can_send:
            log = EmailLog(
                campaign_id=campaign_id,
                recipient_email=to_email,
                recipient_name=to_name,
                status="failed",
                send_method="smtp",
                account_used=smtp_account.username,
                error_message=f"Rate limit: {reason}"
            )
            self.db.add(log)
            self.db.commit()
            return log
        
        # Create email message
        if email_type == "html":
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = smtp_account.username
            msg['To'] = to_email
            msg.attach(MIMEText(body, 'html'))
        else:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = smtp_account.username
            msg['To'] = to_email
            msg.set_content(body)
        
        try:
            # Send email
            await aiosmtplib.send(
                msg,
                hostname=smtp_account.host,
                port=smtp_account.port,
                username=smtp_account.username,
                password=smtp_account.password,
                start_tls=True,
                timeout=30
            )
            
            # Log success
            log = EmailLog(
                campaign_id=campaign_id,
                recipient_email=to_email,
                recipient_name=to_name,
                status="sent",
                send_method="smtp",
                account_used=smtp_account.username
            )
            self.db.add(log)
            
            # Increment rate limit
            self.increment_rate_limit("smtp", smtp_account.id)
            
            self.db.commit()
            return log
            
        except Exception as e:
            # Log failure
            error_msg = str(e)
            if "timed out" in error_msg.lower():
                error_msg = "Connection timed out. Check SMTP host, port, and credentials."
            
            log = EmailLog(
                campaign_id=campaign_id,
                recipient_email=to_email,
                recipient_name=to_name,
                status="failed",
                send_method="smtp",
                account_used=smtp_account.username,
                error_message=error_msg
            )
            self.db.add(log)
            self.db.commit()
            return log
    
    async def send_via_api(self, api_account: APIAccount, to_email: str, to_name: str,
                          subject: str, body: str, email_type: str, campaign_id: int) -> EmailLog:
        """
        Send email via API (NexoMailer or custom)
        """
        # Check rate limit
        can_send, reason = self.check_rate_limit(
            "api",
            api_account.id,
            api_account.rate_limit_per_hour,
            api_account.rate_limit_per_day
        )
        
        if not can_send:
            log = EmailLog(
                campaign_id=campaign_id,
                recipient_email=to_email,
                recipient_name=to_name,
                status="failed",
                send_method="api",
                account_used=api_account.name,
                error_message=f"Rate limit: {reason}"
            )
            self.db.add(log)
            self.db.commit()
            return log
        
        try:
            if api_account.provider == "nexomailer":
                # Use NexoMailer API
                headers = {
                    "Content-Type": "application/json",
                    "X-API-Key": api_account.api_key
                }
                
                payload = {
                    "to": to_email,
                    "subject": subject,
                    "app_name": api_account.name
                }
                
                if email_type == "html":
                    payload["html"] = body
                else:
                    payload["html"] = f"<p>{body}</p>"
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        f"{api_account.api_url}/api/send",
                        headers=headers,
                        json=payload
                    )
                    
                    result = response.json()
                    
                    if result.get("success"):
                        log = EmailLog(
                            campaign_id=campaign_id,
                            recipient_email=to_email,
                            recipient_name=to_name,
                            status="sent",
                            send_method="api",
                            account_used=api_account.name
                        )
                        self.db.add(log)
                        self.increment_rate_limit("api", api_account.id)
                        self.db.commit()
                        return log
                    else:
                        raise Exception(result.get("error", "API returned error"))
            
            elif api_account.provider == "custom":
                # Use custom API
                headers = {"Content-Type": "application/json"}
                if api_account.custom_headers:
                    headers.update(json.loads(api_account.custom_headers))
                
                payload = {
                    "to": to_email,
                    "subject": subject,
                    "body": body,
                    "email_type": email_type
                }
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        api_account.custom_endpoint,
                        headers=headers,
                        json=payload
                    )
                    
                    if response.status_code == 200:
                        log = EmailLog(
                            campaign_id=campaign_id,
                            recipient_email=to_email,
                            recipient_name=to_name,
                            status="sent",
                            send_method="api",
                            account_used=api_account.name
                        )
                        self.db.add(log)
                        self.increment_rate_limit("api", api_account.id)
                        self.db.commit()
                        return log
                    else:
                        raise Exception(f"API returned status {response.status_code}")
            
        except Exception as e:
            log = EmailLog(
                campaign_id=campaign_id,
                recipient_email=to_email,
                recipient_name=to_name,
                status="failed",
                send_method="api",
                account_used=api_account.name,
                error_message=str(e)
            )
            self.db.add(log)
            self.db.commit()
            return log
    
    def get_available_accounts(self, campaign: Campaign) -> tuple[list, str]:
        """
        Get available SMTP and API accounts for a campaign
        Returns accounts that haven't reached their rate limits
        """
        available = []
        
        # Get SMTP accounts
        for campaign_smtp in campaign.smtp_accounts:
            smtp = campaign_smtp.smtp_account
            if smtp and smtp.is_active:
                can_send, reason = self.check_rate_limit(
                    "smtp", smtp.id, 
                    smtp.rate_limit_per_hour, 
                    smtp.rate_limit_per_day
                )
                if can_send:
                    available.append(("smtp", smtp))
        
        # Get API accounts
        for campaign_api in campaign.api_accounts:
            api = campaign_api.api_account
            if api and api.is_active:
                can_send, reason = self.check_rate_limit(
                    "api", api.id,
                    api.rate_limit_per_hour,
                    api.rate_limit_per_day
                )
                if can_send:
                    available.append(("api", api))
        
        return available, "OK" if available else "No available accounts (all rate limited)"
