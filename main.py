import os
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db, engine, Base
from models import User, SMTPAccount, Campaign, CampaignSMTP, EmailLog, APIAccount, CampaignAPI, RateLimitLog
from auth import get_password_hash, authenticate_user, get_current_user, require_superadmin, check_permission
from email_service import EmailService
import datetime
import pandas as pd
import asyncio
import json

# Create tables
Base.metadata.create_all(bind=engine)

# Migration: Add new columns to tables if they don't exist
from sqlalchemy import text
try:
    with engine.connect() as conn:
        # Check campaigns table
        result = conn.execute(text("PRAGMA table_info(campaigns)"))
        columns = [row[1] for row in result]
        
        if 'scheduled_at' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN scheduled_at DATETIME"))
        if 'total_recipients' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN total_recipients INTEGER DEFAULT 0"))
        if 'sent_count' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN sent_count INTEGER DEFAULT 0"))
        if 'failed_count' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN failed_count INTEGER DEFAULT 0"))
        if 'email_type' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN email_type VARCHAR DEFAULT 'text'"))
        if 'started_at' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN started_at DATETIME"))
        if 'completed_at' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN completed_at DATETIME"))
        if 'repeat_enabled' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN repeat_enabled BOOLEAN DEFAULT 0"))
        if 'repeat_frequency' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN repeat_frequency VARCHAR"))
        if 'next_repeat_at' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN next_repeat_at DATETIME"))
        if 'pause_start_hour' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN pause_start_hour INTEGER"))
        if 'pause_end_hour' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN pause_end_hour INTEGER"))
        if 'timezone_offset' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN timezone_offset INTEGER DEFAULT 0"))
        
        # Check users table
        result = conn.execute(text("PRAGMA table_info(users)"))
        columns = [row[1] for row in result]
        
        if 'role' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'admin'"))
        if 'is_active' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"))
        if 'created_at' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
        if 'can_access_campaigns' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN can_access_campaigns BOOLEAN DEFAULT 1"))
        if 'can_access_smtp' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN can_access_smtp BOOLEAN DEFAULT 1"))
        if 'can_access_api_accounts' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN can_access_api_accounts BOOLEAN DEFAULT 1"))
        if 'can_access_logs' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN can_access_logs BOOLEAN DEFAULT 1"))
        if 'can_access_reports' not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN can_access_reports BOOLEAN DEFAULT 1"))
        
        # Check smtp_accounts table
        result = conn.execute(text("PRAGMA table_info(smtp_accounts)"))
        columns = [row[1] for row in result]
        
        if 'rate_limit_per_hour' not in columns:
            conn.execute(text("ALTER TABLE smtp_accounts ADD COLUMN rate_limit_per_hour INTEGER DEFAULT 100"))
        if 'rate_limit_per_day' not in columns:
            conn.execute(text("ALTER TABLE smtp_accounts ADD COLUMN rate_limit_per_day INTEGER DEFAULT 1000"))
        
        # Check email_logs table
        result = conn.execute(text("PRAGMA table_info(email_logs)"))
        columns = [row[1] for row in result]
        
        if 'recipient_name' not in columns:
            conn.execute(text("ALTER TABLE email_logs ADD COLUMN recipient_name VARCHAR"))
        if 'send_method' not in columns:
            conn.execute(text("ALTER TABLE email_logs ADD COLUMN send_method VARCHAR"))
        if 'account_used' not in columns:
            conn.execute(text("ALTER TABLE email_logs ADD COLUMN account_used VARCHAR"))
        if 'retry_count' not in columns:
            conn.execute(text("ALTER TABLE email_logs ADD COLUMN retry_count INTEGER DEFAULT 0"))
        
        conn.commit()
except Exception as e:
    print(f"Migration note: {e}")

# Create default superadmin user if none exists
from database import SessionLocal
from auth import get_password_hash

db = SessionLocal()
if db.query(User).count() == 0:
    superadmin_user = User(
        username="superadmin", 
        password_hash=get_password_hash("superadmin123"),
        role="superadmin",
        is_active=True,
        created_at=datetime.datetime.utcnow()
    )
    db.add(superadmin_user)
    db.commit()
