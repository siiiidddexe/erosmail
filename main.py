import os
import json
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from database import get_db, engine, Base
from models import User, SMTPAccount, Campaign, CampaignSMTP, EmailLog, ContactBase, Contact, AppSetting
from auth import get_password_hash, authenticate_user, get_current_user
import datetime
import pandas as pd
import aiosmtplib
from email.message import EmailMessage
import asyncio
import httpx
from typing import Optional, List

# Create tables
Base.metadata.create_all(bind=engine)

# Migrations for legacy columns
from sqlalchemy import text
try:
    with engine.connect() as conn:
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
        if 'contact_base_id' not in columns:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN contact_base_id INTEGER"))

        result2 = conn.execute(text("PRAGMA table_info(email_logs)"))
        log_columns = [row[1] for row in result2]
        if 'recipient_name' not in log_columns:
            conn.execute(text("ALTER TABLE email_logs ADD COLUMN recipient_name TEXT"))

        result3 = conn.execute(text("PRAGMA table_info(smtp_accounts)"))
        smtp_columns = [row[1] for row in result3]
        if 'provider' not in smtp_columns:
            conn.execute(text("ALTER TABLE smtp_accounts ADD COLUMN provider TEXT DEFAULT 'smtp'"))

        conn.commit()
except Exception as e:
    print(f"Migration note: {e}")

# Create default admin user if none exists
from database import SessionLocal
db = SessionLocal()
if db.query(User).count() == 0:
    admin_user = User(username="admin", password_hash=get_password_hash("admin123"))
    db.add(admin_user)
    db.commit()
db.close()

app = FastAPI(title="ErosMail Campaign Manager")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "super-secret-key-change-in-production"),
    session_cookie="erosmail_session",
    max_age=86400 * 7,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_user_setting(db: Session, user_id: int, key: str, default: str = "") -> str:
    s = db.query(AppSetting).filter(AppSetting.user_id == user_id, AppSetting.key == key).first()
    return s.value if s and s.value else default


# --- Auth ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = authenticate_user(db, username, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


# --- Dashboard ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    status_filter: str = None,
    date_from: str = None,
    date_to: str = None,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    q = db.query(Campaign).filter(Campaign.user_id == user.id)
    if status_filter and status_filter != "all":
        q = q.filter(Campaign.status == status_filter)
    if date_from:
        q = q.filter(Campaign.created_at >= datetime.datetime.fromisoformat(date_from))
    if date_to:
        q = q.filter(Campaign.created_at < datetime.datetime.fromisoformat(date_to) + datetime.timedelta(days=1))
    campaigns = q.order_by(Campaign.created_at.desc()).all()

    total_sent = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "sent").count()
    total_failed = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id, EmailLog.status == "failed").count()
    total_campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).count()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "campaigns": campaigns,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_campaigns": total_campaigns,
        "status_filter": status_filter or "all",
        "date_from": date_from or "",
        "date_to": date_to or "",
    })


# --- Campaign CRUD ---
@app.get("/campaign/new", response_class=HTMLResponse)
async def new_campaign(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    smtp_accounts = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id, SMTPAccount.is_active == True).all()
    contact_bases = db.query(ContactBase).filter(ContactBase.user_id == user.id).all()
    has_openrouter = bool(get_user_setting(db, user.id, "openrouter_api_key") or get_user_setting(db, user.id, "gemini_api_key"))
    return templates.TemplateResponse("campaign_new.html", {
        "request": request, "user": user,
        "smtp_accounts": smtp_accounts,
        "contact_bases": contact_bases,
        "has_openrouter": has_openrouter,
    })

