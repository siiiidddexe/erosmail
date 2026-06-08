# NexoMailer — API Documentation

> **Production instance:** `https://nexomail.logiclaunch.in`
> **By:** Logic Launch Software Solutions
> **Version:** 1.0 (Flask + Gmail OAuth2)

---

## What is NexoMailer?

NexoMailer is a **multi-tenant email-as-a-service API** that sends transactional emails through Gmail accounts using Google OAuth2. It's designed so any app (web, mobile, backend) can send OTP codes, password reset links, and custom HTML emails by making a single HTTP POST request.

**Key architecture:**
- Each **API key** represents a client/project (e.g. "Servleash", "MyStore")
- Each API key has its own **Gmail account pool** (1 or more linked Gmail accounts)
- Emails are sent via the Gmail API (not SMTP) — high deliverability, no spam folder
- Built-in **email queue** with automatic retries (3 attempts) and rate limiting
- Includes **branded HTML email templates** (OTP, password reset, generic)

---

## Quick Start (2 minutes)

### 1. Get your API key

Ask the NexoMailer admin for an API key, or self-host and create one from the dashboard. Keys look like:

```
nxm_eff8cefbc068d6ea671167d8de1cd355fc419554bfa1926f
```

### 2. Send your first email

```bash
curl -X POST https://nexomail.logiclaunch.in/api/send-otp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "to": "user@example.com",
    "code": "482910",
    "app_name": "MyApp"
  }'
```

**Response:**
```json
{
  "success": true,
  "sent_by": "noreply@yourdomain.com"
}
```

That's it. The user receives a beautifully styled OTP email.

---

## Authentication

All send endpoints require an API key. Pass it via **header** (recommended) or query parameter.

| Method | Example |
|--------|---------|
| **Header** (recommended) | `X-API-Key: nxm_abc123...` |
| **Query param** | `?api_key=nxm_abc123...` |

**Error if missing/invalid:**
```json
{ "error": "Invalid or missing API key" }
```
HTTP status: `401`

---

## API Endpoints

### Base URL

```
https://nexomail.logiclaunch.in
```

---

### `POST /api/send-otp`

Send a **verification code** email using the built-in OTP template.

**Headers:**
```
Content-Type: application/json
X-API-Key: YOUR_API_KEY
```

**Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | string | ✅ | Recipient email address |
| `code` | string | ✅ | The OTP/verification code to display |
| `app_name` | string | ❌ | Your app's name shown in the email header. Defaults to the API key's company name. |
| `subject` | string | ❌ | Custom subject line. Defaults to `"{app_name} — Verification Code: {code}"` |

**Example request:**
```json
{
  "to": "john@example.com",
  "code": "847293",
  "app_name": "Servleash"
}
```

**Success response** (`200`):
```json
{
  "success": true,
  "sent_by": "noreply@servleash.com"
}
```

**Queued response** (`200`) — if all accounts are rate-limited:
```json
{
  "success": true,
  "queued": true,
  "reason": "No available accounts"
}
```

---

### `POST /api/send-reset`

Send a **password reset link** email using the built-in reset template.

**Headers:**
```
Content-Type: application/json
X-API-Key: YOUR_API_KEY
```

**Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | string | ✅ | Recipient email address |
| `reset_link` | string | ✅ | The full URL for the password reset page |
| `app_name` | string | ❌ | Your app's name shown in the email header |
| `subject` | string | ❌ | Custom subject line. Defaults to `"{app_name} — Reset Your Password"` |

**Example request:**
```json
{
  "to": "john@example.com",
  "reset_link": "https://myapp.com/reset-password?token=abc123xyz",
  "app_name": "Servleash"
}
```

**Success response** (`200`):
```json
{
  "success": true,
  "sent_by": "noreply@servleash.com"
}
```

---

### `POST /api/send`

**Universal send endpoint** — supports all templates and custom HTML.

**Headers:**
```
Content-Type: application/json
X-API-Key: YOUR_API_KEY
```

**Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | string | ✅ | Recipient email address |
| `template` | string | ❌ | `"otp"`, `"reset"`, or omit for custom/generic |
| `code` | string | ❌ | OTP code (required when `template` = `"otp"`) |
| `reset_link` | string | ❌ | Reset URL (required when `template` = `"reset"`) |
| `html` | string | ❌ | Custom HTML body (used when no template specified) |
| `subject` | string | ❌ | Email subject line |
| `app_name` | string | ❌ | Your app's name for branding |