db.close()

app = FastAPI(title="ErosMail Campaign Manager")

from urllib.parse import quote

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN]:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            error_msg = exc.detail if exc.detail else "Not authenticated"
            return RedirectResponse(url=f"/?error={quote(error_msg)}", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# Import scheduler
from scheduler import scheduler

# Startup and shutdown events
@app.on_event("startup")
async def startup_event():
    """Start the background scheduler on app startup"""
    await scheduler.start()

@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background scheduler on app shutdown"""
    await scheduler.stop()

# Session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "super-secret-key-change-in-production"),
    session_cookie="erosmail_session",
    max_age=86400 * 7,  # 7 days
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Helper function to check permissions
def has_permission(user: User, permission: str) -> bool:
    """Check if user has permission (superadmin always has all permissions)"""
    if user.role == "superadmin":
        return True
    return getattr(user, permission, False)

# --- Auth Routes ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, error: str = None):
    if request.session.get("user_id"):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": error})

@app.get("/favicon.ico")
async def favicon():
    """Return a simple favicon or 204 No Content"""
    from fastapi.responses import Response
    return Response(status_code=204)

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    if not user.is_active:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Account disabled"})
    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

# --- Dashboard & Campaigns ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    
    # Get campaigns based on permissions
    if has_permission(user, "can_access_campaigns"):
        campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.created_at.desc()).all()
    else:
        campaigns = []
    
    # Comprehensive stats
    total_sent = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "sent").count()
    total_failed = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "failed").count()
    total_campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).count()
    active_campaigns = db.query(Campaign).filter(Campaign.user_id == user.id, Campaign.status == "sending").count()
    
    # Today's stats
    today = datetime.datetime.utcnow().date()
    today_sent = db.query(EmailLog).join(Campaign).filter(
        Campaign.user_id == user.id,
        EmailLog.status == "sent",
        func.date(EmailLog.sent_at) == today
    ).count()
    
    # SMTP and API account counts
    smtp_count = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id, SMTPAccount.is_active == True).count()
    api_count = db.query(APIAccount).filter(APIAccount.user_id == user.id, APIAccount.is_active == True).count()
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request, 
        "user": user, 
        "campaigns": campaigns,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_campaigns": total_campaigns,
        "active_campaigns": active_campaigns,
        "today_sent": today_sent,
        "smtp_count": smtp_count,
        "api_count": api_count,
        "has_permission": has_permission
    })

@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_list(request: Request, db: Session = Depends(get_db)):
    """List all campaigns for the current user"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.created_at.desc()).all()
    
    return templates.TemplateResponse("campaigns_list.html", {
        "request": request,
        "user": user,
        "campaigns": campaigns
    })

@app.get("/campaign/new", response_class=HTMLResponse)
async def new_campaign(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    smtp_accounts = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id, SMTPAccount.is_active == True).all()
    api_accounts = db.query(APIAccount).filter(APIAccount.user_id == user.id, APIAccount.is_active == True).all()
    
    return templates.TemplateResponse("campaign_new.html", {
        "request": request, 
        "user": user, 
        "smtp_accounts": smtp_accounts,
        "api_accounts": api_accounts
    })

@app.get("/template/download")
async def download_template():
    """Generate and download a sample Excel template for recipients."""
    import io
    from openpyxl import Workbook
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Recipients"
    
    # Add headers
    ws.append(["name", "email"])
    
    # Add sample data
    ws.append(["John Doe", "john.doe@example.com"])
    ws.append(["Jane Smith", "jane.smith@example.com"])
    
    # Save to bytes
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    headers = {
        "Content-Disposition": "attachment; filename=recipients_template.xlsx"
    }
    
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers
    )

