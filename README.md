# ErosMail - Email Campaign Management System

A powerful, feature-rich email campaign management system built with FastAPI, SQLAlchemy, and SQLite.

## Features

### Core Functionality
- **Campaign Management**: Full CRUD operations for email campaigns
- **Multiple Sending Methods**: Support for SMTP and API-based email sending (NexoMail)
- **Background Scheduler**: Autonomous email sending with intelligent scheduling
- **Rate Limiting**: Per-account rate limits (hourly/daily) to prevent IP blocking
- **Pause Hours**: Configure time windows when sending should be paused
- **Repeat Scheduling**: Daily, weekly, monthly, or yearly campaign repetition

### User Management
- **Role-Based Access Control**: Superadmin and admin roles
- **Permission System**: Granular permissions for different features
- **User Management**: Superadmin can create, disable, and manage users

### Email Features
- **HTML & Plain Text**: Support for both HTML and plain text emails
- **HTML Preview**: Preview HTML emails before sending
- **Recipients Preview**: View recipients from uploaded Excel templates
- **Personalization**: Use {name} placeholder for personalized emails
- **Excel Templates**: Upload recipients via Excel files with name and email columns

### Monitoring & Analytics
- **Real-time Progress**: Live progress tracking during campaign sending
- **Delivery Logs**: Detailed logs for each email sent
- **NexoMail Integration**: Fetch and sync delivery status from NexoMail API
- **Reports & Analytics**: Comprehensive statistics and performance metrics
- **Campaign History**: View all past campaigns and their results

### UI/UX
- **Responsive Design**: Mobile-friendly interface with hamburger menu
- **Left Sidebar Navigation**: Clean, modern webapp-style layout
- **Icons**: Professional icon-based navigation (no emojis)
- **Status Indicators**: Color-coded status badges for campaigns and logs

## Technology Stack

- **Backend**: FastAPI (Python 3.11+)
- **Database**: SQLite (file-based, persistent storage)
- **ORM**: SQLAlchemy
- **Email Sending**: aiosmtplib (SMTP), httpx (API)
- **Task Scheduling**: asyncio (background tasks)
- **Frontend**: Jinja2 templates, Tailwind CSS
- **Containerization**: Docker, Docker Compose

## Installation

### Local Development

1. Clone the repository:
```bash
git clone <repository-url>
cd erosmail
```

2. Create a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Run the application:
```bash
uvicorn main:app --reload --port 8001
```

5. Access the application at `http://localhost:8001`

### Default Credentials
- **Username**: superadmin
- **Password**: superadmin123
- **IMPORTANT**: Change the password after first login!

## Dokploy Deployment

### Prerequisites
- Dokploy server installed and running
- SSH access to your Dokploy server
- Git repository with your code

### Deployment Steps

1. **Push your code to a Git repository** (GitHub, GitLab, etc.)

2. **Create a new application in Dokploy**:
   - Go to your Dokploy dashboard
   - Click "Create Application"
   - Select "Docker Compose" as the deployment type
   - Connect your Git repository

3. **Configure Environment Variables**:
   ```
   SECRET_KEY=your-super-secret-key-change-in-production
   DATABASE_URL=sqlite:////app/data/erosmail.db
   PYTHONUNBUFFERED=1
   ```

4. **Deploy**:
   - Click "Deploy" in Dokploy
   - Wait for the build and deployment to complete
   - Access your application at the assigned domain

### Docker Configuration

The application includes:
- **Dockerfile**: Optimized Python 3.11 slim image with health checks
- **docker-compose.yml**: Production-ready configuration with persistent volumes
- **.env.example**: Template for environment variables

### Persistent Storage

The Docker setup uses two volumes:
- `erosmail_uploads`: Stores uploaded Excel templates
- `erosmail_data`: Stores the SQLite database

These volumes ensure your data persists across deployments.

## Usage Guide

### Creating a Campaign

1. Navigate to "Campaigns" → "New Campaign"
2. Fill in campaign details:
   - Campaign name
   - Email subject
   - Email body (use {name} for personalization)
   - Email type (Plain Text or HTML)
3. Upload recipients Excel file (columns: name, email)
4. Select SMTP accounts and/or API accounts
5. Configure scheduling options:
   - Enable repeat scheduling (daily/weekly/monthly/yearly)
   - Set pause hours (24-hour format)
6. Save the campaign

### Sending a Campaign

1. Go to the campaign detail page
2. Click "Send Campaign"
3. Review recipients and settings
4. Choose "Send Now" or "Schedule for Later"
5. Monitor progress in real-time

### Managing SMTP Accounts

1. Navigate to "SMTP Accounts"
2. Click "Add SMTP Account"
3. Enter SMTP server details:
   - Host, port, username, password
   - Rate limits (per hour and per day)
4. Save the account

### Managing API Accounts (NexoMail)

1. Navigate to "API Accounts"
2. Click "Add API Account"
3. Select provider (NexoMail or Custom)
4. Enter API key and configuration
5. Set rate limits
6. Save the account

### Syncing NexoMail Logs

1. Go to "API Accounts"
2. Click "Sync Logs" for a specific account
3. Or use "Sync All NexoMail Logs" to update all accounts
4. View updated delivery status in campaign logs

### User Management (Superadmin Only)

1. Navigate to "Admin"
2. Create new users with specific roles and permissions
3. Enable/disable user accounts
4. Configure granular permissions for each user

## API Endpoints

### NexoMail Integration
- `GET /api-accounts/{id}/logs`: Fetch logs from NexoMail API
- `POST /api-accounts/{id}/sync-logs`: Sync delivery status to local database
- `POST /sync-all-nexomail-logs`: Sync all NexoMail accounts

### Campaign Management
- `GET /campaigns`: List all campaigns
- `GET /campaign/{id}`: View campaign details
- `POST /campaign/create`: Create new campaign
- `POST /campaign/{id}/update`: Update campaign
- `POST /campaign/{id}/delete`: Delete campaign
- `POST /campaign/{id}/send`: Send campaign
- `GET /campaign/{id}/progress`: Get real-time progress

## Database Schema

### Key Tables
- **users**: User accounts with roles and permissions
- **campaigns**: Email campaigns with scheduling settings
- **smtp_accounts**: SMTP server configurations
- **api_accounts**: API provider configurations (NexoMail)
- **email_logs**: Delivery logs for each email sent
- **rate_limit_logs**: Rate limit tracking per account

## Security Considerations

1. **Change Default Password**: Always change the superadmin password after first login
2. **Use Strong SECRET_KEY**: Generate a random secret key for production
3. **HTTPS**: Always use HTTPS in production (Dokploy handles this automatically)
4. **Rate Limiting**: Configure appropriate rate limits to prevent IP blocking
5. **Backup Database**: Regularly backup the SQLite database file

## Troubleshooting

### Database Issues
- Ensure the `/app/data` directory is writable
- Check volume mounts in docker-compose.yml
- Verify DATABASE_URL environment variable

### Email Sending Failures
- Check SMTP credentials and server settings
- Verify rate limits are not exceeded
- Review error messages in email logs
- For NexoMail, sync logs to get detailed delivery status

### Scheduler Not Running
- Check application logs for scheduler startup messages
- Verify the application is running with a single worker
- Ensure no blocking operations in the event loop

## Support

For issues or questions:
1. Check the application logs
2. Review this README
3. Check NexoMail API documentation if using API sending
4. Open an issue in the repository

## License

[Your License Here]

## Credits

Built with FastAPI, SQLAlchemy, and Tailwind CSS.
