# Johnson Church of Christ - Accounts Payable System

A responsive web application for managing accounts payable requests, invoice entry, approvals workflow, general ledger accounts, and users.

Works great on desktop browsers and mobile devices (responsive Tailwind UI).

## Features

- **Invoice/Request Entry**: Key new AP requests with vendor, amount, dates, description, GL coding, and requester.
- **Edit Requests**: Edit pending requests.
- **General Ledger Management**: Add/Edit/Remove GL accounts (seeded from provided Account List). Assign Primary, Secondary, Tertiary approvers per account (from user dropdown).
- **Users Management**: Add/Edit/Remove users (username, first/last name, email).
- **Status & Lookup**: View all requests with status (Pending/Approved/Rejected), progress through approval chain. Filter by date range, account, description/vendor, status. Export to CSV.
- **Automated Email Workflow**:
  - On keying a request, it is routed to the first approver (Primary) via email.
  - Email contains full request details + "Approve" and "Reject" buttons (links).
  - Approve: advances to next approver (or completes), notifies accordingly.
  - Reject: notifies original requester.
- Real email delivery configured (with full Email Log for auditing and testing).

## Getting Started

### Prerequisites
- Python 3.9+ (tested on Windows)
- pip

### Setup & Run

1. Open PowerShell or terminal in `C:\GrokAccountsPayable`

2. Create and activate a virtual environment (recommended):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Run the app:

```powershell
python app.py
```

5. Open browser to: http://127.0.0.1:5000

The app will auto-create `ap.db` SQLite database on first run and seed:

- Expense GL accounts from the provided "JOHNSON CHURCH OF CHRIST_Account List.xlsx"
- A few sample users for testing workflow

## Using the App

### 1. Users Screen
- Add at least a few users with real emails if using SMTP (or any for testing).
- Note usernames and emails.

### 2. General Ledger Screen
- Review seeded accounts (many expense accounts from the list).
- For testing workflow, edit a GL account and assign Primary (required), optionally Secondary and Tertiary approvers.
- Only users appear in dropdowns.

### 3. New Request (Input Screen)
- Fill standard fields: Vendor/Payee, Invoice # (opt), Invoice Date, Amount, Description.
- Select GL Account (expense accounts shown).
- Select "Requested By".
- Save: System creates request, sets status Pending, routes email to first approver in chain for that GL.

### 4. Status & Lookup
- See all requests in a filterable, searchable table.
- Columns include current status and progress (e.g. "Pending - Awaiting Secondary (Name)").
- Filters: Date range, Account #, free text search (vendor/desc), status.
- Export CSV of current view.
- Click "View/Edit" on a request to see details or modify if still Pending.
- From details you can also see full approval chain.

### 5. Approval via Email (or direct links)
- Approval request emails are sent in real time to the assigned approvers.
- Emails appear in the **Email Log** tab (with full content and status).
- Approvers can click the **Approve** or **Reject** buttons directly in the email, or you can copy the links from the Email Log for testing.

## Email Configuration

This installation is **currently configured to send real emails**.

### Current SMTP Settings

The app is using the following SMTP configuration (loaded from `.env`):

- **SMTP Server**: `SMTP.JohnsonChurchofChrist.Com`
- **Port**: `587`
- **Username**: `AccountsPayable@JohnsonChurchofChrist.com`
- **From Address**: `AccountsPayable@JohnsonChurchofChrist.com`
- **Password**: Set in `.env` (not shown here for security)

The app automatically loads these settings from the `.env` file at startup.

### How to Verify Emails Are Working

1. Create a new Accounts Payable request.
2. Go to the **Email Log** tab.
3. Look for the latest entry:
   - Status should show **`sent`** (not `simulated` or `failed`).
   - The recipient (approver) should receive a real email containing the request details and **Approve** / **Reject** links.

If you see `failed`, check the error details in the Email Log or the console output when starting the app.

### Changing the SMTP Settings

If you need to update the SMTP server, username, or password:

1. Edit the `.env` file in this folder:
   ```powershell
   notepad .env
   ```

2. Update the relevant lines:
   ```env
   SMTP_SERVER=your.new.server.com
   SMTP_PORT=587
   SMTP_USERNAME=newuser@domain.com
   SMTP_PASSWORD=newpassword
   FROM_EMAIL=AccountsPayable@JohnsonChurchofChrist.com
   ```

3. Restart the application:
   ```powershell
   # Stop the current server with Ctrl+C, then run:
   python app.py
   ```

A template with comments is also available in `.env.example`.

### Common Notes
- Most church or organizational mail servers use port **587** with STARTTLS.
- If using Gmail or Microsoft 365, you may need an "App Password" instead of your regular login password.
- All sent emails (successful or failed) are always recorded in the **Email Log** for auditing.
- The recipient should receive a real email with working Approve/Reject buttons.

If emails fail, check the console output and the Email Log for the error message.

## Database

- `ap.db` : SQLite file created next to app.py
- You can inspect it with DB Browser for SQLite or `sqlite3 ap.db`

## Notes / Future Enhancements
- Attachments: Currently description/notes field. Can extend for file uploads.
- Full user login/auth for UI: Currently open for simplicity (internal use).
- Notifications on final approval.
- Dashboard stats.
- Budget vs actual (future).

## Support
For Johnson Church of Christ internal use.

Built with Flask + Tailwind CSS (CDN) + SQLite + vanilla JS.