@app.post("/campaign/create")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    email_type: str = Form("text"),
    smtp_ids: list[int] = Form([]),
    api_ids: list[int] = Form([]),
    template_file: UploadFile = File(None),
    repeat_enabled: bool = Form(False),
    repeat_frequency: str = Form(""),
    pause_start_hour: str = Form(None),
    pause_end_hour: str = Form(None),
    timezone_offset: int = Form(0),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    template_filename = None
    if template_file and template_file.filename:
        os.makedirs("uploads", exist_ok=True)
        template_filename = f"uploads/{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{template_file.filename}"
        with open(template_filename, "wb") as f:
            f.write(await template_file.read())

    p_start = int(pause_start_hour) if (pause_start_hour and pause_start_hour.strip()) else None
    p_end = int(pause_end_hour) if (pause_end_hour and pause_end_hour.strip()) else None

    campaign = Campaign(
        user_id=user.id,
        name=name,
        subject=subject,
        body=body,
        email_type=email_type,
        template_file=template_filename,
        status="draft",
        repeat_enabled=repeat_enabled,
        repeat_frequency=repeat_frequency if repeat_frequency else None,
        pause_start_hour=p_start,
        pause_end_hour=p_end,
        timezone_offset=timezone_offset
    )
    db.add(campaign)
    db.flush()
    
    # Add SMTP accounts
    for smtp_id in smtp_ids:
        campaign_smtp = CampaignSMTP(campaign_id=campaign.id, smtp_account_id=smtp_id)
        db.add(campaign_smtp)
    
    # Add API accounts
    for api_id in api_ids:
        campaign_api = CampaignAPI(campaign_id=campaign.id, api_account_id=api_id)
        db.add(campaign_api)
        
    db.commit()
    return RedirectResponse(url="/campaigns", status_code=status.HTTP_302_FOUND)

def _parse_recipients(template_file):
    """Parse recipients from an Excel template file. Returns (recipients_list, error_string)."""
    if not template_file or not os.path.exists(template_file):
        return None, "No template file uploaded"
    try:
        df = pd.read_excel(template_file)
        df.columns = df.columns.str.lower().str.strip()
        if 'email' not in df.columns:
            return None, "Template must have an 'email' column"
        if 'name' not in df.columns:
            df['name'] = df['email'].apply(lambda x: x.split('@')[0] if isinstance(x, str) else 'Customer')
        df = df.dropna(subset=['email'])
        recipients = df[['name', 'email']].to_dict('records')
        if not recipients:
            return None, "No valid recipients found in the template"
        return recipients, None
    except Exception as e:
        return None, f"Error reading template: {str(e)}"

@app.get("/campaign/{campaign_id}/send", response_class=HTMLResponse)
async def confirm_send(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    """Show a confirmation page listing all recipients before sending."""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Get SMTP accounts for this campaign
    campaign_smtps = db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).all()
    smtp_accounts = [db.get(SMTPAccount, cs.smtp_account_id) for cs in campaign_smtps]
    smtp_accounts = [s for s in smtp_accounts if s is not None]
    
    # Get API accounts for this campaign
    campaign_apis = db.query(CampaignAPI).filter(CampaignAPI.campaign_id == campaign_id).all()
    api_accounts = [db.get(APIAccount, ca.api_account_id) for ca in campaign_apis]
    api_accounts = [a for a in api_accounts if a is not None]

    if not smtp_accounts and not api_accounts:
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "campaign": campaign, 
            "error": "No SMTP or API accounts assigned. Please add accounts first."
        })

    recipients, error = _parse_recipients(campaign.template_file)
    if error:
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "campaign": campaign, "error": error
        })

    return templates.TemplateResponse("campaign_confirm.html", {
        "request": request,
        "user": user,
        "campaign": campaign,
        "recipients": recipients,
        "smtp_accounts": smtp_accounts,
        "api_accounts": api_accounts,
    })