**Template behavior:**
- `template: "otp"` → Uses the OTP template. Requires `code`.
- `template: "reset"` → Uses the reset template. Requires `reset_link`.
- No template + `html` provided → If the HTML doesn't start with `<!DOCTYPE`, it's wrapped in the generic branded template. Full HTML documents are sent as-is.
- No template + no `html` → Sends a minimal notification email.

**Example — OTP via universal endpoint:**
```json
{
  "to": "user@example.com",
  "template": "otp",
  "code": "123456",
  "app_name": "MyApp"
}
```

**Example — Custom HTML email:**
```json
{
  "to": "user@example.com",
  "subject": "Welcome to Servleash!",
  "html": "<h2>Welcome aboard! 🐾</h2><p>Your account is ready.</p>",
  "app_name": "Servleash"
}
```

**Example — Raw full HTML (no wrapper):**
```json
{
  "to": "user@example.com",
  "subject": "Invoice #1234",
  "html": "<!DOCTYPE html><html><body><h1>Your Invoice</h1>...</body></html>"
}
```

---

### `GET /api/health`

Public endpoint (no API key needed). Check service status.

**Example:**
```bash
curl https://nexomail.logiclaunch.in/api/health
```

**Response:**
```json
{
  "status": "ok",
  "brand": "NexoMailer",
  "api_keys": 1,
  "accounts": 1,
  "active": 1,
  "sent_today": 42
}
```

---

## Integration Examples

### Node.js / Express Backend

```javascript
// ── Environment variables ──
// MAIL_API_URL=https://nexomail.logiclaunch.in
// MAIL_API_KEY=nxm_your_key_here

const MAIL_API_URL = process.env.MAIL_API_URL || "https://nexomail.logiclaunch.in";
const MAIL_API_KEY = process.env.MAIL_API_KEY || "";

// ── Send OTP ──
async function sendOtpEmail(email, code) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (MAIL_API_KEY) headers["X-API-Key"] = MAIL_API_KEY;

    const res = await fetch(`${MAIL_API_URL}/api/send-otp`, {
      method: "POST",
      headers,
      body: JSON.stringify({ to: email, code, app_name: "MyApp" }),
      signal: AbortSignal.timeout(5000),
    });

    const data = await res.json();
    if (!res.ok) { console.warn("Mail API error:", data.error); return false; }
    return true;
  } catch (err) {
    console.warn("Mail API unreachable:", err.message);
    return false;
  }
}

// ── Send Password Reset ──
async function sendResetEmail(email, resetLink) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (MAIL_API_KEY) headers["X-API-Key"] = MAIL_API_KEY;

    const res = await fetch(`${MAIL_API_URL}/api/send-reset`, {
      method: "POST",
      headers,
      body: JSON.stringify({ to: email, reset_link: resetLink, app_name: "MyApp" }),
      signal: AbortSignal.timeout(5000),
    });

    const data = await res.json();
    if (!res.ok) { console.warn("Mail API error:", data.error); return false; }
    return true;
  } catch (err) {
    console.warn("Mail API unreachable:", err.message);
    return false;
  }
}

// ── Send Custom HTML ──
async function sendCustomEmail(email, subject, htmlBody) {
  try {
    const headers = { "Content-Type": "application/json" };
    if (MAIL_API_KEY) headers["X-API-Key"] = MAIL_API_KEY;

    const res = await fetch(`${MAIL_API_URL}/api/send`, {
      method: "POST",
      headers,
      body: JSON.stringify({ to: email, subject, html: htmlBody, app_name: "MyApp" }),
      signal: AbortSignal.timeout(5000),
    });

    const data = await res.json();
    if (!res.ok) { console.warn("Mail API error:", data.error); return false; }
    return true;
  } catch (err) {
    console.warn("Mail API unreachable:", err.message);
    return false;
  }
}

// ── Usage ──
await sendOtpEmail("user@gmail.com", "482910");
await sendResetEmail("user@gmail.com", "https://myapp.com/reset?token=xyz");
await sendCustomEmail("user@gmail.com", "Welcome!", "<h2>Thanks for joining!</h2>");
```

### Python Backend