@app.post("/campaign/create")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    smtp_ids: list[int] = Form(...),
    template_file: UploadFile = File(None),
    contact_base_id: int = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)

    template_filename = None
    if template_file and template_file.filename:
        os.makedirs("uploads", exist_ok=True)
        template_filename = f"uploads/{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{template_file.filename}"
        with open(template_filename, "wb") as f:
            f.write(await template_file.read())

    campaign = Campaign(
        user_id=user.id,
        name=name,
        subject=subject,
        body=body,
        template_file=template_filename,
        contact_base_id=contact_base_id if contact_base_id else None,
        status="draft"
    )
    db.add(campaign)
    db.flush()

    for smtp_id in smtp_ids:
        db.add(CampaignSMTP(campaign_id=campaign.id, smtp_account_id=smtp_id))

    db.commit()
    return RedirectResponse(url=f"/campaign/{campaign.id}", status_code=status.HTTP_302_FOUND)

@app.get("/campaign/{campaign_id}/edit", response_class=HTMLResponse)
async def edit_campaign_page(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    smtp_accounts = db.query(SMTPAccount).filter(SMTPAccount.user_id == user.id, SMTPAccount.is_active == True).all()
    contact_bases = db.query(ContactBase).filter(ContactBase.user_id == user.id).all()
    selected_smtp_ids = [cs.smtp_account_id for cs in campaign.smtp_accounts]
    has_openrouter = bool(get_user_setting(db, user.id, "openrouter_api_key") or get_user_setting(db, user.id, "gemini_api_key"))
    return templates.TemplateResponse("campaign_edit.html", {
        "request": request, "user": user, "campaign": campaign,
        "smtp_accounts": smtp_accounts,
        "contact_bases": contact_bases,
        "selected_smtp_ids": selected_smtp_ids,
        "has_openrouter": has_openrouter,
    })

@app.post("/campaign/{campaign_id}/edit")
async def edit_campaign(
    request: Request,
    campaign_id: int,
    name: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    smtp_ids: list[int] = Form(...),
    template_file: UploadFile = File(None),
    contact_base_id: int = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.name = name
    campaign.subject = subject
    campaign.body = body
    campaign.contact_base_id = contact_base_id if contact_base_id else None

    if template_file and template_file.filename:
        os.makedirs("uploads", exist_ok=True)
        template_filename = f"uploads/{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{template_file.filename}"
        with open(template_filename, "wb") as f:
            f.write(await template_file.read())
        campaign.template_file = template_filename

    db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).delete()
    for smtp_id in smtp_ids:
        db.add(CampaignSMTP(campaign_id=campaign.id, smtp_account_id=smtp_id))

    db.commit()
    return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if campaign:
        db.delete(campaign)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/duplicate")
async def duplicate_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    src = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Campaign not found")

    new_camp = Campaign(
        user_id=user.id,
        name=f"Copy of {src.name}",
        subject=src.subject,
        body=src.body,
        template_file=src.template_file,
        contact_base_id=src.contact_base_id,
        status="draft"
    )
    db.add(new_camp)
    db.flush()

    for cs in src.smtp_accounts:
        db.add(CampaignSMTP(campaign_id=new_camp.id, smtp_account_id=cs.smtp_account_id))

    db.commit()
    return RedirectResponse(url=f"/campaign/{new_camp.id}/edit", status_code=status.HTTP_302_FOUND)

@app.post("/campaigns/bulk-delete")
async def bulk_delete_campaigns(request: Request, campaign_ids: list[int] = Form(default=[]), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    for cid in campaign_ids:
        c = db.query(Campaign).filter(Campaign.id == cid, Campaign.user_id == user.id).first()
        if c:
            db.delete(c)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


# --- Template Download ---
@app.get("/template/download")
async def download_template():
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Recipients"
    ws.append(["name", "email"])
    ws.append(["John Doe", "john.doe@example.com"])
    ws.append(["Jane Smith", "jane.smith@example.com"])
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=recipients_template.xlsx"}
    )


# --- Recipients parsing ---
def _parse_recipients(template_file):
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

def _get_recipients_from_contact_base(db: Session, contact_base_id: int, filter_mode: str = "all", campaign_id: int = None):
    """Get recipients from a contact base with optional filter mode."""
    contacts = db.query(Contact).filter(Contact.contact_base_id == contact_base_id).all()
    base = [{"name": c.name or c.email.split("@")[0], "email": c.email} for c in contacts]
    return _apply_resend_filter(db, base, filter_mode, campaign_id)

def _apply_resend_filter(db: Session, recipients: list, filter_mode: str, campaign_id: int = None) -> list:
    """Apply resend_mode filter to a recipient list."""
    if not campaign_id or filter_mode == "all":
        return recipients
    if filter_mode == "failed_only":
        failed_emails = {
            log.recipient_email
            for log in db.query(EmailLog).filter(
                EmailLog.campaign_id == campaign_id,
                EmailLog.status == "failed"
            ).all()
        }
        return [r for r in recipients if r["email"] in failed_emails]
    if filter_mode == "not_sent":
        sent_emails = {
            log.recipient_email
            for log in db.query(EmailLog).filter(
                EmailLog.campaign_id == campaign_id,
                EmailLog.status == "sent"
            ).all()
        }
        return [r for r in recipients if r["email"] not in sent_emails]
    return recipients


# --- Send Campaign ---
@app.get("/campaign/{campaign_id}/send", response_class=HTMLResponse)
async def confirm_send(request: Request, campaign_id: int, resend_mode: str = "all", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign_smtps = db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).all()
    smtp_accounts = [db.get(SMTPAccount, cs.smtp_account_id) for cs in campaign_smtps]
    smtp_accounts = [s for s in smtp_accounts if s is not None]

    if not smtp_accounts:
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "campaign": campaign,
            "logs": campaign.logs, "log_filter": "all",
            "error": "No SMTP accounts assigned. Go to SMTP Accounts and add one first."
        })

    if campaign.contact_base_id:
        recipients = _get_recipients_from_contact_base(db, campaign.contact_base_id, resend_mode, campaign_id)
    else:
        all_recipients, error = _parse_recipients(campaign.template_file)
        if error:
            return templates.TemplateResponse("campaign_detail.html", {
                "request": request, "user": user, "campaign": campaign,
                "logs": campaign.logs, "log_filter": "all",
                "error": error
            })
        recipients = _apply_resend_filter(db, all_recipients, resend_mode, campaign_id)

    if not recipients:
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "campaign": campaign,
            "logs": campaign.logs, "log_filter": "all",
            "error": "No recipients found for the selected filter."
        })

    return templates.TemplateResponse("campaign_confirm.html", {
        "request": request, "user": user,
        "campaign": campaign,
        "recipients": recipients,
        "smtp_accounts": smtp_accounts,
        "resend_mode": resend_mode,
    })