@app.post("/campaign/{campaign_id}/send")
async def send_campaign(
    request: Request, 
    campaign_id: int, 
    schedule_type: str = Form("now"),
    schedule_datetime: str = Form(None),
    db: Session = Depends(get_db)
):
    """Send or schedule the campaign."""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Get SMTP accounts for this campaign
    campaign_smtps = db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).all()
    smtp_accounts = [db.get(SMTPAccount, cs.smtp_account_id) for cs in campaign_smtps]
    smtp_accounts = [s for s in smtp_accounts if s is not None]
    
    # Get API accounts for this campaign
    campaign_apis = db.query(CampaignAPI).filter(CampaignAPI.campaign_id == campaign_id).all()
    api_accounts = [db.get(APIAccount, ca.api_account_id) for ca in campaign_apis]
    api_accounts = [a for a in api_accounts if a is not None]

    if not smtp_accounts and not api_accounts:
        return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

    recipients, error = _parse_recipients(campaign.template_file)
    if error:
        return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

    # Update campaign stats
    campaign.total_recipients = len(recipients)
    campaign.sent_count = 0
    campaign.failed_count = 0

    # Handle scheduling
    if schedule_type == "schedule" and schedule_datetime:
        try:
            scheduled_time = datetime.datetime.fromisoformat(schedule_datetime)
            campaign.scheduled_at = scheduled_time
            campaign.status = "scheduled"
            db.commit()
            return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)
        except ValueError:
            pass

    # Send immediately
    campaign.status = "sending"
    campaign.started_at = datetime.datetime.utcnow()
    db.commit()

    # Initialize email service
    email_service = EmailService(db)
    
    # Combine all accounts for round-robin sending
    all_accounts = [("smtp", acc) for acc in smtp_accounts] + [("api", acc) for acc in api_accounts]
    
    # Send emails with round-robin distribution
    account_index = 0
    for recipient in recipients:
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
    campaign.completed_at = datetime.datetime.utcnow()
    db.commit()

    return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/resend")
async def resend_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    """Resend a completed campaign."""
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    # Clear old logs
    db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id).delete()
    
    # Reset campaign status
    campaign.status = "draft"
    campaign.sent_count = 0
    campaign.failed_count = 0
    campaign.total_recipients = 0
    db.commit()
    
    return RedirectResponse(url=f"/campaign/{campaign_id}/send", status_code=status.HTTP_302_FOUND)