```python
import requests

MAIL_API_URL = "https://nexomail.logiclaunch.in"
MAIL_API_KEY = "nxm_your_key_here"

headers = {
    "Content-Type": "application/json",
    "X-API-Key": MAIL_API_KEY,
}

# Send OTP
def send_otp(email: str, code: str, app_name: str = "MyApp") -> bool:
    try:
        r = requests.post(f"{MAIL_API_URL}/api/send-otp", json={
            "to": email, "code": code, "app_name": app_name,
        }, headers=headers, timeout=5)
        return r.json().get("success", False)
    except Exception as e:
        print(f"Mail error: {e}")
        return False

# Send Password Reset
def send_reset(email: str, reset_link: str, app_name: str = "MyApp") -> bool:
    try:
        r = requests.post(f"{MAIL_API_URL}/api/send-reset", json={
            "to": email, "reset_link": reset_link, "app_name": app_name,
        }, headers=headers, timeout=5)
        return r.json().get("success", False)
    except Exception as e:
        print(f"Mail error: {e}")
        return False

# Send Custom Email
def send_email(email: str, subject: str, html: str, app_name: str = "MyApp") -> bool:
    try:
        r = requests.post(f"{MAIL_API_URL}/api/send", json={
            "to": email, "subject": subject, "html": html, "app_name": app_name,
        }, headers=headers, timeout=5)
        return r.json().get("success", False)
    except Exception as e:
        print(f"Mail error: {e}")
        return False
```

### cURL Examples

```bash
# ── Send OTP ──
curl -X POST https://nexomail.logiclaunch.in/api/send-otp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: nxm_your_key_here" \
  -d '{"to":"user@example.com","code":"123456","app_name":"MyApp"}'

# ── Send Password Reset ──
curl -X POST https://nexomail.logiclaunch.in/api/send-reset \
  -H "Content-Type: application/json" \
  -H "X-API-Key: nxm_your_key_here" \
  -d '{"to":"user@example.com","reset_link":"https://myapp.com/reset?token=abc","app_name":"MyApp"}'

# ── Send Custom HTML ──
curl -X POST https://nexomail.logiclaunch.in/api/send \
  -H "Content-Type: application/json" \
  -H "X-API-Key: nxm_your_key_here" \
  -d '{"to":"user@example.com","subject":"Hello!","html":"<h2>Welcome!</h2><p>Your account is ready.</p>","app_name":"MyApp"}'

# ── Health Check (no API key needed) ──
curl https://nexomail.logiclaunch.in/api/health
```

---

## Rate Limits

NexoMailer enforces per-Gmail-account rate limits to stay within Google's quotas:

| Account Type | Per Minute | Per Day |
|-------------|-----------|---------|
| `workspace` (Google Workspace) | 30 | 2,000 |
| `standard` (free Gmail) | 20 | 500 |

When all accounts hit their limit, emails are **queued** (not dropped) and retried automatically every 3 seconds. The response will have `"queued": true`.

Multiple Gmail accounts can be linked to a single API key — NexoMailer automatically picks the account with the lowest current load (smart load balancing).

---

## Email Queue

- Emails that can't be sent immediately are queued automatically
- The queue processor runs every **3 seconds**
- Failed emails are retried up to **3 times** before being marked as `failed`
- Queue status is visible in the admin dashboard
- All this is transparent to the API consumer — you always get `"success": true`

---

## Built-in Email Templates

NexoMailer includes 3 production-ready HTML email templates:

### 1. OTP Template (`/api/send-otp` or `template: "otp"`)
- Dark header with app name
- Large dashed-border code display
- "Expires in 10 minutes" notice
- Auto-generated subject: `"{AppName} — Verification Code: {code}"`

### 2. Password Reset Template (`/api/send-reset` or `template: "reset"`)
- Dark header with app name
- Blue "Reset Password" CTA button
- Copyable link fallback
- "Expires in 1 hour" notice
- Auto-generated subject: `"{AppName} — Reset Your Password"`

### 3. Generic Template (`/api/send` with `html` body)
- Dark header with app name
- Your HTML content rendered in a clean card
- Automatically wraps partial HTML in the branded template
- Full `<!DOCTYPE html>` documents are sent as-is (no wrapping)

All templates include a subtle footer: *"Powered by NexoMailer — Logic Launch Software Solutions"*

---

## Error Handling

| HTTP Status | Body | Meaning |
|------------|------|---------|
| `200` | `{"success": true, "sent_by": "..."}` | Email sent immediately |
| `200` | `{"success": true, "queued": true, "reason": "..."}` | Email queued for retry |
| `400` | `{"error": "Missing 'to'"}` | Required field missing |
| `400` | `{"error": "Missing 'to' or 'code'"}` | OTP endpoint missing fields |
| `400` | `{"error": "Missing 'to' or 'reset_link'"}` | Reset endpoint missing fields |
| `401` | `{"error": "Invalid or missing API key"}` | Bad or missing API key |

**Recommended client-side pattern:**
```javascript
const res = await fetch(`${MAIL_API_URL}/api/send-otp`, { ... });
const data = await res.json();

if (data.success) {
  // Email sent or queued — either way, tell the user to check their inbox
  console.log(data.queued ? "Email queued" : `Sent via ${data.sent_by}`);
} else {
  console.error("Failed:", data.error);
}
```