@app.post("/campaign/{campaign_id}/send")
async def send_campaign(
    request: Request,
    campaign_id: int,
    schedule_type: str = Form("now"),
    schedule_datetime: str = Form(None),
    resend_mode: str = Form("all"),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign_smtps = db.query(CampaignSMTP).filter(CampaignSMTP.campaign_id == campaign_id).all()
    smtp_accounts = [db.get(SMTPAccount, cs.smtp_account_id) for cs in campaign_smtps]
    smtp_accounts = [s for s in smtp_accounts if s is not None]

    if not smtp_accounts:
        return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

    if campaign.contact_base_id:
        recipients = _get_recipients_from_contact_base(db, campaign.contact_base_id, resend_mode, campaign_id)
    else:
        all_recipients, error = _parse_recipients(campaign.template_file)
        if error:
            return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)
        recipients = _apply_resend_filter(db, all_recipients, resend_mode, campaign_id)

    if not recipients:
        return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

    if schedule_type == "schedule" and schedule_datetime:
        try:
            scheduled_time = datetime.datetime.fromisoformat(schedule_datetime)
            campaign.scheduled_at = scheduled_time
            campaign.status = "scheduled"
            campaign.total_recipients = len(recipients)
            db.commit()
            return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)
        except ValueError:
            pass

    # Clear logs for the emails we're about to resend
    if resend_mode == "failed_only":
        db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id, EmailLog.status == "failed").delete()
    elif resend_mode == "all":
        db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id).delete()

    campaign.status = "sending"
    campaign.total_recipients = len(recipients)
    campaign.sent_count = 0
    campaign.failed_count = 0
    db.commit()

    smtp_index = 0
    for recipient in recipients:
        smtp = smtp_accounts[smtp_index % len(smtp_accounts)]
        smtp_index += 1

        personalized_body = campaign.body.replace("{name}", str(recipient.get('name', 'Valued Customer')))
        personalized_subject = campaign.subject.replace("{name}", str(recipient.get('name', 'Valued Customer')))

        msg = EmailMessage()
        msg['Subject'] = personalized_subject
        msg['From'] = smtp.username
        msg['To'] = str(recipient['email'])
        msg.set_content(personalized_body)
        if '<' in personalized_body and '>' in personalized_body:
            msg.add_alternative(personalized_body, subtype='html')

        try:
            if smtp.provider == "nexomail":
                api_key = smtp.password
                app_name = smtp.username or "ErosMail"
                headers = {"Content-Type": "application/json", "X-API-Key": api_key}
                payload = {"to": str(recipient['email']), "subject": personalized_subject, "html": personalized_body, "app_name": app_name}
                async with httpx.AsyncClient() as client:
                    response = await client.post("https://nexomail.logiclaunch.in/api/send", json=payload, headers=headers, timeout=10.0)
                    response.raise_for_status()
                    data = response.json()
                    if not data.get("success"):
                        raise Exception(data.get("error", "NexoMailer API failed"))
            else:
                kwargs = dict(hostname=smtp.host, port=smtp.port,
                              username=smtp.username, password=smtp.password,
                              timeout=30, validate_certs=False)
                if smtp.port == 465:
                    kwargs['use_tls'] = True
                elif smtp.port in (587, 2525):
                    kwargs['start_tls'] = True
                await aiosmtplib.send(msg, **kwargs)

            log = EmailLog(
                campaign_id=campaign.id,
                recipient_email=str(recipient['email']),
                recipient_name=str(recipient.get('name', '')),
                status="sent"
            )
            db.add(log)
            campaign.sent_count += 1
        except Exception as e:
            error_msg = str(e)
            if "timed out" in error_msg.lower():
                error_msg = "Connection timed out."
            log = EmailLog(
                campaign_id=campaign.id,
                recipient_email=str(recipient['email']),
                recipient_name=str(recipient.get('name', '')),
                status="failed",
                error_message=error_msg
            )
            db.add(log)
            campaign.failed_count += 1
        db.commit()

    campaign.status = "completed"
    db.commit()
    return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/resend")