@app.get("/campaign/{campaign_id}/progress")
async def get_campaign_progress(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    """Get real-time progress of a campaign."""
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    return {
        "status": campaign.status,
        "total": campaign.total_recipients,
        "sent": campaign.sent_count,
        "failed": campaign.failed_count,
        "progress": round((campaign.sent_count + campaign.failed_count) / max(campaign.total_recipients, 1) * 100, 1)
    }

@app.get("/campaign/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
        
    logs = db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id).all()
    
    # Parse recipients for preview
    recipients = None
    if campaign.template_file:
        recipients, _ = _parse_recipients(campaign.template_file)
    
    return templates.TemplateResponse("campaign_detail.html", {
        "request": request, 
        "user": user, 
        "campaign": campaign, 
        "logs": logs,
        "recipients": recipients
    })

@app.get("/campaign/{campaign_id}/edit", response_class=HTMLResponse)
async def edit_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    """Edit an existing campaign"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    # Get all SMTP and API accounts
    smtp_accounts = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id, SMTPAccount.is_active == True).all()
    api_accounts = db.query(APIAccount).filter(APIAccount.user_id == user.id, APIAccount.is_active == True).all()
    
    # Get currently selected accounts
    selected_smtp_ids = [cs.smtp_account_id for cs in campaign.smtp_accounts]
    selected_api_ids = [ca.api_account_id for ca in campaign.api_accounts]
    
    return templates.TemplateResponse("campaign_edit.html", {
        "request": request,
        "user": user,
        "campaign": campaign,
        "smtp_accounts": smtp_accounts,
        "api_accounts": api_accounts,
        "selected_smtp_ids": selected_smtp_ids,
        "selected_api_ids": selected_api_ids
    })

@app.post("/campaign/{campaign_id}/update")
async def update_campaign(
    request: Request,
    campaign_id: int,
    name: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    email_type: str = Form("text"),
    smtp_ids: list[int] = Form([]),
    api_ids: list[int] = Form([]),
    template_file: UploadFile = File(None),
    repeat_enabled: bool = Form(False),
    repeat_frequency: str = Form(""),
    pause_start_hour: str = Form(None),
    pause_end_hour: str = Form(None),
    timezone_offset: int = Form(0),
    db: Session = Depends(get_db)
):
    """Update an existing campaign"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    # Update basic fields
    campaign.name = name
    campaign.subject = subject
    campaign.body = body
    campaign.email_type = email_type
    
    # Update scheduling fields
    campaign.repeat_enabled = repeat_enabled
    campaign.repeat_frequency = repeat_frequency if repeat_frequency else None
    campaign.pause_start_hour = int(pause_start_hour) if (pause_start_hour and pause_start_hour.strip()) else None
    campaign.pause_end_hour = int(pause_end_hour) if (pause_end_hour and pause_end_hour.strip()) else None
    campaign.timezone_offset = timezone_offset
    
    # Handle template file upload if provided
    if template_file and template_file.filename:
        import os
        from datetime import datetime
        upload_dir = "uploads"
        os.makedirs(upload_dir, exist_ok=True)
        filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{template_file.filename}"
        file_path = os.path.join(upload_dir, filename)
        
        with open(file_path, "wb") as f:
            content = await template_file.read()
            f.write(content)
        
        campaign.template_file = file_path
    
    # Clear existing SMTP associations
    db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).delete()
    
    # Add new SMTP associations
    for smtp_id in smtp_ids:
        campaign_smtp = CampaignSMTP(campaign_id=campaign_id, smtp_account_id=smtp_id)
        db.add(campaign_smtp)
    
    # Clear existing API associations
    db.query(CampaignAPI).filter(CampaignAPI.campaign_id == campaign_id).delete()
    
    # Add new API associations
    for api_id in api_ids:
        campaign_api = CampaignAPI(campaign_id=campaign_id, api_account_id=api_id)
        db.add(campaign_api)
    
    db.commit()
    
    return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    """Delete a campaign"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_campaigns"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    
    # Delete associated logs
    db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id).delete()
    
    # Delete SMTP associations
    db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).delete()
    
    # Delete API associations
    db.query(CampaignAPI).filter(CampaignAPI.campaign_id == campaign_id).delete()
    
    # Delete the campaign
    db.delete(campaign)
    db.commit()
    
    return RedirectResponse(url="/campaigns", status_code=status.HTTP_302_FOUND)

# --- SMTP Management ---
@app.get("/smtp", response_class=HTMLResponse)
async def smtp_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    accounts = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id).all()
    return templates.TemplateResponse("smtp_list.html", {"request": request, "user": user, "accounts": accounts})

@app.post("/smtp/add")
async def add_smtp(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    rate_limit_per_hour: int = Form(100),
    rate_limit_per_day: int = Form(1000),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    account = SMTPAccount(
        user_id=user.id,
        name=name,
        host=host,
        port=port,
        username=username,
        password=password,
        rate_limit_per_hour=rate_limit_per_hour,
        rate_limit_per_day=rate_limit_per_day,
        is_active=True
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url="/smtp", status_code=status.HTTP_302_FOUND)

@app.post("/smtp/{account_id}/delete")
async def delete_smtp(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if account:
        db.delete(account)
        db.commit()
    return RedirectResponse(url="/smtp", status_code=status.HTTP_302_FOUND)

@app.get("/smtp/{account_id}/edit", response_class=HTMLResponse)
async def edit_smtp(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="SMTP account not found")
    return templates.TemplateResponse("smtp_edit.html", {"request": request, "user": user, "account": account})

@app.post("/smtp/{account_id}/update")
async def update_smtp(
    request: Request,
    account_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    rate_limit_per_hour: int = Form(100),
    rate_limit_per_day: int = Form(1000),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="SMTP account not found")
    
    account.name = name
    account.host = host
    account.port = port
    account.username = username
    account.password = password
    account.rate_limit_per_hour = rate_limit_per_hour
    account.rate_limit_per_day = rate_limit_per_day
    
    db.commit()
    return RedirectResponse(url="/smtp", status_code=status.HTTP_302_FOUND)

# --- API Account Management ---
@app.get("/api-accounts", response_class=HTMLResponse)
async def api_accounts_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    accounts = db.query(APIAccount).filter(APIAccount.user_id == user.id).all()
    return templates.TemplateResponse("api_accounts_list.html", {"request": request, "user": user, "accounts": accounts})

@app.post("/api-accounts/add")
async def add_api_account(
    request: Request,
    name: str = Form(...),
    provider: str = Form(...),
    api_key: str = Form(...),
    api_url: str = Form("https://nexomail.logiclaunch.in"),
    rate_limit_per_hour: int = Form(500),
    rate_limit_per_day: int = Form(5000),
    custom_endpoint: str = Form(None),
    custom_headers: str = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = APIAccount(
        user_id=user.id,
        name=name,
        provider=provider,
        api_key=api_key,
        api_url=api_url,
        rate_limit_per_hour=rate_limit_per_hour,
        rate_limit_per_day=rate_limit_per_day,
        custom_endpoint=custom_endpoint if provider == "custom" else None,
        custom_headers=custom_headers if provider == "custom" else None,
        is_active=True
    )
    db.add(account)
    db.commit()
    return RedirectResponse(url="/api-accounts", status_code=status.HTTP_302_FOUND)

@app.post("/api-accounts/{account_id}/delete")
async def delete_api_account(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = db.query(APIAccount).filter(APIAccount.id == account_id, APIAccount.user_id == user.id).first()
    if account:
        db.delete(account)
        db.commit()
    return RedirectResponse(url="/api-accounts", status_code=status.HTTP_302_FOUND)

@app.get("/api-accounts/{account_id}/edit", response_class=HTMLResponse)
async def edit_api_account(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = db.query(APIAccount).filter(APIAccount.id == account_id, APIAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="API account not found")
    
    return templates.TemplateResponse("api_accounts_edit.html", {"request": request, "user": user, "account": account})

@app.post("/api-accounts/{account_id}/update")
async def update_api_account(
    request: Request,
    account_id: int,
    name: str = Form(...),
    provider: str = Form(...),
    api_key: str = Form(...),
    api_url: str = Form("https://nexomail.logiclaunch.in"),
    rate_limit_per_hour: int = Form(500),
    rate_limit_per_day: int = Form(5000),
    custom_endpoint: str = Form(None),
    custom_headers: str = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = db.query(APIAccount).filter(APIAccount.id == account_id, APIAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="API account not found")
    
    account.name = name
    account.provider = provider
    account.api_key = api_key
    account.api_url = api_url
    account.rate_limit_per_hour = rate_limit_per_hour
    account.rate_limit_per_day = rate_limit_per_day
    account.custom_endpoint = custom_endpoint if provider == "custom" else None
    account.custom_headers = custom_headers if provider == "custom" else None
    
    db.commit()
    return RedirectResponse(url="/api-accounts", status_code=status.HTTP_302_FOUND)

@app.get("/api-accounts/{account_id}/logs")
async def fetch_api_logs(request: Request, account_id: int, db: Session = Depends(get_db)):
    """Fetch logs from NexoMail API for a specific account"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = db.query(APIAccount).filter(APIAccount.id == account_id, APIAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="API account not found")
    
    email_service = EmailService(db)
    logs = await email_service.fetch_nexomail_logs(account)
    
    return JSONResponse(content={"logs": logs, "count": len(logs)})

