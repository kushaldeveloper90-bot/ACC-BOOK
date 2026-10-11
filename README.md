# AccBook - Accounting Software

A desktop accounting software built with Python (Flask) + HTML/CSS.

## Features
- Login / User Authentication (multi-user with roles)
- Dashboard with live summary
- Sales / Invoicing with printable invoices
- Purchase / Expense recording
- Full Ledger with filters
- Party Management (Customers & Suppliers)
- User Management (Admin only)
- SQLite database (no server needed)

## Setup & Run

### Step 1: Install Python (if not installed)
Download from https://python.org

### Step 2: Install dependencies
Open terminal / command prompt in this folder and run:
```
pip install -r requirements.txt
```

### Step 3: Run the app
```
python app.py
```

### Step 4: Open in browser
Go to: http://127.0.0.1:5000

## Default Login
- Username: admin
- Password: admin123

**Change your password after first login!**

## Data Storage
All data is stored in `instance/accounting.db` (SQLite file).
Back up this file regularly to protect your data.
