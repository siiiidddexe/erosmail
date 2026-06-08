"""
Background scheduler for autonomous email sending
Handles scheduled campaigns, repeat scheduling, and timing restrictions
"""
import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Campaign, EmailLog, CampaignSMTP, CampaignAPI, SMTPAccount, APIAccount
from email_service import EmailService
import pandas as pd

logger = logging.getLogger(__name__)

class CampaignScheduler:
    def __init__(self):
        self.running = False
        self.task = None
    
    async def start(self):
        """Start the scheduler"""
        if self.running:
            return
        
        self.running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info("Campaign scheduler started")
    
    async def stop(self):
        """Stop the scheduler"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Campaign scheduler stopped")
    
    async def _run_scheduler(self):
        """Main scheduler loop"""
        while self.running:
            try:
                await self._process_scheduled_campaigns()
                await self._process_repeat_campaigns()
                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)
    
    async def _process_scheduled_campaigns(self):
        """Process campaigns that are scheduled to send"""
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            
            # Find campaigns that are scheduled and ready to send
            campaigns = db.query(Campaign).filter(
                Campaign.status == "scheduled",
                Campaign.scheduled_at <= now
            ).all()
            
            for campaign in campaigns:
                # Check timing restrictions
                if self._is_within_pause_hours(campaign):
                    logger.info(f"Campaign {campaign.id} is in pause hours, skipping")
                    continue
                
                logger.info(f"Processing scheduled campaign {campaign.id}: {campaign.name}")
                await self._send_campaign(campaign, db)
                
        except Exception as e:
            logger.error(f"Error processing scheduled campaigns: {e}")
        finally:
            db.close()
    
    async def _process_repeat_campaigns(self):
        """Process repeat campaigns that need to be rescheduled"""
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            
            # Find completed campaigns with repeat settings
            campaigns = db.query(Campaign).filter(
                Campaign.status == "completed",
                Campaign.repeat_enabled == True,
                Campaign.next_repeat_at <= now
            ).all()
            
            for campaign in campaigns:
                logger.info(f"Processing repeat campaign {campaign.id}: {campaign.name}")
                
                # Reset campaign for resending
                campaign.status = "sending"
                campaign.sent_count = 0
                campaign.failed_count = 0
                campaign.started_at = now
                
                # Clear old logs
                db.query(EmailLog).filter(EmailLog.campaign_id == campaign.id).delete()
                db.commit()
                
                # Send the campaign
                await self._send_campaign(campaign, db)
                
                # Calculate next repeat time
                campaign.next_repeat_at = self._calculate_next_repeat(campaign)
                campaign.status = "scheduled"
                db.commit()
                
        except Exception as e:
            logger.error(f"Error processing repeat campaigns: {e}")
        finally:
            db.close()
    
    def _is_within_pause_hours(self, campaign: Campaign) -> bool:
        """Check if current time is within pause hours"""
        if campaign.pause_start_hour is None or campaign.pause_end_hour is None:
            return False
        
        # Adjust UTC time to the campaign's local timezone
        offset_minutes = campaign.timezone_offset or 0
        local_time = datetime.utcnow() - timedelta(minutes=offset_minutes)
        current_hour = local_time.hour
        
        # Handle overnight pause (e.g., 22:00 to 06:00)
        if campaign.pause_start_hour > campaign.pause_end_hour:
            return current_hour >= campaign.pause_start_hour or current_hour < campaign.pause_end_hour
        else:
            return campaign.pause_start_hour <= current_hour < campaign.pause_end_hour
    
    def _calculate_next_repeat(self, campaign: Campaign) -> datetime:
        """Calculate the next repeat time based on repeat settings"""
        now = datetime.utcnow()
        
        if campaign.repeat_frequency == "daily":
            return now + timedelta(days=1)
        elif campaign.repeat_frequency == "weekly":
            return now + timedelta(weeks=1)
        elif campaign.repeat_frequency == "monthly":
            return now + timedelta(days=30)
        elif campaign.repeat_frequency == "yearly":
            return now + timedelta(days=365)
        else:
            return None
    
    async def _send_campaign(self, campaign: Campaign, db: Session):
        """Send a campaign's emails"""
        try:
            # Get SMTP and API accounts
            smtp_accounts = []
            for cs in campaign.smtp_accounts:
                smtp = db.query(SMTPAccount).filter(SMTPAccount.id == cs.smtp_account_id).first()
                if smtp and smtp.is_active:
                    smtp_accounts.append(smtp)
            
            api_accounts = []
            for ca in campaign.api_accounts:
                api = db.query(APIAccount).filter(APIAccount.id == ca.api_account_id).first()
                if api and api.is_active:
                    api_accounts.append(api)
            
            if not smtp_accounts and not api_accounts:
                logger.error(f"No active accounts for campaign {campaign.id}")
                campaign.status = "failed"
                db.commit()
                return
            
            # Parse recipients
            if not campaign.template_file:
                logger.error(f"No template file for campaign {campaign.id}")
                campaign.status = "failed"
                db.commit()
                return
            
            try:
                df = pd.read_excel(campaign.template_file)
                df.columns = df.columns.str.lower().str.strip()
                
                if 'email' not in df.columns:
                    logger.error(f"Template missing 'email' column for campaign {campaign.id}")
                    campaign.status = "failed"
                    db.commit()
                    return
                
                if 'name' not in df.columns:
                    df['name'] = df['email'].apply(lambda x: x.split('@')[0] if isinstance(x, str) else 'Customer')
                
                df = df.dropna(subset=['email'])
                recipients = df[['name', 'email']].to_dict('records')
                
                if not recipients:
                    logger.error(f"No valid recipients for campaign {campaign.id}")
                    campaign.status = "failed"
                    db.commit()
                    return
                
            except Exception as e:
                logger.error(f"Error parsing template for campaign {campaign.id}: {e}")
                campaign.status = "failed"
                db.commit()
                return
            
            # Update campaign status
            campaign.status = "sending"
            campaign.total_recipients = len(recipients)
            campaign.sent_count = 0
            campaign.failed_count = 0
            campaign.started_at = datetime.utcnow()
            db.commit()
            
            # Initialize email service
            email_service = EmailService(db)
            
            # Combine all accounts for round-robin sending
            all_accounts = [("smtp", acc) for acc in smtp_accounts] + [("api", acc) for acc in api_accounts]
            
            # Send emails with round-robin distribution
            account_index = 0
            for recipient in recipients:
                # Check timing restrictions
                if self._is_within_pause_hours(campaign):
                    logger.info(f"Campaign {campaign.id} entered pause hours, stopping")
                    campaign.status = "paused"
                    db.commit()
                    return
                
                # Get next account (round-robin)
                account_type, account = all_accounts[account_index % len(all_accounts)]
                account_index += 1
                
                # Personalize body
                personalized_body = campaign.body.replace("{name}", str(recipient.get('name', 'Valued Customer')))
                
                # Send based on account type
                if account_type == "smtp":
                    log = await email_service.send_via_smtp(
                        account,
                        str(recipient['email']),
                        str(recipient.get('name', '')),
                        campaign.subject,
                        personalized_body,
                        campaign.email_type,
                        campaign.id
                    )
                else:  # api
                    log = await email_service.send_via_api(
                        account,
                        str(recipient['email']),
                        str(recipient.get('name', '')),
                        campaign.subject,
                        personalized_body,
                        campaign.email_type,
                        campaign.id
                    )
                
                # Update campaign stats
                if log.status == "sent":
                    campaign.sent_count += 1
                else:
                    campaign.failed_count += 1
                
                db.commit()
                
                # Small delay to avoid overwhelming servers
                await asyncio.sleep(0.1)
            
            campaign.status = "completed"
            campaign.completed_at = datetime.utcnow()
            db.commit()
            
            logger.info(f"Campaign {campaign.id} completed: {campaign.sent_count} sent, {campaign.failed_count} failed")
            
        except Exception as e:
            logger.error(f"Error sending campaign {campaign.id}: {e}")
            campaign.status = "failed"
            db.commit()

# Global scheduler instance
scheduler = CampaignScheduler()