@app.post("/api-accounts/{account_id}/sync-logs")
async def sync_api_logs(request: Request, account_id: int, campaign_id: int = None, db: Session = Depends(get_db)):
    """Sync NexoMail delivery status to local database"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    account = db.query(APIAccount).filter(APIAccount.id == account_id, APIAccount.user_id == user.id).first()
    if not account:
        raise HTTPException(status_code=404, detail="API account not found")
    
    email_service = EmailService(db)
    updated_count = await email_service.sync_nexomail_status(account, campaign_id)
    
    return JSONResponse(content={"updated": updated_count, "message": f"Updated {updated_count} log entries"})

@app.post("/sync-all-nexomail-logs")
async def sync_all_nexomail_logs(request: Request, db: Session = Depends(get_db)):
    """Sync logs from all NexoMail API accounts"""
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_api_accounts"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all NexoMail API accounts
    accounts = db.query(APIAccount).filter(
        APIAccount.user_id == user.id,
        APIAccount.provider == "nexomailer",
        APIAccount.is_active == True
    ).all()
    
    email_service = EmailService(db)
    total_updated = 0
    
    for account in accounts:
        updated = await email_service.sync_nexomail_status(account)
        total_updated += updated
    
    return JSONResponse(content={
        "accounts_synced": len(accounts),
        "total_updated": total_updated,
        "message": f"Synced {len(accounts)} accounts, updated {total_updated} log entries"
    })

# --- Reports & Analytics ---
@app.get("/reports", response_class=HTMLResponse)
async def reports(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_reports"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Overall statistics
    total_sent = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "sent").count()
    total_failed = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "failed").count()
    total_campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).count()
    
    # Success rate
    success_rate = (total_sent / (total_sent + total_failed) * 100) if (total_sent + total_failed) > 0 else 0
    
    # Campaign performance
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.created_at.desc()).limit(10).all()
    
    # Daily stats for last 7 days
    daily_stats = []
    for i in range(7):
        date = datetime.datetime.utcnow().date() - datetime.timedelta(days=i)
        next_date = date + datetime.timedelta(days=1)
        
        sent = db.query(EmailLog).join(Campaign).filter(
            Campaign.user_id == user.id,
            EmailLog.status == "sent",
            EmailLog.sent_at >= datetime.datetime.combine(date, datetime.time.min),
            EmailLog.sent_at < datetime.datetime.combine(next_date, datetime.time.min)
        ).count()
        
        failed = db.query(EmailLog).join(Campaign).filter(
            Campaign.user_id == user.id,
            EmailLog.status == "failed",
            EmailLog.sent_at >= datetime.datetime.combine(date, datetime.time.min),
            EmailLog.sent_at < datetime.datetime.combine(next_date, datetime.time.min)
        ).count()
        
        daily_stats.append({
            "date": date.strftime("%Y-%m-%d"),
            "sent": sent,
            "failed": failed
        })
    
    # Method breakdown (SMTP vs API)
    smtp_sent = db.query(EmailLog).join(Campaign).filter(
        Campaign.user_id == user.id,
        EmailLog.status == "sent",
        EmailLog.send_method == "smtp"
    ).count()
    
    api_sent = db.query(EmailLog).join(Campaign).filter(
        Campaign.user_id == user.id,
        EmailLog.status == "sent",
        EmailLog.send_method == "api"
    ).count()
    
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "user": user,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_campaigns": total_campaigns,
        "success_rate": round(success_rate, 2),
        "campaigns": campaigns,
        "daily_stats": daily_stats,
        "smtp_sent": smtp_sent,
        "api_sent": api_sent
    })

# --- Superadmin Routes ---
@app.get("/superadmin", response_class=HTMLResponse)
async def superadmin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    # Get all users
    users = db.query(User).order_by(User.created_at.desc()).all()
    
    # System-wide statistics
    total_users = len(users)
    total_campaigns = db.query(Campaign).count()
    total_sent = db.query(EmailLog).filter(EmailLog.status == "sent").count()
    total_failed = db.query(EmailLog).filter(EmailLog.status == "failed").count()
    
    # Active accounts
    active_smtp = db.query(SMTPAccount).filter(SMTPAccount.is_active == True).count()
    active_api = db.query(APIAccount).filter(APIAccount.is_active == True).count()
    
    return templates.TemplateResponse("superadmin_dashboard.html", {
        "request": request,
        "user": user,
        "users": users,
        "total_users": total_users,
        "total_campaigns": total_campaigns,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "active_smtp": active_smtp,
        "active_api": active_api
    })

@app.post("/superadmin/users/add")
async def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("admin"),
    can_access_campaigns: bool = Form(True),
    can_access_smtp: bool = Form(True),
    can_access_api_accounts: bool = Form(True),
    can_access_logs: bool = Form(True),
    can_access_reports: bool = Form(True),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    # Check if username exists
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return RedirectResponse(url="/superadmin?error=username_exists", status_code=status.HTTP_302_FOUND)
    
    new_user = User(
        username=username,
        password_hash=get_password_hash(password),
        role=role,
        is_active=True,
        created_at=datetime.datetime.utcnow(),
        can_access_campaigns=can_access_campaigns,
        can_access_smtp=can_access_smtp,
        can_access_api_accounts=can_access_api_accounts,
        can_access_logs=can_access_logs,
        can_access_reports=can_access_reports
    )
    db.add(new_user)
    db.commit()
    return RedirectResponse(url="/superadmin", status_code=status.HTTP_302_FOUND)

@app.post("/superadmin/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user and target_user.id != user.id:  # Can't disable yourself
        target_user.is_active = not target_user.is_active
        db.commit()
    
    return RedirectResponse(url="/superadmin", status_code=status.HTTP_302_FOUND)

@app.post("/superadmin/users/{user_id}/update-permissions")
async def update_user_permissions(
    request: Request,
    user_id: int,
    can_access_campaigns: bool = Form(False),
    can_access_smtp: bool = Form(False),
    can_access_api_accounts: bool = Form(False),
    can_access_logs: bool = Form(False),
    can_access_reports: bool = Form(False),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user:
        target_user.can_access_campaigns = can_access_campaigns
        target_user.can_access_smtp = can_access_smtp
        target_user.can_access_api_accounts = can_access_api_accounts
        target_user.can_access_logs = can_access_logs
        target_user.can_access_reports = can_access_reports
        db.commit()
    
    return RedirectResponse(url="/superadmin", status_code=status.HTTP_302_FOUND)

@app.post("/superadmin/users/{user_id}/change-password")
async def change_password(request: Request, user_id: int, new_password: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user:
        target_user.password_hash = get_password_hash(new_password)
        db.commit()
    
    return RedirectResponse(url="/superadmin?success=password_changed", status_code=status.HTTP_302_FOUND)

@app.post("/superadmin/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    
    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user and target_user.id != user.id:  # Can't delete yourself
        # Cascade delete everything associated with this user
        db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user_id).delete(synchronize_session=False)
        db.query(CampaignSMTP).join(Campaign).filter(Campaign.user_id == user_id).delete(synchronize_session=False)
        db.query(CampaignAPI).join(Campaign).filter(Campaign.user_id == user_id).delete(synchronize_session=False)
        db.query(Campaign).filter(Campaign.user_id == user_id).delete(synchronize_session=False)
        db.query(SMTPAccount).filter(SMTPAccount.user_id == user_id).delete(synchronize_session=False)
        db.query(APIAccount).filter(APIAccount.user_id == user_id).delete(synchronize_session=False)
        
        db.delete(target_user)
        db.commit()
    
    return RedirectResponse(url="/superadmin?success=user_deleted", status_code=status.HTTP_302_FOUND)

# --- Logs ---
@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not has_permission(user, "can_access_logs"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id)
    
    if start_date:
        query = query.filter(EmailLog.sent_at >= datetime.datetime.fromisoformat(start_date))
    if end_date:
        # Add one day to include the end date fully
        end_dt = datetime.datetime.fromisoformat(end_date) + datetime.timedelta(days=1)
        query = query.filter(EmailLog.sent_at < end_dt)
        
    logs = query.order_by(EmailLog.sent_at.desc()).all()
    return templates.TemplateResponse("logs.html", {"request": request, "user": user, "logs": logs, "start_date": start_date, "end_date": end_date})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