async def resend_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return RedirectResponse(url=f"/campaign/{campaign_id}/send?resend_mode=all", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/resend-failed")
async def resend_failed(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return RedirectResponse(url=f"/campaign/{campaign_id}/send?resend_mode=failed_only", status_code=status.HTTP_302_FOUND)

@app.post("/campaign/{campaign_id}/logs/bulk-action")
async def logs_bulk_action(
    request: Request,
    campaign_id: int,
    action: str = Form(...),
    log_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if action == "delete_selected" and log_ids:
        db.query(EmailLog).filter(EmailLog.id.in_(log_ids), EmailLog.campaign_id == campaign_id).delete(synchronize_session=False)
        db.commit()
    elif action == "delete_failed":
        db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id, EmailLog.status == "failed").delete()
        db.commit()
    elif action == "delete_sent":
        db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id, EmailLog.status == "sent").delete()
        db.commit()
    elif action == "delete_all":
        db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id).delete()
        campaign.sent_count = 0
        campaign.failed_count = 0
        campaign.status = "draft"
        db.commit()

    return RedirectResponse(url=f"/campaign/{campaign_id}", status_code=status.HTTP_302_FOUND)

@app.get("/campaign/{campaign_id}/progress")
async def get_campaign_progress(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
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
async def campaign_detail(request: Request, campaign_id: int, log_filter: str = "all", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.user_id == user.id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    log_q = db.query(EmailLog).filter(EmailLog.campaign_id == campaign_id)
    if log_filter == "sent":
        log_q = log_q.filter(EmailLog.status == "sent")
    elif log_filter == "failed":
        log_q = log_q.filter(EmailLog.status == "failed")
    logs = log_q.order_by(EmailLog.sent_at.desc()).all()

    return templates.TemplateResponse("campaign_detail.html", {
        "request": request, "user": user, "campaign": campaign,
        "logs": logs, "log_filter": log_filter
    })


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
    provider: str = Form("smtp"),
    host: str = Form(None),
    port: int = Form(None),
    smtp_username: str = Form(None),
    smtp_password: str = Form(None),
    nexo_username: str = Form(None),
    nexo_password: str = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if provider == "nexomail":
        username, password = nexo_username, nexo_password
        host, port = None, None
    else:
        username, password = smtp_username, smtp_password
    account = SMTPAccount(user_id=user.id, name=name, provider=provider,
                          host=host, port=port, username=username, password=password, is_active=True)
    db.add(account)
    db.commit()
    return RedirectResponse(url="/smtp?added=1", status_code=status.HTTP_302_FOUND)

@app.post("/smtp/{account_id}/edit")
async def edit_smtp(
    request: Request,
    account_id: int,
    name: str = Form(...),
    host: str = Form(None),
    port: int = Form(None),
    smtp_username: str = Form(None),
    smtp_password: str = Form(None),
    nexo_username: str = Form(None),
    nexo_password: str = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if account:
        account.name = name
        if account.provider == "nexomail":
            if nexo_username: account.username = nexo_username
            if nexo_password: account.password = nexo_password
        else:
            account.host = host
            account.port = port
            if smtp_username: account.username = smtp_username
            if smtp_password: account.password = smtp_password
        db.commit()
    return RedirectResponse(url="/smtp?saved=1", status_code=status.HTTP_302_FOUND)

@app.post("/smtp/{account_id}/test")
async def test_smtp(
    request: Request,
    account_id: int,
    to_email: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    try:
        if account.provider == "nexomail":
            headers = {"Content-Type": "application/json", "X-API-Key": account.password or ""}
            payload = {"to": to_email, "subject": "ErosMail Test", "html": "<p>Test email from ErosMail.</p>", "app_name": account.username or "ErosMail"}
            async with httpx.AsyncClient() as client:
                r = await client.post("https://nexomail.logiclaunch.in/api/send", json=payload, headers=headers, timeout=15.0)
                data = r.json()
                if r.status_code != 200 or not data.get("success"):
                    raise Exception(data.get("error") or data.get("message") or f"HTTP {r.status_code}")
        else:
            msg = EmailMessage()
            msg["Subject"] = "ErosMail Test"
            msg["From"] = account.username
            msg["To"] = to_email
            msg.set_content("Test email from ErosMail. If you received this, your SMTP settings are correct.")
            kwargs = dict(hostname=account.host, port=account.port,
                          username=account.username, password=account.password,
                          timeout=20, validate_certs=False)
            if account.port == 465:
                kwargs["use_tls"] = True
            elif account.port in (587, 2525):
                kwargs["start_tls"] = True
            await aiosmtplib.send(msg, **kwargs)
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/smtp/{account_id}/delete")
async def delete_smtp(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if account:
        db.delete(account)
        db.commit()
    return RedirectResponse(url="/smtp", status_code=status.HTTP_302_FOUND)

@app.post("/smtp/{account_id}/toggle")
async def toggle_smtp(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    account = db.query(SMTPAccount).filter(SMTPAccount.id == account_id, SMTPAccount.user_id == user.id).first()
    if account:
        account.is_active = not account.is_active
        db.commit()
    return RedirectResponse(url="/smtp", status_code=status.HTTP_302_FOUND)


# --- Contact Bases ---
@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    bases = db.query(ContactBase).filter(ContactBase.user_id == user.id).order_by(ContactBase.created_at.desc()).all()
    return templates.TemplateResponse("contacts.html", {"request": request, "user": user, "bases": bases})

@app.post("/contacts/create")
async def create_contact_base(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    base = ContactBase(user_id=user.id, name=name, description=description)
    db.add(base)
    db.commit()
    return RedirectResponse(url=f"/contacts/{base.id}", status_code=status.HTTP_302_FOUND)

@app.get("/contacts/{base_id}", response_class=HTMLResponse)
async def contact_base_detail(request: Request, base_id: int, search: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    base = db.query(ContactBase).filter(ContactBase.id == base_id, ContactBase.user_id == user.id).first()
    if not base:
        raise HTTPException(status_code=404, detail="Contact base not found")
    q = db.query(Contact).filter(Contact.contact_base_id == base_id)
    if search:
        q = q.filter((Contact.email.contains(search)) | (Contact.name.contains(search)))
    contacts = q.order_by(Contact.created_at.desc()).all()
    return templates.TemplateResponse("contact_base_detail.html", {
        "request": request, "user": user, "base": base, "contacts": contacts, "search": search
    })

@app.post("/contacts/{base_id}/add")
async def add_contact(
    request: Request,
    base_id: int,
    name: str = Form(""),
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    base = db.query(ContactBase).filter(ContactBase.id == base_id, ContactBase.user_id == user.id).first()
    if not base:
        raise HTTPException(status_code=404, detail="Contact base not found")
    existing = db.query(Contact).filter(Contact.contact_base_id == base_id, Contact.email == email).first()
    if not existing:
        db.add(Contact(contact_base_id=base_id, name=name, email=email))
        db.commit()
    return RedirectResponse(url=f"/contacts/{base_id}", status_code=status.HTTP_302_FOUND)

@app.post("/contacts/{base_id}/import")
async def import_contacts(
    request: Request,
    base_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    base = db.query(ContactBase).filter(ContactBase.id == base_id, ContactBase.user_id == user.id).first()
    if not base:
        raise HTTPException(status_code=404, detail="Contact base not found")

    content = await file.read()
    import io
    df = pd.read_excel(io.BytesIO(content))
    df.columns = df.columns.str.lower().str.strip()
    if 'email' not in df.columns:
        return RedirectResponse(url=f"/contacts/{base_id}?error=no_email_column", status_code=status.HTTP_302_FOUND)
    if 'name' not in df.columns:
        df['name'] = df['email'].apply(lambda x: x.split('@')[0] if isinstance(x, str) else '')
    df = df.dropna(subset=['email'])
    added = 0
    for _, row in df.iterrows():
        email = str(row['email']).strip()
        name = str(row.get('name', '')).strip()
        existing = db.query(Contact).filter(Contact.contact_base_id == base_id, Contact.email == email).first()
        if not existing and email:
            db.add(Contact(contact_base_id=base_id, name=name, email=email))
            added += 1
    db.commit()
    return RedirectResponse(url=f"/contacts/{base_id}?imported={added}", status_code=status.HTTP_302_FOUND)

@app.post("/contacts/{base_id}/delete-contacts")
async def delete_contacts(
    request: Request,
    base_id: int,
    contact_ids: list[int] = Form(default=[]),
    action: str = Form("delete_selected"),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    base = db.query(ContactBase).filter(ContactBase.id == base_id, ContactBase.user_id == user.id).first()
    if not base:
        raise HTTPException(status_code=404, detail="Contact base not found")
    if action == "delete_all":
        db.query(Contact).filter(Contact.contact_base_id == base_id).delete()
    elif contact_ids:
        db.query(Contact).filter(Contact.id.in_(contact_ids), Contact.contact_base_id == base_id).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/contacts/{base_id}", status_code=status.HTTP_302_FOUND)

@app.post("/contacts/{base_id}/delete")
async def delete_contact_base(request: Request, base_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    base = db.query(ContactBase).filter(ContactBase.id == base_id, ContactBase.user_id == user.id).first()
    if base:
        db.delete(base)
        db.commit()
    return RedirectResponse(url="/contacts", status_code=status.HTTP_302_FOUND)


# --- Settings ---
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    openrouter_key = get_user_setting(db, user.id, "openrouter_api_key")
    gemini_key     = get_user_setting(db, user.id, "gemini_api_key")
    ai_model       = get_user_setting(db, user.id, "ai_model",    "google/gemini-2.0-flash-exp:free")
    gemini_model   = get_user_setting(db, user.id, "gemini_model", "gemini-2.0-flash")
    # Determine which provider will actually be used
    active_provider = "none"
    if openrouter_key:
        active_provider = "openrouter"
    elif gemini_key:
        active_provider = "gemini"
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "openrouter_key": openrouter_key,
        "gemini_key": gemini_key,
        "ai_model": ai_model,
        "gemini_model": gemini_model,
        "active_provider": active_provider,
    })

@app.post("/settings")
async def save_settings(
    request: Request,
    openrouter_api_key: str = Form(""),
    gemini_api_key: str = Form(""),
    ai_model: str = Form("google/gemini-2.0-flash-exp:free"),
    gemini_model: str = Form("gemini-2.0-flash"),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)

    def upsert_setting(key, value):
        s = db.query(AppSetting).filter(AppSetting.user_id == user.id, AppSetting.key == key).first()
        if s:
            s.value = value
        else:
            db.add(AppSetting(user_id=user.id, key=key, value=value))

    upsert_setting("openrouter_api_key", openrouter_api_key)
    upsert_setting("gemini_api_key",     gemini_api_key)
    upsert_setting("ai_model",           ai_model)
    upsert_setting("gemini_model",       gemini_model)
    db.commit()
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_302_FOUND)


# --- AI helpers ---

def _build_ai_messages(prompt: str, field: str) -> tuple[str, str]:
    user_msg = f"Write a professional marketing email. Context: {prompt}"
    if field == "subject":
        system_msg = (
            "You are an expert email copywriter. "
            "Return ONLY a valid JSON object with a single key 'subject' "
            "containing a compelling, concise email subject line. No markdown, no explanation."
        )
    elif field == "body":
        system_msg = (
            "You are an expert email copywriter. "
            "Return ONLY a valid JSON object with a single key 'body' "
            "containing a professional plain-text email body. "
            "Use {name} as the personalization placeholder. No markdown, no explanation."
        )
    else:
        system_msg = (
            "You are an expert email marketing copywriter. "
            "Return ONLY a valid JSON object with exactly two keys: "
            "'subject' (a compelling subject line) and "
            "'body' (a professional plain-text email body using {name} for personalization). "
            "No markdown, no explanation, no extra keys."
        )
    return system_msg, user_msg

# Ordered fallback lists — tried in sequence when the preferred model fails
_OPENROUTER_FALLBACKS = [
    "google/gemini-flash-1.5:free",
    "google/gemini-2.0-flash-lite:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-chat:free",
]
_GEMINI_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash-exp",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

def _extract_json(text: str) -> dict:
    """Parse JSON from model output, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

async def _call_openrouter(api_key: str, model: str, system_msg: str, user_msg: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://erosmail.app",
                "X-Title": "ErosMail",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _extract_json(content)

async def _call_gemini_direct(api_key: str, model: str, system_msg: str, user_msg: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            params={"key": api_key},
            json={
                "system_instruction": {"parts": [{"text": system_msg}]},
                "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                "generationConfig": {"response_mime_type": "application/json"},
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json(text)


# --- AI Generate ---
@app.post("/ai/generate")
async def ai_generate(
    request: Request,
    prompt: str = Form(...),
    field: str = Form("both"),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    or_key  = get_user_setting(db, user.id, "openrouter_api_key")
    gem_key = get_user_setting(db, user.id, "gemini_api_key")

    if not or_key and not gem_key:
        return JSONResponse(
            {"error": "No AI key configured. Go to Settings and add an OpenRouter or Gemini API key."},
            status_code=400,
        )

    system_msg, user_msg = _build_ai_messages(prompt, field)
    errors: list[str] = []

    # 1. OpenRouter — try preferred model then fallbacks
    if or_key:
        preferred = get_user_setting(db, user.id, "ai_model", _OPENROUTER_FALLBACKS[0])
        or_models = [preferred] + [m for m in _OPENROUTER_FALLBACKS if m != preferred]
        for model in or_models:
            try:
                result = await _call_openrouter(or_key, model, system_msg, user_msg)
                return JSONResponse(result)
            except Exception as e:
                errors.append(f"OpenRouter/{model}: {e}")

    # 2. Gemini direct — try preferred model then fallbacks
    if gem_key:
        preferred = get_user_setting(db, user.id, "gemini_model", "gemini-2.5-flash")
        gem_models = [preferred] + [m for m in _GEMINI_FALLBACKS if m != preferred]
        for model in gem_models:
            try:
                result = await _call_gemini_direct(gem_key, model, system_msg, user_msg)
                return JSONResponse(result)
            except Exception as e:
                errors.append(f"Gemini/{model}: {e}")

    return JSONResponse(
        {"error": "All AI providers failed — " + " | ".join(errors)},
        status_code=500,
    )


# --- Reports ---
@app.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    date_from: str = None,
    date_to: str = None,
    campaign_id: int = None,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.created_at.desc()).all()

    q = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id)
    if date_from:
        q = q.filter(EmailLog.sent_at >= datetime.datetime.fromisoformat(date_from))
    if date_to:
        q = q.filter(EmailLog.sent_at < datetime.datetime.fromisoformat(date_to) + datetime.timedelta(days=1))
    if campaign_id:
        q = q.filter(EmailLog.campaign_id == campaign_id)

    logs = q.all()
    total_sent = sum(1 for l in logs if l.status == "sent")
    total_failed = sum(1 for l in logs if l.status == "failed")
    delivery_rate = round(total_sent / max(len(logs), 1) * 100, 1)

    # Daily breakdown
    from collections import defaultdict
    daily = defaultdict(lambda: {"sent": 0, "failed": 0})
    for log in logs:
        day = log.sent_at.strftime("%Y-%m-%d")
        daily[day][log.status] += 1
    daily_sorted = sorted(daily.items())

    # Per-campaign breakdown
    camp_stats = defaultdict(lambda: {"name": "", "sent": 0, "failed": 0})
    for log in logs:
        camp_stats[log.campaign_id]["name"] = log.campaign.name
        camp_stats[log.campaign_id][log.status] += 1

    return templates.TemplateResponse("reports.html", {
        "request": request, "user": user,
        "campaigns": campaigns,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "selected_campaign_id": campaign_id,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_emails": len(logs),
        "delivery_rate": delivery_rate,
        "daily_stats": daily_sorted,
        "campaign_stats": list(camp_stats.values()),
    })


# --- Logs ---
@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    start_date: str = None,
    end_date: str = None,
    campaign_filter: int = None,
    status_filter: str = None,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.created_at.desc()).all()

    q = db.query(EmailLog).join(Campaign).filter(Campaign.user_id == user.id)
    if start_date:
        q = q.filter(EmailLog.sent_at >= datetime.datetime.fromisoformat(start_date))
    if end_date:
        q = q.filter(EmailLog.sent_at < datetime.datetime.fromisoformat(end_date) + datetime.timedelta(days=1))
    if campaign_filter:
        q = q.filter(EmailLog.campaign_id == campaign_filter)
    if status_filter and status_filter != "all":
        q = q.filter(EmailLog.status == status_filter)

    logs = q.order_by(EmailLog.sent_at.desc()).all()

    return templates.TemplateResponse("logs.html", {
        "request": request, "user": user, "logs": logs,
        "campaigns": campaigns,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "campaign_filter": campaign_filter,
        "status_filter": status_filter or "all",
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