---

## Self-Hosting

### Requirements
- Python 3.10+
- A Google Cloud project with OAuth2 credentials (Gmail API enabled)

### Docker (recommended)

```bash
git clone https://github.com/siiiidddexe/nexomailer.git
cd nexomailer

# Configure
cp .env.example .env
# Edit .env — set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ADMIN_PASSWORD

# Run
docker compose up -d

# Access dashboard at http://localhost:5050
```

### Manual

```bash
git clone https://github.com/siiiidddexe/nexomailer.git
cd nexomailer
pip install -r requirements.txt

cp .env.example .env
# Edit .env

python app.py
# Dashboard at http://localhost:5050
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | ✅ | — | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | ✅ | — | Google OAuth2 client secret |
| `ADMIN_PASSWORD` | ✅ | `admin` | Dashboard login password |
| `SECRET_KEY` | ❌ | auto-generated | Flask session secret |
| `PORT` | ❌ | `5050` | Server port |
| `DEBUG` | ❌ | `false` | Enable Flask debug mode |
| `DATA_DIR` | ❌ | `./data` | Where JSON data files are stored |
| `CORS_ORIGINS` | ❌ | `*` | Comma-separated allowed origins |
| `SESSION_TTL_HOURS` | ❌ | `72` | Admin session duration |
| `OAUTH_REDIRECT_URI` | ❌ | auto-detected | Override OAuth callback URL (for proxied setups) |

### Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable the **Gmail API**
3. Go to **Credentials** → Create **OAuth 2.0 Client ID** (Web application)
4. Add authorized redirect URI: `https://yourdomain.com/oauth/callback`
5. Copy the Client ID and Client Secret into your `.env`
6. Go to **OAuth consent screen** → Add test users (or publish the app)

### Linking Gmail Accounts (via Dashboard)

1. Log into the dashboard at `https://yourdomain.com`
2. Create an API key (give it a name like "MyApp")
3. Click the API key → "Add Gmail Account" → Sign in with Google
4. The Gmail account is now linked and ready to send emails for that API key

---

## API Key Structure (Internal Reference)

Each API key stores this structure in `data/api_keys.json`:

```json
{
  "key": "nxm_abc123...",
  "name": "Servleash",
  "company": "Servleash Inc",
  "active": true,
  "created": "2025-01-01T00:00:00",
  "accounts": [
    {
      "email": "noreply@servleash.com",
      "account_type": "standard",
      "active": true,
      "added": "2025-01-01T00:00:00",
      "credentials": { "...google oauth tokens..." }
    }
  ]
}
```

- `name` — Display name for the client/project
- `company` — Used as the default `app_name` in email templates (shows in email header)
- `accounts` — Array of linked Gmail accounts. Emails are sent from these.
- `account_type` — `"standard"` (free Gmail, 500/day) or `"workspace"` (Google Workspace, 2000/day)

---

## FAQ

**Q: Do emails go to spam?**
A: No. NexoMailer sends via the Gmail API (not SMTP), so emails come from a real Gmail/Workspace account with full Google authentication. Deliverability is excellent.

**Q: What happens if the API is down?**
A: Your request will timeout. Use a 5-second timeout and handle the failure gracefully in your app (e.g., "Email service temporarily unavailable, try again").

**Q: Can I send to multiple recipients at once?**
A: Not in a single request. Call the endpoint once per recipient. The queue handles concurrency.

**Q: What if my Gmail account token expires?**
A: NexoMailer automatically refreshes expired tokens using the stored refresh token. No manual intervention needed.

**Q: Is there a sandbox/test mode?**
A: No — all emails are real. Use a test email address during development.

**Q: Can I use my own HTML without the NexoMailer template?**
A: Yes. Send a full `<!DOCTYPE html>` document in the `html` field of `/api/send` — it won't be wrapped in any template.

---

## TL;DR Cheat Sheet

```
BASE URL: https://nexomail.logiclaunch.in
AUTH:     X-API-Key: nxm_your_key

POST /api/send-otp    → { to, code, app_name? }
POST /api/send-reset  → { to, reset_link, app_name? }
POST /api/send        → { to, subject?, html?, template?, code?, reset_link?, app_name? }
GET  /api/health      → no auth needed

Response: { success: true, sent_by: "..." }
    or:   { success: true, queued: true }
Error:    { error: "message" }
```

---

*Built by [Logic Launch Software Solutions](https://wa.me/918549013115) • NexoMailer*
