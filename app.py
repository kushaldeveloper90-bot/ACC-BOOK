import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_from_directory, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, csv, uuid, hashlib
from io import StringIO
from datetime import datetime, date
from functools import wraps

import sys

app = Flask(__name__)
if not app.debug:
    file_handler = RotatingFileHandler('flask_error.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.ERROR)
    app.logger.addHandler(file_handler)

app.secret_key = 'accounting_secret_key_2025'

def to_float(val, default=0.0):
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).strip()
    if not val_str:
        return default
    try:
        return float(val_str)
    except (ValueError, TypeError):
        return default

def to_int(val, default=0):
    if val is None:
        return default
    if isinstance(val, int):
        return val
    val_str = str(val).strip()
    if not val_str:
        return default
    try:
        return int(float(val_str))
    except (ValueError, TypeError):
        return default


@app.template_filter('format_date')
def format_date(value):
    if not value:
        return value
    try:
        from datetime import datetime
        dt = datetime.strptime(str(value), '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except ValueError:
        return value


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Ensure the database is saved next to the .exe when compiled
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
if not os.path.exists(INSTANCE_DIR):
    os.makedirs(INSTANCE_DIR)

DB = os.path.join(INSTANCE_DIR, 'accounting.db')
SYSTEM_DB = os.path.join(INSTANCE_DIR, 'system.db')

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(INSTANCE_DIR, filename)

# DB helpers
def get_system_db():
    conn = sqlite3.connect(SYSTEM_DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_db():
    active_db_filename = session.get('active_db', 'accounting.db')
    db_path = os.path.join(INSTANCE_DIR, active_db_filename)
    if not os.path.exists(db_path):
        try: init_db(active_db_filename)
        except Exception: pass
    else:
        try:
            test_c = sqlite3.connect(db_path)
            t = test_c.cursor().execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            test_c.close()
            if not t: init_db(active_db_filename)
        except Exception:
            try: init_db(active_db_filename)
            except Exception: pass
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def log_audit(conn, action, table_name, record_id, old_values, new_values):
    import json
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        username = current_user.username if current_user.is_authenticated else 'System'
        
        old_val_str = json.dumps(dict(old_values)) if old_values else None
        new_val_str = json.dumps(dict(new_values)) if new_values else None
        
        conn.execute('''INSERT INTO audit_logs (user_id, username, action, table_name, record_id, old_values, new_values)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                     (user_id, username, action, table_name, record_id, old_val_str, new_val_str))
    except Exception as e:
        print("Audit Log Error:", e)


def init_system_db():
    conn = get_system_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            db_filename TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS license_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            install_date TEXT NOT NULL,
            is_activated INTEGER DEFAULT 0,
            install_id TEXT,
            company_limit INTEGER DEFAULT 1
        );
    """)
    conn.commit()

    # Migrate logic if old accounting.db exists but companies is empty
    existing_companies = c.execute("SELECT COUNT(*) as cnt FROM companies").fetchone()['cnt']
    if existing_companies == 0:
        import os
        from datetime import date
        import uuid
        old_db_path = os.path.join(INSTANCE_DIR, 'accounting.db')
        if os.path.exists(old_db_path):
            # Migrate
            c.execute("INSERT INTO companies (name, db_filename) VALUES ('Main Company', 'accounting.db')")
            
            import sqlite3
            old_conn = sqlite3.connect(old_db_path)
            old_conn.row_factory = sqlite3.Row
            old_c = old_conn.cursor()
            try:
                old_lic = old_c.execute("SELECT * FROM license_info WHERE id=1").fetchone()
                if old_lic:
                    c.execute("INSERT INTO license_info (id, install_date, is_activated, install_id) VALUES (1, ?, ?, ?)", 
                              (old_lic['install_date'], old_lic['is_activated'], old_lic.get('install_id', uuid.uuid4().hex[:8].upper())))
            except:
                pass
            old_conn.close()
        else:
            # First time install
            c.execute("INSERT INTO companies (name, db_filename) VALUES ('Main Company', 'accounting.db')")

    # Ensure license exists
    lic = c.execute("SELECT * FROM license_info WHERE id=1").fetchone()
    if not lic:
        import uuid
        from datetime import date
        install_id = uuid.uuid4().hex[:8].upper()
        c.execute("INSERT INTO license_info (id, install_date, is_activated, install_id, company_limit) VALUES (1, ?, 0, ?, 1)", 
                  (date.today().isoformat(), install_id))
    else:
        # Migrate multi_company column if missing
        try: c.execute("ALTER TABLE license_info ADD COLUMN company_limit INTEGER DEFAULT 1")
        except: pass
        
    conn.commit()
    conn.close()

def init_db(db_filename='accounting.db'):
    active_db_path = os.path.join(INSTANCE_DIR, db_filename)
    conn = sqlite3.connect(active_db_path)
    conn.row_factory = sqlite3.Row

    c = conn.cursor()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT,
            role TEXT DEFAULT 'user',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS parties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            phone TEXT,
            address TEXT,
            gstin TEXT,
            opening_balance REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT UNIQUE NOT NULL,
            invoice_date TEXT NOT NULL,
            party_id INTEGER,
            party_name TEXT,
            subtotal REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            cgst_pct REAL DEFAULT 0,
            sgst_pct REAL DEFAULT 0,
            igst_pct REAL DEFAULT 0,
            cgst_amount REAL DEFAULT 0,
            sgst_amount REAL DEFAULT 0,
            igst_amount REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'unpaid',
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (party_id) REFERENCES parties(id)
        );
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            description TEXT,
            qty REAL,
            rate REAL,
            amount REAL,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_no TEXT,
            bill_date TEXT NOT NULL,
            party_id INTEGER,
            party_name TEXT,
            category TEXT,
            description TEXT,
            amount REAL DEFAULT 0,
            discount_pct REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            cgst_pct REAL DEFAULT 0,
            sgst_pct REAL DEFAULT 0,
            igst_pct REAL DEFAULT 0,
            cgst_amount REAL DEFAULT 0,
            sgst_amount REAL DEFAULT 0,
            igst_amount REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_date TEXT NOT NULL,
            type TEXT NOT NULL,
            party_id INTEGER,
            party_name TEXT,
            amount REAL DEFAULT 0,
            payment_mode TEXT,
            reference_no TEXT,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            ref_type TEXT,
            ref_id INTEGER,
            ref_no TEXT,
            party_id INTEGER,
            party_name TEXT,
            description TEXT,
            debit REAL DEFAULT 0,
            credit REAL DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS company_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT 'My Company',
            address TEXT,
            phone TEXT,
            gstin TEXT,
            state TEXT,
            bank_details TEXT,
            cash_opening REAL DEFAULT 0,
            bank_opening REAL DEFAULT 0,
            logo_filename TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            table_name TEXT NOT NULL,
            record_id INTEGER,
            old_values TEXT,
            new_values TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL DEFAULT 0,
            hsn TEXT,
            opening_stock REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS purchase_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER,
            description TEXT,
            qty REAL,
            rate REAL,
            amount REAL,
            FOREIGN KEY (purchase_id) REFERENCES purchases(id)
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_date TEXT NOT NULL,
            category TEXT NOT NULL,
            payee TEXT,
            amount REAL DEFAULT 0,
            description TEXT,
            status TEXT DEFAULT 'Paid',
            payment_mode TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inc_date TEXT NOT NULL,
            category TEXT NOT NULL,
            payer TEXT,
            amount REAL DEFAULT 0,
            description TEXT,
            status TEXT DEFAULT 'Received',
            payment_mode TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS other_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_date TEXT NOT NULL,
            category TEXT NOT NULL,
            payee TEXT,
            amount REAL DEFAULT 0,
            description TEXT,
            status TEXT DEFAULT 'Paid',
            payment_mode TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            client_id INTEGER,
            client_name TEXT,
            description TEXT,
            budget REAL DEFAULT 0,
            status TEXT DEFAULT 'Active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        


        CREATE TABLE IF NOT EXISTS timesheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            employee_name TEXT NOT NULL,
            task_date TEXT NOT NULL,
            hours REAL DEFAULT 0,
            task_description TEXT,
            hourly_rate REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_date TEXT NOT NULL,
            from_account TEXT NOT NULL,
            to_account TEXT NOT NULL,
            amount REAL DEFAULT 0,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS app_modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1
        );
    ''')
    try:
        c.execute("ALTER TABLE invoices ADD COLUMN discount_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN discount_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN cgst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN sgst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN igst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN cgst_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN sgst_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE invoices ADD COLUMN igst_amount REAL DEFAULT 0")
    except: pass
    try:
        c.execute("ALTER TABLE purchases ADD COLUMN discount_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN discount_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN cgst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN sgst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN igst_pct REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN cgst_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN sgst_amount REAL DEFAULT 0")
        c.execute("ALTER TABLE purchases ADD COLUMN igst_amount REAL DEFAULT 0")
    except: pass
    try:
        c.execute("ALTER TABLE expenses ADD COLUMN ref_no TEXT")
    except Exception:
        pass
        
    # Project ID linkages
    try: c.execute("ALTER TABLE invoices ADD COLUMN project_id INTEGER")
    except: pass
    try: c.execute("ALTER TABLE expenses ADD COLUMN project_id INTEGER")
    except: pass
    try: c.execute("ALTER TABLE purchases ADD COLUMN project_id INTEGER")
    except: pass
    
    try: c.execute("ALTER TABLE parties ADD COLUMN state TEXT")
    except: pass
    try: c.execute("ALTER TABLE parties ADD COLUMN city TEXT")
    except: pass
    
    # New GST Fields
    try: c.execute("ALTER TABLE invoices ADD COLUMN place_of_supply TEXT")
    except: pass
    try: c.execute("ALTER TABLE invoices ADD COLUMN reverse_charge INTEGER DEFAULT 0")
    except: pass
    
    try: c.execute("ALTER TABLE purchases ADD COLUMN place_of_supply TEXT")
    except: pass
    try: c.execute("ALTER TABLE purchases ADD COLUMN reverse_charge INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE purchases ADD COLUMN itc_eligibility TEXT DEFAULT 'Eligible'")
    except: pass
    
    try: c.execute("ALTER TABLE timesheets ADD COLUMN rate_type TEXT DEFAULT 'Hourly'")
    except: pass
    
    # Create default company
    existing_comp = c.execute("SELECT id FROM company_profile WHERE id=1").fetchone()
    if not existing_comp:
        c.execute("INSERT INTO company_profile (id, name) VALUES (1, 'My Company')")
        


    # Create default admin
    existing = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not existing:
        c.execute("INSERT INTO users (username, password, full_name, role) VALUES (?,?,?,?)",
                  ('admin', generate_password_hash('admin123'), 'Administrator', 'admin'))
                  
    # Initialize default modules
    default_modules = [
        'invoices', 'purchases', 'receipts', 'expenses', 
        'products', 'financials', 'projects'
    ]
    for mod in default_modules:
        c.execute("INSERT OR IGNORE INTO app_modules (module_name, is_active) VALUES (?, 1)", (mod,))

    conn.commit()
    conn.close()

# Auto-initialize databases on startup (ensures tables exist on WSGI / PythonAnywhere)
try:
    init_system_db()
    init_db("accounting.db")
except Exception as e:
    app.logger.error(f"Startup DB init error: {e}")


# â”€â”€ User model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class User(UserMixin):
    def __init__(self, id, username, full_name, role):
        self.id = id
        self.username = username
        self.full_name = full_name
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if u:
        return User(u['id'], u['username'], u['full_name'], u['role'])
    return None

@app.context_processor
def inject_modules():
    try:
        conn = get_db()
        mods = conn.execute("SELECT module_name, is_active FROM app_modules").fetchall()
        conn.close()
        active_modules = {m['module_name']: bool(m['is_active']) for m in mods}
    except:
        active_modules = {}
    return dict(active_modules=active_modules)

@app.before_request
def enforce_modules():
    if request.endpoint in ['static', 'login', 'logout', 'activate', 'modules', 'dashboard', 'select_company', 'set_company', 'activate_multi']:
        return
    try:
        conn = get_db()
        mods = conn.execute("SELECT module_name, is_active FROM app_modules").fetchall()
        conn.close()
        active = {m['module_name']: bool(m['is_active']) for m in mods}
        
        ep = request.endpoint or ''
        if ep == 'dashboard' and not active.get('dashboard', True):
            return redirect(url_for('invoices'))
        if ep == 'audit_trail' and not active.get('audit_trail', True):
            return redirect(url_for('dashboard'))
        if ep.startswith('invoice') and not active.get('invoices', True):
            return redirect(url_for('dashboard' if active.get('dashboard', True) else 'purchases'))
        if ep.startswith('purchase') and not active.get('purchases', True):
            return redirect(url_for('dashboard'))
        if ep.startswith('receipt') and not active.get('receipts', True):
            return redirect(url_for('dashboard'))
        if ep.startswith('expense') and not active.get('expenses', True):
            return redirect(url_for('dashboard'))
        if ep.startswith('product') and not active.get('products', True):
            return redirect(url_for('dashboard'))
        if ep in ['financials', 'cash_book', 'bank_book', 'ledger', 'supplier_ledger', 'transfer', 'transfer_list', 'edit_transfer', 'delete_transfer'] and not active.get('financials', True):
            return redirect(url_for('dashboard'))
        if ep in ['projects', 'timesheets'] and not active.get('projects', True):
            return redirect(url_for('dashboard'))
    except:
        pass



@app.before_request
def check_trial():
    if request.endpoint in ['activate', 'static', 'activate_multi'] or request.path.startswith('/static'):
        return
    conn = get_system_db()
    lic = conn.execute("SELECT * FROM license_info WHERE id=1").fetchone()
    conn.close()
    if lic and lic['is_activated'] == 0:
        install_date = datetime.strptime(lic['install_date'], '%Y-%m-%d').date()
        days_passed = (date.today() - install_date).days
        if days_passed > 7:
            return redirect(url_for('activate'))

@app.route('/activate', methods=['GET', 'POST'])
def activate():
    conn = get_system_db()
    lic = conn.execute("SELECT * FROM license_info WHERE id=1").fetchone()
    if not lic:
        conn.close()
        return redirect(url_for('login'))
        
    if lic['is_activated'] == 1:
        conn.close()
        return redirect(url_for('dashboard'))
        
    install_date = datetime.strptime(lic['install_date'], '%Y-%m-%d').date()
    days_passed = (date.today() - install_date).days
    install_id = lic['install_id'] if 'install_id' in lic.keys() and lic['install_id'] else 'UNKNOWN'
    expected_key = hashlib.md5((install_id + "SK-CLOUDEDGE-2026-SECRET").encode()).hexdigest()[:12].upper()
    
    if request.method == 'POST':
        key = request.form.get('activation_key', '').strip()
        if key == expected_key or key == 'SK-CLOUDEDGE-UNLOCK':
            conn.execute("UPDATE license_info SET is_activated = 1 WHERE id=1")
            conn.commit()
            conn.close()
            flash("Software Activated Successfully!", "success")
            return redirect(url_for('login'))
        else:
            flash("Invalid Activation Key.", "error")
            
    conn.close()
    return render_template('activate.html', days_passed=days_passed, install_id=install_id)

# â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/')
@app.route('/select_company', methods=['GET', 'POST'])
def select_company():
    conn = get_system_db()
    
    if request.method == 'POST':
        # Create new company
        lic = conn.execute("SELECT company_limit FROM license_info WHERE id=1").fetchone()
        existing_companies = conn.execute("SELECT COUNT(*) as c FROM companies").fetchone()['c']
        
        if existing_companies >= lic['company_limit']:
            conn.close()
            return redirect(url_for('activate_multi'))
            
        company_name = request.form.get('company_name', '').strip()
        if company_name:
            import re
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '', company_name.replace(' ', '_')).lower()
            import uuid
            db_filename = f"company_{safe_name}_{uuid.uuid4().hex[:6]}.db"
            conn.execute("INSERT INTO companies (name, db_filename) VALUES (?, ?)", (company_name, db_filename))
            conn.commit()
            
            # Initialize the new company's database
            init_db(db_filename)
            
            flash(f"Company '{company_name}' created successfully!", "success")
        
    companies = conn.execute("SELECT * FROM companies ORDER BY created_at DESC").fetchall()
    conn.close()
    
    # If no companies exist, force create one (fallback)
    if not companies:
        conn = get_system_db()
        conn.execute("INSERT INTO companies (name, db_filename) VALUES ('Main Company', 'accounting.db')")
        conn.commit()
        conn.close()
        init_db('accounting.db')
        return redirect(url_for('select_company'))
        
    return render_template('select_company.html', companies=companies)

@app.route('/set_company/<int:comp_id>')
def set_company(comp_id):
    conn = get_system_db()
    comp = conn.execute("SELECT * FROM companies WHERE id=?", (comp_id,)).fetchone()
    conn.close()
    if comp:
        session['active_db'] = comp['db_filename']
        session['company_name'] = comp['name']
        flash(f"Switched to {comp['name']}", "info")
        logout_user() # Force re-login for the new company
        return redirect(url_for('login'))
    return redirect(url_for('select_company'))

@app.route('/activate_multi', methods=['GET', 'POST'])
def activate_multi():
    conn = get_system_db()
    lic = conn.execute("SELECT * FROM license_info WHERE id=1").fetchone()
    
    current_limit = lic['company_limit']
    target_limit = current_limit + 1
    install_id = lic['install_id']
    
    if request.method == 'POST':
        key = request.form.get('activation_key', '').strip().upper()
        import hashlib
        expected_key = hashlib.md5(f"AGT-{install_id}-{target_limit}-SEC".encode()).hexdigest()[:10].upper()
        
        if key == expected_key or key == 'SK-CLOUDEDGE-UNLOCK':
            conn.execute("UPDATE license_info SET company_limit = ? WHERE id=1", (target_limit,))
            conn.commit()
            conn.close()
            flash(f"Company Limit upgraded to {target_limit}!", "success")
            return redirect(url_for('select_company'))
        else:
            flash("Invalid Upgrade Key", "danger")
            
    conn.close()
    return render_template('activate_multi.html', install_id=install_id, target_limit=target_limit)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not session.get('active_db'):
        return redirect(url_for('select_company'))
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        conn = get_db()
        u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if u and check_password_hash(u['password'], password):
            user = User(u['id'], u['username'], u['full_name'], u['role'])
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# â”€â”€ Dashboard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    total_sales = conn.execute("SELECT COALESCE(SUM(total),0) as t FROM invoices").fetchone()['t']
    total_purchases = conn.execute("SELECT COALESCE(SUM(total),0) as t FROM purchases").fetchone()['t']
    total_expenses = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM expenses").fetchone()['t']
    total_other_incomes = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM incomes WHERE status='Received'").fetchone()['t']
    total_other_expenses = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM other_expenses WHERE status='Paid'").fetchone()['t']
    unpaid_invoices = conn.execute("SELECT COUNT(*) as c FROM invoices WHERE status='unpaid'").fetchone()['c']
    
    monthly_sales = conn.execute("""
        SELECT strftime('%m/%Y', dt) as month, SUM(total) as total FROM (
            SELECT invoice_date as dt, COALESCE(total, 0) as total FROM invoices
            UNION ALL
            SELECT inc_date as dt, COALESCE(amount, 0) as total FROM incomes WHERE status='Received'
        ) GROUP BY month ORDER BY MAX(dt) DESC LIMIT 6
    """).fetchall()
    
    monthly_expenses = conn.execute("""
        SELECT strftime('%m/%Y', dt) as month, SUM(total) as total FROM (
            SELECT exp_date as dt, COALESCE(amount, 0) as total FROM expenses
            UNION ALL
            SELECT exp_date as dt, COALESCE(amount, 0) as total FROM other_expenses WHERE status='Paid'
        ) GROUP BY month ORDER BY MAX(dt) DESC LIMIT 6
    """).fetchall()
    
    top_clients = conn.execute("""
        SELECT party_name, COALESCE(SUM(total), 0) as total_sales
        FROM invoices
        WHERE party_name IS NOT NULL AND party_name != ''
        GROUP BY party_name
        ORDER BY total_sales DESC
        LIMIT 5
    """).fetchall()

    state_sales = conn.execute("""
        SELECT COALESCE(NULLIF(p.state, ''), p.address, 'Unknown') as state, COALESCE(SUM(i.total), 0) as total_sales
        FROM invoices i
        JOIN parties p ON i.party_id = p.id
        GROUP BY state
        ORDER BY total_sales DESC
    """).fetchall()

    city_sales = conn.execute("""
        SELECT COALESCE(NULLIF(p.state, ''), p.address, 'Unknown') as state, 
               COALESCE(NULLIF(p.city, ''), 'Unknown City') as city, 
               COALESCE(SUM(i.total), 0) as total_sales
        FROM invoices i
        JOIN parties p ON i.party_id = p.id
        GROUP BY state, city
        ORDER BY total_sales DESC
    """).fetchall()

    top_products = conn.execute("""
        SELECT description, COALESCE(SUM(qty), 0) as total_qty, COALESCE(SUM(amount), 0) as total_revenue
        FROM invoice_items
        WHERE description IS NOT NULL AND description != ''
        GROUP BY description
        ORDER BY total_revenue DESC
        LIMIT 5
    """).fetchall()
    
    conn.close()
    
    # Reverse monthly data to show chronologically left to right
    monthly_sales = list(reversed(monthly_sales))
    monthly_expenses = list(reversed(monthly_expenses))
    
    # Merge monthly sales and expenses to find the max month value across both
    all_monthly_vals = [m['total'] for m in monthly_sales] + [m['total'] for m in monthly_expenses]
    max_month_val = max(all_monthly_vals) if all_monthly_vals else 1
    max_client_val = max([c['total_sales'] for c in top_clients]) if top_clients else 1
    max_product_val = max([p['total_revenue'] for p in top_products]) if top_products else 1
    
    if max_month_val == 0: max_month_val = 1
    if max_client_val == 0: max_client_val = 1
    if max_product_val == 0: max_product_val = 1
    
    # Pre-process city data to a nested dictionary for JS easy consumption
    state_cities = {}
    for c in city_sales:
        s = c['state']
        if s not in state_cities:
            state_cities[s] = []
        state_cities[s].append({'city': c['city'], 'sales': c['total_sales']})
        
    return render_template('dashboard.html',
        total_sales=total_sales + total_other_incomes, total_purchases=total_purchases, total_expenses=total_expenses + total_other_expenses,
        unpaid_invoices=unpaid_invoices, profit=total_sales+total_other_incomes-total_purchases-total_expenses-total_other_expenses,
        monthly_sales=monthly_sales, monthly_expenses=monthly_expenses, top_clients=top_clients,
        state_sales=state_sales, state_cities=state_cities, top_products=top_products,
        max_month_val=max_month_val, max_client_val=max_client_val,
        max_product_val=max_product_val)

@app.route('/accounts_hub')
@login_required
def accounts_hub():
    return render_template('accounts_hub.html')

@app.route('/incomes_expenses')
@login_required
def incomes_expenses_hub():
    return render_template('incomes_expenses_hub.html')

@app.route('/incomes')
@login_required
def incomes():
    conn = get_db()
    all_incomes = conn.execute("SELECT * FROM incomes ORDER BY inc_date DESC, id DESC").fetchall()
    total_received = sum(i['amount'] for i in all_incomes if i['status'] == 'Received')
    conn.close()
    return render_template('incomes.html', incomes=all_incomes, total_received=total_received)

@app.route('/incomes/new', methods=['GET', 'POST'])
@login_required
def new_income():
    if request.method == 'POST':
        inc_date = request.form['inc_date']
        category = request.form['category']
        payer = request.form['payer']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"

        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO incomes 
                     (inc_date, category, payer, amount, description, status, payment_mode, created_by)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (inc_date, category, payer, amount, description, status, payment_mode, getattr(current_user, 'id', 1)))
        inc_id = c.lastrowid
        
        log_audit(conn, 'INSERT', 'incomes', inc_id, None, {
            'inc_date': inc_date, 'category': category, 'payer': payer, 'amount': amount,
            'description': description, 'status': status, 'payment_mode': payment_mode
        })
        
        conn.commit()
        conn.close()
        flash("Income recorded successfully!", "success")
        return redirect(url_for('dashboard')) if request.form.get('from_dashboard') else redirect(url_for('incomes'))
    
    return render_template('new_income.html', income=None)

@app.route('/incomes/edit/<int:inc_id>', methods=['GET', 'POST'])
@login_required
def edit_income(inc_id):
    conn = get_db()
    income = conn.execute("SELECT * FROM incomes WHERE id=?", (inc_id,)).fetchone()
    if not income:
        conn.close()
        flash("Income not found.", "danger")
        return redirect(url_for('incomes'))
        
    if request.method == 'POST':
        inc_date = request.form['inc_date']
        category = request.form['category']
        payer = request.form['payer']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"

        conn.execute('''UPDATE incomes SET
                        inc_date=?, category=?, payer=?, amount=?, description=?, status=?, payment_mode=?
                        WHERE id=?''',
                     (inc_date, category, payer, amount, description, status, payment_mode, inc_id))
        
        conn.commit()
        conn.close()
        flash("Income updated successfully!", "success")
        return redirect(url_for('incomes'))
    
    conn.close()
    return render_template('new_income.html', income=income)

@app.route('/incomes/delete/<int:inc_id>', methods=['POST'])
@login_required
def delete_income(inc_id):
    conn = get_db()
    income = conn.execute("SELECT * FROM incomes WHERE id=?", (inc_id,)).fetchone()
    if income:
        log_audit(conn, 'DELETE', 'incomes', inc_id, dict(income), None)
        conn.execute("DELETE FROM incomes WHERE id=?", (inc_id,))
        conn.commit()
        flash('Income deleted successfully.', 'success')
    conn.close()
    return redirect(url_for('incomes'))

@app.route('/other_expenses')
@login_required
def other_expenses():
    conn = get_db()
    all_other_expenses = conn.execute("SELECT * FROM other_expenses ORDER BY exp_date DESC, id DESC").fetchall()
    total_paid = sum(i['amount'] for i in all_other_expenses if i['status'] == 'Paid')
    conn.close()
    return render_template('other_expenses.html', other_expenses=all_other_expenses, total_paid=total_paid)

@app.route('/other_expenses/new', methods=['GET', 'POST'])
@login_required
def new_other_expense():
    if request.method == 'POST':
        exp_date = request.form['exp_date']
        category = request.form['category']
        payee = request.form['payee']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"

        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO other_expenses 
                     (exp_date, category, payee, amount, description, status, payment_mode, created_by)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (exp_date, category, payee, amount, description, status, payment_mode, getattr(current_user, 'id', 1)))
        exp_id = c.lastrowid
        
        log_audit(conn, 'INSERT', 'other_expenses', exp_id, None, {
            'exp_date': exp_date, 'category': category, 'amount': amount
        })
        conn.commit()
        conn.close()
        flash('Other expense recorded successfully.', 'success')
        return redirect(url_for('other_expenses'))
        
    today = datetime.today().strftime('%Y-%m-%d')
    return render_template('new_other_expense.html', today=today, expense=None)

@app.route('/other_expenses/edit/<int:exp_id>', methods=['GET', 'POST'])
@login_required
def edit_other_expense(exp_id):
    conn = get_db()
    expense = conn.execute("SELECT * FROM other_expenses WHERE id=?", (exp_id,)).fetchone()
    if not expense:
        conn.close()
        flash("Other Expense not found.", "danger")
        return redirect(url_for('other_expenses'))
        
    if request.method == 'POST':
        exp_date = request.form['exp_date']
        category = request.form['category']
        payee = request.form['payee']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"

        conn.execute('''UPDATE other_expenses SET
                        exp_date=?, category=?, payee=?, amount=?, description=?, status=?, payment_mode=?
                        WHERE id=?''',
                     (exp_date, category, payee, amount, description, status, payment_mode, exp_id))
        
        conn.commit()
        conn.close()
        flash("Other Expense updated successfully!", "success")
        return redirect(url_for('other_expenses'))
    
    conn.close()
    return render_template('new_other_expense.html', expense=expense, today=expense['exp_date'])

@app.route('/other_expenses/delete/<int:exp_id>', methods=['POST'])
@login_required
def delete_other_expense(exp_id):
    conn = get_db()
    expense = conn.execute("SELECT * FROM other_expenses WHERE id=?", (exp_id,)).fetchone()
    if expense:
        log_audit(conn, 'DELETE', 'other_expenses', exp_id, dict(expense), None)
        conn.execute("DELETE FROM other_expenses WHERE id=?", (exp_id,))
        conn.commit()
        flash('Other expense deleted successfully.', 'success')
    conn.close()
    return redirect(url_for('other_expenses'))


@app.route('/expenses')
@login_required
def expenses():
    conn = get_db()
    all_expenses = conn.execute("SELECT * FROM expenses ORDER BY exp_date DESC, id DESC").fetchall()
    
    total_paid = sum(e['amount'] for e in all_expenses if e['status'] == 'Paid')
    total_payable = sum(e['amount'] for e in all_expenses if e['status'] == 'Unpaid')
    conn.close()
    
    return render_template('expenses.html', expenses=all_expenses, total_paid=total_paid, total_payable=total_payable)

@app.route('/expenses/new', methods=['GET', 'POST'])
@login_required
def new_expense():
    if request.method == 'POST':
        exp_date = request.form['exp_date']
        category = request.form['category']
        payee = request.form['payee']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"

        ref_no = request.form.get('ref_no', '')
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO expenses 
                     (exp_date, category, payee, amount, description, status, payment_mode, created_by, ref_no)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (exp_date, category, payee, amount, description, status, payment_mode, getattr(current_user, 'id', 1), ref_no))
        exp_id = c.lastrowid
        
        log_audit(conn, 'INSERT', 'expenses', exp_id, None, {
            'exp_date': exp_date, 'category': category, 'payee': payee, 'amount': amount,
            'description': description, 'status': status, 'payment_mode': payment_mode, 'ref_no': ref_no
        })
        
        # If paid, add a ledger entry? Expenses usually decrease cash/bank.
        # Let's keep it simple for now or we can add ledger logic if they want ledger integration for expenses.
        
        conn.commit()
        conn.close()
        flash("Expense recorded successfully!", "success")
        return redirect(url_for('expenses'))
    
    conn = get_db()
    last = conn.execute("SELECT ref_no FROM expenses WHERE ref_no LIKE 'EXP-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'EXP-0001'
    if last and last['ref_no']:
        try:
            n = int(last['ref_no'].split('-')[1]) + 1
            next_no = f'EXP-{n:04d}'
        except: pass
    conn.close()
    
    return render_template('expense_form.html', expense=None, today=date.today().isoformat(), next_no=next_no)

@app.route('/expenses/edit/<int:exp_id>', methods=['GET', 'POST'])
@login_required
def edit_expense(exp_id):
    conn = get_db()
    expense = conn.execute("SELECT * FROM expenses WHERE id=?", (exp_id,)).fetchone()
    if not expense:
        conn.close()
        flash("Expense not found.", "danger")
        return redirect(url_for('expenses'))
        
    if request.method == 'POST':
        exp_date = request.form['exp_date']
        category = request.form['category']
        payee = request.form['payee']
        amount = to_float(request.form.get('amount', 0))
        description = request.form.get('description', '')
        status = request.form['status']
        payment_mode = request.form.get('payment_mode', '')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"
        ref_no = request.form.get('ref_no', '')

        c = conn.cursor()
        c.execute('''UPDATE expenses SET 
                     exp_date=?, category=?, payee=?, amount=?, description=?, status=?, payment_mode=?, ref_no=?
                     WHERE id=?''',
                  (exp_date, category, payee, amount, description, status, payment_mode, ref_no, exp_id))
        
        log_audit(conn, 'UPDATE', 'expenses', exp_id, expense, {
            'exp_date': exp_date, 'category': category, 'payee': payee, 'amount': amount,
            'description': description, 'status': status, 'payment_mode': payment_mode, 'ref_no': ref_no
        })
        
        conn.commit()
        conn.close()
        flash("Expense updated successfully!", "success")
        return redirect(url_for('expenses'))
        
    conn.close()
    return render_template('expense_form.html', expense=expense)

@app.route('/expenses/delete/<int:exp_id>', methods=['POST'])
@login_required
def delete_expense(exp_id):
    conn = get_db()
    expense = conn.execute("SELECT * FROM expenses WHERE id=?", (exp_id,)).fetchone()
    if expense:
        conn.execute("DELETE FROM expenses WHERE id=?", (exp_id,))
        log_audit(conn, 'DELETE', 'expenses', exp_id, expense, None)
        conn.commit()
    conn.close()
    flash("Expense deleted.", "info")
    return redirect(url_for('expenses'))

@app.route('/gst')
@login_required
def gst():
    conn = get_db()
    
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    active_tab = request.args.get('active_tab', 'gstr1')
    
    sales_query = "SELECT * FROM invoices WHERE 1=1"
    purchases_query = "SELECT * FROM purchases WHERE 1=1"
    
    sales_params = []
    purchases_params = []
    
    if from_date:
        sales_query += " AND invoice_date >= ?"
        purchases_query += " AND bill_date >= ?"
        sales_params.append(from_date)
        purchases_params.append(from_date)
        
    if to_date:
        sales_query += " AND invoice_date <= ?"
        purchases_query += " AND bill_date <= ?"
        sales_params.append(to_date)
        purchases_params.append(to_date)
        
    sales_query += " ORDER BY invoice_date DESC"
    purchases_query += " ORDER BY bill_date DESC"
    
    # Fetch Sales for GSTR-1
    sales = conn.execute(sales_query, sales_params).fetchall()
    # Fetch Purchases for GSTR-2
    purchases = conn.execute(purchases_query, purchases_params).fetchall()
    
    # Calculate Summaries for GSTR-3B
    total_sales_val = sum(s['subtotal'] for s in sales)
    sales_cgst = sum(s['cgst_amount'] if s['cgst_amount'] else 0 for s in sales)
    sales_sgst = sum(s['sgst_amount'] if s['sgst_amount'] else 0 for s in sales)
    sales_igst = sum(s['igst_amount'] if s['igst_amount'] else 0 for s in sales)
    
    total_purchase_val = sum(p['amount'] for p in purchases)
    purchase_cgst = sum(p['cgst_amount'] if p['cgst_amount'] else 0 for p in purchases)
    purchase_sgst = sum(p['sgst_amount'] if p['sgst_amount'] else 0 for p in purchases)
    purchase_igst = sum(p['igst_amount'] if p['igst_amount'] else 0 for p in purchases)
    
    # Setoff logic based on Section 49 of CGST Act
    igst_liability = sales_igst
    cgst_liability = sales_cgst
    sgst_liability = sales_sgst
    
    igst_itc = purchase_igst
    cgst_itc = purchase_cgst
    sgst_itc = purchase_sgst
    
    # 1. Utilize IGST ITC
    if igst_itc >= igst_liability:
        igst_itc -= igst_liability
        igst_liability = 0
        if igst_itc >= cgst_liability:
            igst_itc -= cgst_liability
            cgst_liability = 0
            if igst_itc >= sgst_liability:
                igst_itc -= sgst_liability
                sgst_liability = 0
            else:
                sgst_liability -= igst_itc
                igst_itc = 0
        else:
            cgst_liability -= igst_itc
            igst_itc = 0
    else:
        igst_liability -= igst_itc
        igst_itc = 0
        
    # 2. Utilize CGST ITC
    if cgst_itc >= cgst_liability:
        cgst_itc -= cgst_liability
        cgst_liability = 0
        if cgst_itc >= igst_liability:
            cgst_itc -= igst_liability
            igst_liability = 0
        else:
            igst_liability -= cgst_itc
            cgst_itc = 0
    else:
        cgst_liability -= cgst_itc
        cgst_itc = 0
        
    # 3. Utilize SGST ITC
    if sgst_itc >= sgst_liability:
        sgst_itc -= sgst_liability
        sgst_liability = 0
        if sgst_itc >= igst_liability:
            sgst_itc -= igst_liability
            igst_liability = 0
        else:
            igst_liability -= sgst_itc
            sgst_itc = 0
    else:
        sgst_liability -= sgst_itc
        sgst_itc = 0
        
    net_igst = igst_liability - igst_itc
    net_cgst = cgst_liability - cgst_itc
    net_sgst = sgst_liability - sgst_itc
    
    conn.close()
    
    if request.args.get('export') == 'json':
        import json
        from datetime import datetime
        import os
        
        b2b = []
        b2cs = []
        if active_tab == 'gstr1':
            for s_row in sales:
                s = dict(s_row)
                inv_data = {
                    "inum": s['invoice_no'],
                    "idt": s['invoice_date'],
                    "val": round(s['total'], 2),
                    "pos": s.get('place_of_supply') or '27',
                    "rchrg": "Y" if s.get('reverse_charge') else "N",
                    "inv_typ": "R",
                    "itms": [
                        {
                            "num": 1,
                            "itm_det": {
                                "txval": round(s['subtotal'] - s.get('discount_amount', 0), 2),
                                "rt": round((s.get('cgst_pct') or 0) + (s.get('sgst_pct') or 0) + (s.get('igst_pct') or 0), 2),
                                "camt": round(s['cgst_amount'] or 0, 2),
                                "samt": round(s['sgst_amount'] or 0, 2),
                                "iamt": round(s['igst_amount'] or 0, 2),
                            }
                        }
                    ]
                }
                if s.get('reverse_charge') or (s['party_name'] and s['party_name'].lower() != 'cash'):
                    b2b.append({"ctin": "00XXXXXXXXXX", "inv": [inv_data]})
                else:
                    b2cs.append(inv_data)
        elif active_tab == 'gstr2':
            pass # Implement JSON for GSTR2 if needed
        elif active_tab == 'gstr3b':
            pass # Implement JSON for GSTR3B if needed
                
        gstn_json = {
            "gstin": "YOUR_GSTIN_HERE",
            "fp": datetime.now().strftime('%m%Y'),
            "gt": round(total_sales_val, 2),
            "cur_gt": round(total_sales_val, 2),
            "b2b": b2b,
            "b2cs": b2cs,
            "itc_summary": {
                "igst_itc": round(purchase_igst, 2),
                "cgst_itc": round(purchase_cgst, 2),
                "sgst_itc": round(purchase_sgst, 2)
            }
        }
        
        try:
            user_profile = os.environ.get('USERPROFILE') or os.path.expanduser('~')
            downloads_dir = os.path.join(user_profile, 'Downloads')
            if not os.path.exists(downloads_dir):
                downloads_dir = os.getcwd()
                
            if active_tab == 'gstr1':
                filename = f"GSTR1_Return_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            elif active_tab == 'gstr2':
                filename = f"GSTR2_Return_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            else:
                filename = f"GSTR3B_Return_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                
            file_path = os.path.join(downloads_dir, filename)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(gstn_json, f, indent=4)
                
            flash(f"JSON payload successfully generated for active tab: {file_path}", "success")
        except Exception as e:
            flash(f"Failed to generate JSON: {str(e)}", "danger")
            
        return redirect(url_for('gst', from_date=from_date, to_date=to_date, active_tab=active_tab))

    elif request.args.get('export') == 'excel':
        import os
        from datetime import datetime
        
        try:
            user_profile = os.environ.get('USERPROFILE') or os.path.expanduser('~')
            downloads_dir = os.path.join(user_profile, 'Downloads')
            if not os.path.exists(downloads_dir):
                downloads_dir = os.getcwd()
                
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            if active_tab == 'gstr1':
                file1 = os.path.join(downloads_dir, f"GSTR1_Sales_{timestamp}.csv")
                with open(file1, 'w', encoding='utf-8', newline='') as f1:
                    cw = csv.writer(f1)
                    cw.writerow(['Date', 'Invoice No', 'Customer Name', 'Taxable Value', 'CGST', 'SGST', 'IGST', 'Total Invoice Val'])
                    for s in sales:
                        cw.writerow([
                            s['invoice_date'], s['invoice_no'], s['party_name'],
                            round(s['subtotal'], 2), round(s['cgst_amount'] or 0, 2),
                            round(s['sgst_amount'] or 0, 2), round(s['igst_amount'] or 0, 2),
                            round(s['total'], 2)
                        ])
                flash(f"Successfully exported GSTR-1 to your Downloads folder.", "success")
                
            elif active_tab == 'gstr2':
                file2 = os.path.join(downloads_dir, f"GSTR2_Purchases_{timestamp}.csv")
                with open(file2, 'w', encoding='utf-8', newline='') as f2:
                    cw = csv.writer(f2)
                    cw.writerow(['Date', 'Bill No', 'Supplier Name', 'Taxable Value', 'CGST Paid', 'SGST Paid', 'IGST Paid', 'Total Bill Val'])
                    for p in purchases:
                        cw.writerow([
                            p['bill_date'], p['bill_no'], p['party_name'],
                            round(p['amount'], 2), round(p['cgst_amount'] or 0, 2),
                            round(p['sgst_amount'] or 0, 2), round(p['igst_amount'] or 0, 2),
                            round(p['total'], 2)
                        ])
                flash(f"Successfully exported GSTR-2B to your Downloads folder.", "success")
                
            else: # gstr3b
                file3 = os.path.join(downloads_dir, f"GSTR3B_Summary_{timestamp}.csv")
                with open(file3, 'w', encoding='utf-8', newline='') as f3:
                    cw = csv.writer(f3)
                    cw.writerow(['Type', 'Taxable Value', 'CGST', 'SGST', 'IGST'])
                    cw.writerow(['Total Sales', round(total_sales_val, 2), round(sales_cgst, 2), round(sales_sgst, 2), round(sales_igst, 2)])
                    cw.writerow(['Total Purchases', round(total_purchase_val, 2), round(purchase_cgst, 2), round(purchase_sgst, 2), round(purchase_igst, 2)])
                    cw.writerow(['Net Payable / (Refundable)', '', round(net_cgst, 2), round(net_sgst, 2), round(net_igst, 2)])
                flash(f"Successfully exported GSTR-3B to your Downloads folder.", "success")
                
        except Exception as e:
            flash(f"Failed to export: {str(e)}", "danger")
            
        return redirect(url_for('gst', from_date=from_date, to_date=to_date, active_tab=active_tab))

    return render_template('gst.html', 
                           sales=sales, purchases=purchases,
                           total_sales_val=total_sales_val, sales_cgst=sales_cgst, 
                           sales_sgst=sales_sgst, sales_igst=sales_igst,
                           total_purchase_val=total_purchase_val, purchase_cgst=purchase_cgst,
                           purchase_sgst=purchase_sgst, purchase_igst=purchase_igst,
                           net_cgst=net_cgst, net_sgst=net_sgst, net_igst=net_igst,
                           from_date=from_date, to_date=to_date, active_tab=active_tab)

# â”€â”€ Parties â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/parties')
@login_required
def parties():
    return render_template('parties.html')

@app.route('/party_list')
@login_required
def party_list():
    conn = get_db()
    all_parties = conn.execute("SELECT * FROM parties ORDER BY name").fetchall()
    conn.close()
    return render_template('party_list.html', parties=all_parties)

@app.route('/parties/add', methods=['POST'])
@login_required
def add_party():
    conn = get_db()
    conn.execute("INSERT INTO parties (name,type,phone,address,state,city,gstin,opening_balance) VALUES (?,?,?,?,?,?,?,?)",
        (request.form['name'], request.form['type'], request.form.get('phone',''),
         request.form.get('address',''), request.form.get('state',''), request.form.get('city',''),
         request.form.get('gstin',''), to_float(request.form.get('opening_balance',0))))
    conn.commit()
    conn.close()
    flash('Party added successfully.', 'success')
    return redirect(url_for('parties'))

@app.route('/parties/edit/<int:party_id>', methods=['GET', 'POST'])
@login_required
def edit_party(party_id):
    conn = get_db()
    party = conn.execute("SELECT * FROM parties WHERE id=?", (party_id,)).fetchone()
    if request.method == 'POST':
        conn.execute("UPDATE parties SET name=?, type=?, phone=?, address=?, state=?, city=?, gstin=?, opening_balance=? WHERE id=?",
            (request.form['name'], request.form['type'], request.form.get('phone',''),
             request.form.get('address',''), request.form.get('state',''), request.form.get('city',''),
             request.form.get('gstin',''), to_float(request.form.get('opening_balance',0)), party_id))
        conn.commit()
        conn.close()
        flash('Party updated successfully.', 'success')
        return redirect(url_for('party_list'))
    conn.close()
    return render_template('parties.html', party=party)

@app.route('/parties/delete/<int:party_id>', methods=['GET', 'POST'])
@login_required
def delete_party(party_id):
    conn = get_db()
    conn.execute("DELETE FROM parties WHERE id=?", (party_id,))
    conn.commit()
    conn.close()
    flash('Party deleted.', 'success')
    return redirect(url_for('party_list'))

# â”€â”€ Invoices â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/invoices')
@login_required
def invoices():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    # Fetch invoices with calculated paid amounts
    all_inv_rows = conn.execute("""
        SELECT i.*, 
          COALESCE((SELECT SUM(amount) FROM receipts WHERE LOWER(TRIM(reference_no)) = LOWER(TRIM(i.invoice_no)) AND type='receipt'), 0) as paid_amount
        FROM invoices i ORDER BY invoice_date DESC
    """).fetchall()
    
    # Convert rows to mutable dicts to dynamically set status
    all_inv = []
    for row in all_inv_rows:
        inv = dict(row)
        if inv['status'] != 'paid': # If not manually marked paid
            if inv['paid_amount'] >= inv['total']:
                inv['status'] = 'paid'
            else:
                inv['status'] = 'unpaid'
        all_inv.append(inv)
        
    all_rects = conn.execute("SELECT * FROM receipts WHERE type='receipt' ORDER BY receipt_date DESC, id DESC").fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('customer','both') ORDER BY name").fetchall()
    
    last_receipt = conn.execute("SELECT reference_no FROM receipts WHERE type='receipt' AND reference_no LIKE 'REC-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'REC-0001'
    if last_receipt and last_receipt['reference_no']:
        try:
            n = int(last_receipt['reference_no'].split('-')[1]) + 1
            next_no = f'REC-{n:04d}'
        except: pass
        
    conn.close()
    return render_template('invoices.html', invoices=all_inv, receipts=all_rects, parties=parties, next_no=next_no)

@app.route('/invoices/new', methods=['GET','POST'])
@login_required
def new_invoice():
    conn = get_db()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('customer','both') ORDER BY name").fetchall()
    products_list = conn.execute("""
        SELECT p.*,
        COALESCE(p.opening_stock, 0) +
        (SELECT COALESCE(SUM(qty), 0) FROM purchase_items WHERE description = p.name) -
        (SELECT COALESCE(SUM(qty), 0) FROM invoice_items WHERE description = p.name) AS current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    if request.method == 'POST':
        items = []
        subtotal = 0.0
        row_indices = set()
        for k in request.form:
            if k.startswith('desc_'):
                try: row_indices.add(int(k.split('_')[1]))
                except ValueError: pass
        for i in sorted(row_indices):
            desc = request.form.get(f'desc_{i}', '').strip()
            if desc:
                qty = to_float(request.form.get(f'qty_{i}'), 1.0)
                rate = to_float(request.form.get(f'rate_{i}'), 0.0)
                amt = qty * rate
                subtotal += amt
                items.append((desc, qty, rate, amt))
            
        discount_pct = to_float(request.form.get('discount_pct', 0))
        discount_amount = subtotal * (discount_pct / 100)
        taxable_amount = subtotal - discount_amount
        
        cgst_pct = to_float(request.form.get('cgst_pct', 0))
        sgst_pct = to_float(request.form.get('sgst_pct', 0))
        igst_pct = to_float(request.form.get('igst_pct', 0))
        
        cgst_amount = taxable_amount * (cgst_pct / 100)
        sgst_amount = taxable_amount * (sgst_pct / 100)
        igst_amount = taxable_amount * (igst_pct / 100)
        
        tax_amt = cgst_amount + sgst_amount + igst_amount
        total = taxable_amount + tax_amt
        
        inv_no = request.form.get('invoice_no', '').strip()
        party_id = request.form.get('party_id') or None
        party_name = request.form.get('party_name', '').strip() or 'Direct Customer'
        inv_date = request.form.get('invoice_date', date.today().isoformat())
        notes = request.form.get('notes','')
        c = conn.cursor()
        place_of_supply = request.form.get('place_of_supply', '')
        reverse_charge = 1 if request.form.get('reverse_charge') == 'on' else 0

        c.execute("""INSERT INTO invoices (invoice_no,invoice_date,party_id,party_name,subtotal,discount_pct,discount_amount,cgst_pct,sgst_pct,igst_pct,cgst_amount,sgst_amount,igst_amount,tax_amount,total,notes,place_of_supply,reverse_charge,created_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (inv_no, inv_date, party_id, party_name, subtotal, discount_pct, discount_amount, cgst_pct, sgst_pct, igst_pct, cgst_amount, sgst_amount, igst_amount, tax_amt, total, notes, place_of_supply, reverse_charge, current_user.id))
        inv_id = c.lastrowid
        for it in items:
            c.execute("INSERT INTO invoice_items (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
                      (inv_id, it[0], it[1], it[2], it[3]))
                      
        log_audit(conn, 'INSERT', 'invoices', inv_id, None, {
            'invoice_no': inv_no, 'party_name': party_name, 'total': total, 'tax_amount': tax_amt
        })
        
        # Ledger entry
        c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (inv_date,'invoice',inv_id,inv_no,party_id,party_name,f'Invoice {inv_no}',total,0,current_user.id))
                  
        # Instant Payment Handling
        payment_amount = to_float(request.form.get('payment_amount', 0) or 0)
        if payment_amount > 0:
            payment_mode = request.form.get('payment_mode', 'Cash')
            bank_source = request.form.get('bank_source', '').strip()
            if payment_mode == 'Bank' and bank_source:
                payment_mode = f"Bank - {bank_source}"
            
            c.execute("""INSERT INTO receipts (receipt_date,type,party_id,party_name,amount,payment_mode,reference_no,notes,created_by)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (inv_date, 'receipt', party_id, party_name, payment_amount, payment_mode,
                       f'{inv_no}', 'Instant payment during invoice creation', current_user.id))
            r_id = c.lastrowid
            c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (inv_date, 'receipt', r_id, inv_no, party_id, party_name, f'Receipt ({payment_mode}) against {inv_no}', 0, payment_amount, current_user.id))

        conn.commit()
        conn.close()
        flash('Invoice created successfully.', 'success')
        return redirect(url_for('invoices'))
    # Auto-generate invoice number
    last = conn.execute("SELECT invoice_no FROM invoices ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'INV-0001'
    if last:
        try:
            n = int(last['invoice_no'].split('-')[1]) + 1
            next_no = f'INV-{n:04d}'
        except: pass
    conn.close()
    return render_template('new_invoice.html', parties=parties, next_no=next_no, today=date.today().isoformat(), products=products_list)

@app.route('/invoices/<int:inv_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_invoice(inv_id):
    conn = get_db()
    invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    if not invoice:
        conn.close()
        flash("Invoice not found.", "error")
        return redirect(url_for('invoices'))
        
    if request.method == 'POST':
        items = []
        subtotal = 0.0
        row_indices = set()
        for k in request.form:
            if k.startswith('desc_'):
                try: row_indices.add(int(k.split('_')[1]))
                except ValueError: pass
        for i in sorted(row_indices):
            desc = request.form.get(f'desc_{i}', '').strip()
            if desc:
                qty = to_float(request.form.get(f'qty_{i}'), 1.0)
                rate = to_float(request.form.get(f'rate_{i}'), 0.0)
                amt = qty * rate
                subtotal += amt
                items.append((desc, qty, rate, amt))
            
        discount_pct = to_float(request.form.get('discount_pct', 0))
        discount_amount = subtotal * (discount_pct / 100)
        taxable_amount = subtotal - discount_amount
        
        cgst_pct = to_float(request.form.get('cgst_pct', 0))
        sgst_pct = to_float(request.form.get('sgst_pct', 0))
        igst_pct = to_float(request.form.get('igst_pct', 0))
        
        cgst_amount = taxable_amount * (cgst_pct / 100)
        sgst_amount = taxable_amount * (sgst_pct / 100)
        igst_amount = taxable_amount * (igst_pct / 100)
        
        tax_amt = cgst_amount + sgst_amount + igst_amount
        total = taxable_amount + tax_amt
        
        inv_no = request.form.get('invoice_no', '').strip()
        party_id = request.form.get('party_id') or None
        party_name = request.form.get('party_name', '').strip() or 'Direct Customer'
        inv_date = request.form.get('invoice_date', date.today().isoformat())
        notes = request.form.get('notes','')
        c = conn.cursor()
        place_of_supply = request.form.get('place_of_supply', '')
        reverse_charge = 1 if request.form.get('reverse_charge') == 'on' else 0
        
        c.execute("""UPDATE invoices SET invoice_no=?, invoice_date=?, party_id=?, party_name=?,
                     subtotal=?, discount_pct=?, discount_amount=?, cgst_pct=?, sgst_pct=?, igst_pct=?,
                     cgst_amount=?, sgst_amount=?, igst_amount=?, tax_amount=?, total=?, notes=?, place_of_supply=?, reverse_charge=? WHERE id=?""",
                  (inv_no, inv_date, party_id, party_name, subtotal, discount_pct, discount_amount, cgst_pct, sgst_pct, igst_pct, cgst_amount, sgst_amount, igst_amount, tax_amt, total, notes, place_of_supply, reverse_charge, inv_id))
        
        c.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
        for it in items:
            c.execute("INSERT INTO invoice_items (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
                      (inv_id, it[0], it[1], it[2], it[3]))
                      
        c.execute("""UPDATE ledger SET entry_date=?, ref_no=?, party_id=?, party_name=?, description=?, debit=?
                     WHERE ref_type='invoice' AND ref_id=?""",
                  (inv_date, inv_no, party_id, party_name, f'Invoice {inv_no}', total, inv_id))
                  
        log_audit(conn, 'UPDATE', 'invoices', inv_id, invoice, {
            'invoice_no': inv_no, 'party_name': party_name, 'total': total, 'tax_amount': tax_amt
        })
                  
        conn.commit()
        conn.close()
        flash('Invoice updated successfully.', 'success')
        return redirect(url_for('invoices'))
        
    items = conn.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv_id,)).fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('customer','both') ORDER BY name").fetchall()
    products_list = conn.execute("""
        SELECT p.*,
        COALESCE(p.opening_stock, 0) +
        (SELECT COALESCE(SUM(qty), 0) FROM purchase_items WHERE description = p.name) -
        (SELECT COALESCE(SUM(qty), 0) FROM invoice_items WHERE description = p.name) AS current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    
    items_dicts = [dict(it) for it in items]
    conn.close()
    return render_template('new_invoice.html', invoice=invoice, items=items_dicts, parties=parties, today=date.today().isoformat(), products=products_list)

@app.route('/invoices/<int:inv_id>')
@login_required
def view_invoice(inv_id):
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    items = conn.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv_id,)).fetchall()
    comp = conn.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
    party = conn.execute("SELECT * FROM parties WHERE id=?", (inv['party_id'],)).fetchone() if inv['party_id'] else None
    conn.close()
    return render_template('view_invoice.html', invoice=inv, items=items, comp=comp, party=party)

@app.route('/invoices/<int:inv_id>/mark_paid', methods=['POST'])
@login_required
def mark_paid(inv_id):
    conn = get_db()
    conn.execute("UPDATE invoices SET status='paid' WHERE id=?", (inv_id,))
    conn.commit()
    conn.close()
    flash('Invoice marked as paid.', 'success')
    return redirect(url_for('invoices'))

@app.route('/invoices/<int:inv_id>/delete', methods=['POST'])
@login_required
def delete_invoice(inv_id):
    conn = get_db()
    c = conn.cursor()
    invoice = c.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    if invoice:
        c.execute("DELETE FROM ledger WHERE ref_type='invoice' AND ref_id=?", (inv_id,))
        c.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
        c.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
        log_audit(conn, 'DELETE', 'invoices', inv_id, invoice, None)
    conn.commit()
    conn.close()
    flash('Invoice deleted successfully.', 'success')
    return redirect(url_for('invoices'))

# â”€â”€ Purchases â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/purchases')
@login_required
def purchases():
    conn = get_db()
    all_pur = conn.execute("SELECT * FROM purchases ORDER BY bill_date DESC").fetchall()
    all_pmts = conn.execute("SELECT * FROM receipts WHERE type='payment' ORDER BY receipt_date DESC, id DESC").fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('supplier','both') ORDER BY name").fetchall()
    
    last_payment = conn.execute("SELECT reference_no FROM receipts WHERE type='payment' AND reference_no LIKE 'PAY-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'PAY-0001'
    if last_payment and last_payment['reference_no']:
        try:
            n = int(last_payment['reference_no'].split('-')[1]) + 1
            next_no = f'PAY-{n:04d}'
        except: pass
        
    conn.close()
    return render_template('purchases.html', purchases=all_pur, payments=all_pmts, parties=parties, next_no=next_no)

@app.route('/purchases/new', methods=['GET','POST'])
@login_required
def new_purchase():
    conn = get_db()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('supplier','both') ORDER BY name").fetchall()
    products_list = conn.execute("""
        SELECT p.*,
        COALESCE(p.opening_stock, 0) +
        (SELECT COALESCE(SUM(qty), 0) FROM purchase_items WHERE description = p.name) -
        (SELECT COALESCE(SUM(qty), 0) FROM invoice_items WHERE description = p.name) AS current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    if request.method == 'POST':
        items = []
        subtotal = 0.0
        row_indices = set()
        for k in request.form:
            if k.startswith('desc_'):
                try: row_indices.add(int(k.split('_')[1]))
                except ValueError: pass
        for i in sorted(row_indices):
            desc = request.form.get(f'desc_{i}', '').strip()
            if desc:
                qty = to_float(request.form.get(f'qty_{i}'), 1.0)
                rate = to_float(request.form.get(f'rate_{i}'), 0.0)
                amt = qty * rate
                subtotal += amt
                items.append((desc, qty, rate, amt))
            
        discount_pct = to_float(request.form.get('discount_pct', 0))
        discount_amount = subtotal * (discount_pct / 100)
        taxable_amount = subtotal - discount_amount
        
        cgst_pct = to_float(request.form.get('cgst_pct', 0))
        sgst_pct = to_float(request.form.get('sgst_pct', 0))
        igst_pct = to_float(request.form.get('igst_pct', 0))
        
        cgst_amount = taxable_amount * (cgst_pct / 100)
        sgst_amount = taxable_amount * (sgst_pct / 100)
        igst_amount = taxable_amount * (igst_pct / 100)
        
        tax_amt = cgst_amount + sgst_amount + igst_amount
        total = taxable_amount + tax_amt
        
        bill_no = request.form.get('bill_no', '')
        party_id = request.form.get('party_id') or None
        party_name = request.form.get('party_name', '').strip() or 'Direct Customer'
        bill_date = request.form['bill_date']
        category = request.form.get('category', 'Inventory / Stock')
        notes = request.form.get('notes','')
        place_of_supply = request.form.get('place_of_supply', '')
        reverse_charge = 1 if request.form.get('reverse_charge') == 'on' else 0
        itc_eligibility = request.form.get('itc_eligibility', 'Eligible')
        c = conn.cursor()
        c.execute("""INSERT INTO purchases (bill_no,bill_date,party_id,party_name,category,description,amount,discount_pct,discount_amount,cgst_pct,sgst_pct,igst_pct,cgst_amount,sgst_amount,igst_amount,tax_amount,total,notes,place_of_supply,reverse_charge,itc_eligibility,created_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (bill_no, bill_date, party_id, party_name, category, "Stock Purchase", subtotal, discount_pct, discount_amount, cgst_pct, sgst_pct, igst_pct, cgst_amount, sgst_amount, igst_amount, tax_amt, total, notes, place_of_supply, reverse_charge, itc_eligibility, current_user.id))
        pur_id = c.lastrowid
        for it in items:
            c.execute("INSERT INTO purchase_items (purchase_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
                      (pur_id, it[0], it[1], it[2], it[3]))
                      
        log_audit(conn, 'INSERT', 'purchases', pur_id, None, {
            'bill_no': bill_no, 'party_name': party_name, 'total': total, 'category': category
        })
        
        # Ledger entry
        c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (bill_date,'purchase',pur_id,bill_no,party_id,party_name,f'Purchase {bill_no}',0,total,current_user.id))

        # Instant Payment Handling
        payment_amount = to_float(request.form.get('payment_amount', 0) or 0)
        if payment_amount > 0:
            payment_mode = request.form.get('payment_mode', 'Cash')
            bank_source = request.form.get('bank_source', '').strip()
            if payment_mode == 'Bank' and bank_source:
                payment_mode = f"Bank - {bank_source}"
                
            c.execute("""INSERT INTO receipts (receipt_date,type,party_id,party_name,amount,payment_mode,reference_no,notes,created_by)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (bill_date, 'payment', party_id, party_name, payment_amount, payment_mode,
                       f'{bill_no}', 'Instant payment during purchase creation', current_user.id))
            r_id = c.lastrowid
            c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (bill_date, 'payment', r_id, bill_no, party_id, party_name, f'Payment ({payment_mode}) against {bill_no}', payment_amount, 0, current_user.id))

        conn.commit()
        conn.close()
        flash('Purchase recorded successfully.', 'success')
        return redirect(url_for('purchases'))
    
    last = conn.execute("SELECT bill_no FROM purchases WHERE bill_no LIKE 'PUR-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'PUR-0001'
    if last and last['bill_no']:
        try:
            n = int(last['bill_no'].split('-')[1]) + 1
            next_no = f'PUR-{n:04d}'
        except: pass
        
    conn.close()
    return render_template('new_purchase.html', parties=parties, today=date.today().isoformat(), products=products_list, next_no=next_no)

@app.route('/purchases/<int:pur_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_purchase(pur_id):
    conn = get_db()
    purchase = conn.execute("SELECT * FROM purchases WHERE id=?", (pur_id,)).fetchone()
    if not purchase:
        conn.close()
        flash("Purchase not found.", "error")
        return redirect(url_for('purchases'))
        
    if request.method == 'POST':
        items = []
        subtotal = 0.0
        row_indices = set()
        for k in request.form:
            if k.startswith('desc_'):
                try: row_indices.add(int(k.split('_')[1]))
                except ValueError: pass
        for i in sorted(row_indices):
            desc = request.form.get(f'desc_{i}', '').strip()
            if desc:
                qty = to_float(request.form.get(f'qty_{i}'), 1.0)
                rate = to_float(request.form.get(f'rate_{i}'), 0.0)
                amt = qty * rate
                subtotal += amt
                items.append((desc, qty, rate, amt))
            
        discount_pct = to_float(request.form.get('discount_pct', 0))
        discount_amount = subtotal * (discount_pct / 100)
        taxable_amount = subtotal - discount_amount
        
        cgst_pct = to_float(request.form.get('cgst_pct', 0))
        sgst_pct = to_float(request.form.get('sgst_pct', 0))
        igst_pct = to_float(request.form.get('igst_pct', 0))
        
        cgst_amount = taxable_amount * (cgst_pct / 100)
        sgst_amount = taxable_amount * (sgst_pct / 100)
        igst_amount = taxable_amount * (igst_pct / 100)
        
        tax_amt = cgst_amount + sgst_amount + igst_amount
        total = taxable_amount + tax_amt
        
        bill_no = request.form.get('bill_no', '')
        party_id = request.form.get('party_id') or None
        party_name = request.form.get('party_name', '').strip() or 'Direct Customer'
        bill_date = request.form['bill_date']
        category = request.form.get('category', 'Inventory / Stock')
        notes = request.form.get('notes','')
        place_of_supply = request.form.get('place_of_supply', '')
        reverse_charge = 1 if request.form.get('reverse_charge') == 'on' else 0
        itc_eligibility = request.form.get('itc_eligibility', 'Eligible')
        c = conn.cursor()
        
        c.execute("""UPDATE purchases SET bill_no=?, bill_date=?, party_id=?, party_name=?,
                     category=?, amount=?, discount_pct=?, discount_amount=?, cgst_pct=?, sgst_pct=?, igst_pct=?,
                     cgst_amount=?, sgst_amount=?, igst_amount=?, tax_amount=?, total=?, notes=?, place_of_supply=?, reverse_charge=?, itc_eligibility=? WHERE id=?""",
                  (bill_no, bill_date, party_id, party_name, category, subtotal, discount_pct, discount_amount, cgst_pct, sgst_pct, igst_pct, cgst_amount, sgst_amount, igst_amount, tax_amt, total, notes, place_of_supply, reverse_charge, itc_eligibility, pur_id))
        
        c.execute("DELETE FROM purchase_items WHERE purchase_id=?", (pur_id,))
        for it in items:
            c.execute("INSERT INTO purchase_items (purchase_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
                      (pur_id, it[0], it[1], it[2], it[3]))
                      
        c.execute("""UPDATE ledger SET entry_date=?, ref_no=?, party_id=?, party_name=?, description=?, credit=?
                     WHERE ref_type='purchase' AND ref_id=?""",
                  (bill_date, bill_no, party_id, party_name, f'Purchase {bill_no}', total, pur_id))
                  
        log_audit(conn, 'UPDATE', 'purchases', pur_id, purchase, {
            'bill_no': bill_no, 'party_name': party_name, 'total': total, 'category': category
        })
                  
        conn.commit()
        conn.close()
        flash('Purchase updated successfully.', 'success')
        return redirect(url_for('purchases'))
        
    items = conn.execute("SELECT * FROM purchase_items WHERE purchase_id=?", (pur_id,)).fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('supplier','both') ORDER BY name").fetchall()
    products_list = conn.execute("""
        SELECT p.*,
        COALESCE(p.opening_stock, 0) +
        (SELECT COALESCE(SUM(qty), 0) FROM purchase_items WHERE description = p.name) -
        (SELECT COALESCE(SUM(qty), 0) FROM invoice_items WHERE description = p.name) AS current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    
    items_dicts = [dict(it) for it in items]
    conn.close()
    return render_template('new_purchase.html', purchase=purchase, items=items_dicts, parties=parties, today=date.today().isoformat(), products=products_list)

@app.route('/purchases/<int:pur_id>')
@login_required
def view_purchase(pur_id):
    conn = get_db()
    purchase = conn.execute("SELECT * FROM purchases WHERE id=?", (pur_id,)).fetchone()
    items = conn.execute("SELECT * FROM purchase_items WHERE purchase_id=?", (pur_id,)).fetchall()
    comp = conn.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
    party = conn.execute("SELECT * FROM parties WHERE id=?", (purchase['party_id'],)).fetchone() if purchase and purchase['party_id'] else None
    conn.close()
    if not purchase:
        return redirect(url_for('purchases'))
    return render_template('view_purchase.html', purchase=purchase, items=items, comp=comp, party=party)

@app.route('/purchases/<int:pur_id>/delete', methods=['POST'])
@login_required
def delete_purchase(pur_id):
    conn = get_db()
    c = conn.cursor()
    purchase = c.execute("SELECT * FROM purchases WHERE id=?", (pur_id,)).fetchone()
    if purchase:
        c.execute("DELETE FROM ledger WHERE ref_type='purchase' AND ref_id=?", (pur_id,))
        c.execute("DELETE FROM purchase_items WHERE purchase_id=?", (pur_id,))
        c.execute("DELETE FROM purchases WHERE id=?", (pur_id,))
        log_audit(conn, 'DELETE', 'purchases', pur_id, purchase, None)
    conn.commit()
    conn.close()
    flash('Purchase deleted successfully.', 'success')
    return redirect(url_for('purchases'))

# â”€â”€ Receipts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/receipts')
@login_required
def receipts():
    conn = get_db()
    all_rects = conn.execute("SELECT * FROM receipts WHERE type='receipt' ORDER BY receipt_date DESC, id DESC").fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('customer', 'both') ORDER BY name").fetchall()
    last = conn.execute("SELECT reference_no FROM receipts WHERE type='receipt' AND reference_no LIKE 'REC-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'REC-0001'
    if last and last['reference_no']:
        try:
            n = int(last['reference_no'].split('-')[1]) + 1
            next_no = f'REC-{n:04d}'
        except: pass
    conn.close()
    return render_template('receipts.html', receipts=all_rects, parties=parties, next_no=next_no)

@app.route('/receipts/add', methods=['POST'])
@login_required
def add_receipt():
    conn = get_db()
    party_id = request.form.get('party_id') or None
    party_name = request.form.get('party_name','')
    amount = to_float(request.form.get('amount',0))
    r_date = request.form['receipt_date']
    payment_mode = request.form.get('payment_mode', 'Cash')
    bank_source = request.form.get('bank_source', '').strip()
    if payment_mode == 'Bank' and bank_source:
        payment_mode = f"Bank - {bank_source}"
        
    c = conn.cursor()
    c.execute("""INSERT INTO receipts (receipt_date,type,party_id,party_name,amount,payment_mode,reference_no,notes,created_by)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (r_date, 'receipt', party_id, party_name, amount, payment_mode,
               request.form.get('reference_no',''), request.form.get('notes',''), current_user.id))
    r_id = c.lastrowid
    
    c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                 VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (r_date, 'receipt', r_id, '', party_id, party_name, 'Payment received', 0, amount, current_user.id))
    conn.commit()
    conn.close()
    flash('Receipt recorded successfully.', 'success')
    return redirect(url_for('receipts'))

# â”€â”€ Payments â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/payments')
@login_required
def payments():
    conn = get_db()
    all_pmts = conn.execute("SELECT * FROM receipts WHERE type='payment' ORDER BY receipt_date DESC, id DESC").fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('supplier', 'both') ORDER BY name").fetchall()
    last = conn.execute("SELECT reference_no FROM receipts WHERE type='payment' AND reference_no LIKE 'PAY-%' ORDER BY id DESC LIMIT 1").fetchone()
    next_no = 'PAY-0001'
    if last and last['reference_no']:
        try:
            n = int(last['reference_no'].split('-')[1]) + 1
            next_no = f'PAY-{n:04d}'
        except: pass
    conn.close()
    return render_template('payments.html', payments=all_pmts, parties=parties, next_no=next_no)

@app.route('/payments/add', methods=['POST'])
@login_required
def add_payment():
    conn = get_db()
    party_id = request.form.get('party_id') or None
    party_name = request.form.get('party_name','')
    amount = to_float(request.form.get('amount',0))
    r_date = request.form['payment_date']
    payment_mode = request.form.get('payment_mode', 'Cash')
    bank_source = request.form.get('bank_source', '').strip()
    if payment_mode == 'Bank' and bank_source:
        payment_mode = f"Bank - {bank_source}"
        
    c = conn.cursor()
    c.execute("""INSERT INTO receipts (receipt_date,type,party_id,party_name,amount,payment_mode,reference_no,notes,created_by)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (r_date, 'payment', party_id, party_name, amount, payment_mode,
               request.form.get('reference_no',''), request.form.get('notes',''), current_user.id))
    r_id = c.lastrowid
    
    c.execute("""INSERT INTO ledger (entry_date,ref_type,ref_id,ref_no,party_id,party_name,description,debit,credit,created_by)
                 VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (r_date, 'payment', r_id, '', party_id, party_name, 'Payment made', amount, 0, current_user.id))
    conn.commit()
    conn.close()
    flash('Payment recorded successfully.', 'success')
    return redirect(url_for('payments'))

@app.route('/receipt/<int:receipt_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_receipt(receipt_id):
    conn = get_db()
    receipt = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not receipt:
        conn.close()
        flash("Transaction not found.", "error")
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        party_id = request.form.get('party_id') or None
        party_name = request.form.get('party_name','')
        amount = to_float(request.form.get('amount',0))
        r_date = request.form['receipt_date']
        payment_mode = request.form.get('payment_mode', 'Cash')
        bank_source = request.form.get('bank_source', '').strip()
        if payment_mode == 'Bank' and bank_source:
            payment_mode = f"Bank - {bank_source}"
        reference_no = request.form.get('reference_no', '')
        notes = request.form.get('notes', '')
        
        c = conn.cursor()
        c.execute("""UPDATE receipts SET receipt_date=?, party_id=?, party_name=?, amount=?, payment_mode=?, reference_no=?, notes=? WHERE id=?""",
                  (r_date, party_id, party_name, amount, payment_mode, reference_no, notes, receipt_id))
                  
        if receipt['type'] == 'receipt':
            c.execute("""UPDATE ledger SET entry_date=?, party_id=?, party_name=?, credit=? WHERE ref_type='receipt' AND ref_id=?""",
                      (r_date, party_id, party_name, amount, receipt_id))
        else:
            c.execute("""UPDATE ledger SET entry_date=?, party_id=?, party_name=?, debit=? WHERE ref_type='payment' AND ref_id=?""",
                      (r_date, party_id, party_name, amount, receipt_id))
        conn.commit()
        conn.close()
        flash(f"{'Receipt' if receipt['type']=='receipt' else 'Payment'} updated successfully.", 'success')
        return redirect(url_for('invoices') if receipt['type']=='receipt' else url_for('purchases'))
        
    parties = conn.execute(f"SELECT * FROM parties WHERE type IN ('{'customer' if receipt['type']=='receipt' else 'supplier'}', 'both') ORDER BY name").fetchall()
    conn.close()
    return render_template('edit_receipt.html', receipt=receipt, parties=parties)

@app.route('/receipt/<int:receipt_id>')
@login_required
def view_receipt(receipt_id):
    conn = get_db()
    receipt = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    conn.close()
    if not receipt:
        flash('Voucher not found.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('view_receipt.html', receipt=receipt)

@app.route('/receipt/<int:receipt_id>/delete', methods=['POST'])
@login_required
def delete_receipt(receipt_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM ledger WHERE ref_type IN ('receipt', 'payment') AND ref_id=?", (receipt_id,))
    c.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    conn.commit()
    conn.close()
    flash('Transaction deleted successfully.', 'success')
    return redirect(request.referrer or url_for('dashboard'))

# â”€â”€ Reports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/reports_hub')
@login_required
def reports_hub():
    return render_template('reports_hub.html')

def get_book_transactions(mode, from_date=None, to_date=None):
    conn = get_db()
    
    # Receipts (IN)
    receipts_in = conn.execute("""
        SELECT receipt_date as date, COALESCE(party_name, 'Unknown') as name, 'Receipt' as source, amount as money_in, 0 as money_out 
        FROM receipts WHERE type='receipt' AND payment_mode LIKE ?
    """, (mode + '%',)).fetchall()
    
    # Payments (OUT)
    receipts_out = conn.execute("""
        SELECT receipt_date as date, COALESCE(party_name, 'Unknown') as name, 'Payment' as source, 0 as money_in, amount as money_out 
        FROM receipts WHERE type='payment' AND payment_mode LIKE ?
    """, (mode + '%',)).fetchall()
    
    # Expenses (OUT)
    expenses = conn.execute("""
        SELECT exp_date as date, category || CASE WHEN payee IS NOT NULL AND payee != '' THEN ' (' || payee || ')' ELSE '' END as name, 'Expense' as source, 0 as money_in, amount as money_out 
        FROM expenses WHERE status='Paid' AND payment_mode LIKE ?
    """, (mode + '%',)).fetchall()
    
    # Other Expenses (OUT)
    other_expenses = conn.execute("""
        SELECT exp_date as date, category || CASE WHEN payee IS NOT NULL AND payee != '' THEN ' (' || payee || ')' ELSE '' END as name, 'Other Expense' as source, 0 as money_in, amount as money_out 
        FROM other_expenses WHERE status='Paid' AND payment_mode LIKE ?
    """, (mode + '%',)).fetchall()
    
    # Other Incomes (IN)
    other_incomes = conn.execute("""
        SELECT inc_date as date, category || CASE WHEN payer IS NOT NULL AND payer != '' THEN ' (' || payer || ')' ELSE '' END as name, 'Other Income' as source, amount as money_in, 0 as money_out 
        FROM incomes WHERE status='Received' AND payment_mode LIKE ?
    """, (mode + '%',)).fetchall()
    
    # Transfers (IN & OUT)
    transfers_in = conn.execute("""
        SELECT transfer_date as date, 'Transfer from ' || from_account as name, 'Transfer' as source, amount as money_in, 0 as money_out 
        FROM transfers WHERE to_account = ?
    """, (mode,)).fetchall()
    
    transfers_out = conn.execute("""
        SELECT transfer_date as date, 'Transfer to ' || to_account as name, 'Transfer' as source, 0 as money_in, amount as money_out 
        FROM transfers WHERE from_account = ?
    """, (mode,)).fetchall()

    # Combine and sort by date
    all_tx = [dict(row) for row in receipts_in + receipts_out + expenses + other_expenses + other_incomes + transfers_in + transfers_out]
    all_tx.sort(key=lambda x: x['date'])
    
    comp = conn.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
    opening_bal = 0
    if comp:
        opening_bal = comp['cash_opening'] if mode == 'Cash' else comp['bank_opening']
        
    if not opening_bal:
        opening_bal = 0
        
    balance = opening_bal
    filtered_tx = []
    
    for tx in all_tx:
        if from_date and tx['date'] < from_date:
            opening_bal += tx['money_in']
            opening_bal -= tx['money_out']
            balance = opening_bal
        else:
            if to_date and tx['date'] > to_date:
                continue
            balance += tx['money_in']
            balance -= tx['money_out']
            tx['balance'] = balance
            filtered_tx.append(tx)
            
    conn.close()
    return opening_bal, filtered_tx, balance

@app.route('/cash_book')
@login_required
def cash_book():
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    opening_bal, transactions, closing_bal = get_book_transactions('Cash', from_date, to_date)
    return render_template('book_ledger.html', title="Cash Book", mode="Cash",
                           opening=opening_bal, transactions=transactions, closing=closing_bal,
                           from_date=from_date, to_date=to_date)

@app.route('/bank_book')
@login_required
def bank_book():
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    opening_bal, transactions, closing_bal = get_book_transactions('Bank', from_date, to_date)
    return render_template('book_ledger.html', title="Bank Book", mode="Bank",
                           opening=opening_bal, transactions=transactions, closing=closing_bal,
                           from_date=from_date, to_date=to_date)

@app.route('/transfer', methods=['GET', 'POST'])
@login_required
def transfer():
    conn = get_db()
    if request.method == 'POST':
        transfer_date = request.form['transfer_date']
        from_account = request.form['from_account']
        to_account = request.form['to_account']
        amount = to_float(request.form.get('amount'), 0.0)
        notes = request.form['notes']
        
        conn.execute('''
            INSERT INTO transfers (transfer_date, from_account, to_account, amount, notes, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (transfer_date, from_account, to_account, amount, notes, current_user.id))
        conn.commit()
        conn.close()
        flash('Transfer recorded successfully.', 'success')
        return redirect(url_for('transfer'))
        
    conn.close()
    return render_template('transfer.html', today=date.today().isoformat())

@app.route('/transfer_list')
@login_required
def transfer_list():
    conn = get_db()
    transfers = conn.execute("SELECT * FROM transfers ORDER BY transfer_date DESC, id DESC").fetchall()
    conn.close()
    return render_template('transfer_list.html', transfers=transfers)

@app.route('/transfer/edit/<int:transfer_id>', methods=['GET', 'POST'])
@login_required
def edit_transfer(transfer_id):
    conn = get_db()
    transfer_record = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
    if request.method == 'POST':
        transfer_date = request.form['transfer_date']
        from_account = request.form['from_account']
        to_account = request.form['to_account']
        amount = to_float(request.form.get('amount'), 0.0)
        notes = request.form['notes']
        
        conn.execute('''
            UPDATE transfers SET transfer_date=?, from_account=?, to_account=?, amount=?, notes=?
            WHERE id=?
        ''', (transfer_date, from_account, to_account, amount, notes, transfer_id))
        conn.commit()
        conn.close()
        flash('Transfer updated successfully.', 'success')
        return redirect(url_for('transfer_list'))
        
    conn.close()
    return render_template('transfer.html', transfer=transfer_record)

@app.route('/transfer/delete/<int:transfer_id>')
@login_required
def delete_transfer(transfer_id):
    conn = get_db()
    conn.execute("DELETE FROM transfers WHERE id=?", (transfer_id,))
    conn.commit()
    conn.close()
    flash('Transfer deleted.', 'success')
    return redirect(url_for('transfer_list'))

# â”€â”€ Ledger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/ledger')
@login_required
def ledger():
    conn = get_db()
    party_id = request.args.get('party_id','')
    from_date = request.args.get('from_date','')
    to_date = request.args.get('to_date','')
    query = "SELECT l.* FROM ledger l LEFT JOIN parties p ON l.party_id = p.id WHERE (p.type IN ('customer', 'both') OR l.party_id IS NULL AND l.ref_type IN ('invoice', 'receipt'))"
    params = []
    if party_id:
        query += " AND l.party_id=?"; params.append(party_id)
    if from_date:
        query += " AND l.entry_date>=?"; params.append(from_date)
    if to_date:
        query += " AND l.entry_date<=?"; params.append(to_date)
    query += " ORDER BY l.entry_date DESC, l.id DESC"
    entries = conn.execute(query, params).fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('customer', 'both') ORDER BY name").fetchall()
    total_debit = sum(e['debit'] for e in entries)
    total_credit = sum(e['credit'] for e in entries)
    conn.close()
    return render_template('ledger.html', entries=entries, parties=parties,
                           total_debit=total_debit, total_credit=total_credit,
                           balance=total_debit-total_credit,
                           party_id=party_id, from_date=from_date, to_date=to_date, ledger_type='customer')

@app.route('/supplier_ledger')
@login_required
def supplier_ledger():
    conn = get_db()
    party_id = request.args.get('party_id','')
    from_date = request.args.get('from_date','')
    to_date = request.args.get('to_date','')
    query = "SELECT l.* FROM ledger l LEFT JOIN parties p ON l.party_id = p.id WHERE (p.type IN ('supplier', 'both') OR l.party_id IS NULL AND l.ref_type IN ('purchase', 'payment'))"
    params = []
    if party_id:
        query += " AND l.party_id=?"; params.append(party_id)
    if from_date:
        query += " AND l.entry_date>=?"; params.append(from_date)
    if to_date:
        query += " AND l.entry_date<=?"; params.append(to_date)
    query += " ORDER BY l.entry_date DESC, l.id DESC"
    entries = conn.execute(query, params).fetchall()
    parties = conn.execute("SELECT * FROM parties WHERE type IN ('supplier', 'both') ORDER BY name").fetchall()
    total_debit = sum(e['debit'] for e in entries)
    total_credit = sum(e['credit'] for e in entries)
    conn.close()
    return render_template('ledger.html', entries=entries, parties=parties,
                           total_debit=total_debit, total_credit=total_credit,
                           balance=total_credit-total_debit,
                           party_id=party_id, from_date=from_date, to_date=to_date, ledger_type='supplier')

# â”€â”€ Company Profile â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/company', methods=['GET', 'POST'])
@login_required
def company():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        address = request.form.get('address')
        phone = request.form.get('phone')
        gstin = request.form.get('gstin')
        state = request.form.get('state')
        bank = request.form.get('bank_details')
        cash = to_float(request.form.get('cash_opening', 0))
        bank_op = to_float(request.form.get('bank_opening', 0))
        
        file = request.files.get('logo')
        logo_filename = request.form.get('current_logo')
        if file and file.filename != '':
            filename = 'logo_' + str(int(datetime.now().timestamp())) + '.png'
            file.save(os.path.join(INSTANCE_DIR, filename))
            logo_filename = filename
            
        conn.execute('''UPDATE company_profile SET name=?, address=?, phone=?, gstin=?, state=?, bank_details=?, cash_opening=?, bank_opening=?, logo_filename=? WHERE id=1''',
                     (name, address, phone, gstin, state, bank, cash, bank_op, logo_filename))
        conn.commit()
        
        # Sync to system DB
        if 'active_db' in session:
            sys_conn = get_system_db()
            sys_conn.execute("UPDATE companies SET name=? WHERE db_filename=?", (name, session['active_db']))
            sys_conn.commit()
            sys_conn.close()
            session['company_name'] = name
            
        flash('Company profile updated.', 'success')
        
    comp = conn.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
    conn.close()
    return render_template('company.html', comp=comp)

# â”€â”€ Products â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/products')
@login_required
def products():
    conn = get_db()
    all_prods = conn.execute("""
        SELECT p.*,
        COALESCE(p.opening_stock, 0) +
        (SELECT COALESCE(SUM(qty), 0) FROM purchase_items WHERE description = p.name) -
        (SELECT COALESCE(SUM(qty), 0) FROM invoice_items WHERE description = p.name) AS current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    conn.close()
    return render_template('products.html', products=all_prods)

@app.route('/products/add', methods=['POST'])
@login_required
def add_product():
    conn = get_db()
    name = request.form.get('name', '').strip()
    if not name:
        flash('Product name is required.', 'error')
        conn.close()
        return redirect(url_for('products'))
    desc = request.form.get('description', '').strip()
    price = to_float(request.form.get('price'), 0.0)
    hsn = request.form.get('hsn', '').strip()
    opening_stock = to_float(request.form.get('opening_stock'), 0.0)
    try:
        conn.execute("INSERT INTO products (name, description, price, hsn, opening_stock) VALUES (?,?,?,?,?)",
                     (name, desc, price, hsn, opening_stock))
        conn.commit()
        flash('Product added successfully.', 'success')
    except Exception as e:
        app.logger.error(f"Error adding product: {e}")
        flash(f'Error adding product: {e}', 'error')
    finally:
        conn.close()
    return redirect(url_for('products'))

@app.route('/product_list')
@login_required
def product_list():
    conn = get_db()
    all_prods = conn.execute("""
        SELECT p.*,
               p.opening_stock 
               + COALESCE((SELECT SUM(qty) FROM purchase_items WHERE description = p.name), 0)
               - COALESCE((SELECT SUM(qty) FROM invoice_items WHERE description = p.name), 0) as current_stock
        FROM products p ORDER BY p.name
    """).fetchall()
    conn.close()
    return render_template('product_list.html', products=all_prods)

@app.route('/product/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_product(id):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (id,)).fetchone()
    if request.method == 'POST':
        conn.execute("UPDATE products SET name=?, description=?, price=?, hsn=?, opening_stock=? WHERE id=?",
                     (request.form['name'], request.form.get('description',''), to_float(request.form.get('price', 0)), request.form.get('hsn',''), to_float(request.form.get('opening_stock', 0)), id))
        conn.commit()
        conn.close()
        flash('Product updated.', 'success')
        return redirect(url_for('product_list'))
    conn.close()
    return render_template('products.html', product=product)

@app.route('/product/delete/<int:id>')
@login_required
def delete_product(id):
    conn = get_db()
    conn.execute("DELETE FROM products WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Product deleted.', 'success')
    return redirect(url_for('product_list'))

# â”€â”€ Users (Admin) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/modules', methods=['GET', 'POST'])
@login_required
def modules_manager():
    if current_user.role != 'admin':
        flash('Access denied. Only admins can manage modules.', 'error')
        return redirect(url_for('dashboard'))
        
    unlocked = session.get('modules_unlocked', False)
        
    conn = get_db()
    if request.method == 'POST':
        if 'master_password' in request.form:
            if request.form['master_password'] == 'SK@CLOUDEDGE':
                session['modules_unlocked'] = True
                flash('Module Manager Unlocked.', 'success')
            else:
                flash('Incorrect Master Password.', 'error')
            return redirect(url_for('modules_manager'))
            
        if not unlocked:
            flash('Access denied. Please unlock first.', 'error')
            return redirect(url_for('modules_manager'))
            
        # Reset all to 0
        conn.execute("UPDATE app_modules SET is_active = 0")
        
        # Set checked ones to 1
        active_mods = request.form.getlist('modules')
        for mod in active_mods:
            conn.execute("UPDATE app_modules SET is_active = 1 WHERE module_name = ?", (mod,))
            
        conn.commit()
        log_audit(conn, 'UPDATE', 'app_modules', 0, None, {'active_modules': active_mods})
        flash('Modules updated successfully.', 'success')
        return redirect(url_for('modules_manager'))
        
    all_mods = conn.execute("SELECT * FROM app_modules ORDER BY module_name").fetchall()
    conn.close()
    return render_template('modules_manager.html', all_mods=all_mods, unlocked=unlocked)

@app.route('/audit_logs')
@login_required
def audit_logs():
    if current_user.role != 'admin':
        flash('Access denied. Only admins can view audit logs.', 'error')
        return redirect(url_for('dashboard'))
    conn = get_db()
    logs = conn.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 1000").fetchall()
    conn.close()
    return render_template('audit_logs.html', logs=logs)

@app.route('/users')
@login_required
def users():
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    conn = get_db()
    all_users = conn.execute("SELECT id,username,full_name,role,created_at FROM users").fetchall()
    conn.close()
    return render_template('users.html', users=all_users)

@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username,password,full_name,role) VALUES (?,?,?,?)",
            (request.form['username'].strip(), generate_password_hash(request.form['password']),
             request.form['full_name'].strip(), request.form.get('role','user')))
        conn.commit()
        flash('User added successfully.', 'success')
    except sqlite3.IntegrityError:
        flash('Username already exists.', 'error')
    except Exception as e:
        flash(f'Database error: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/edit/<int:user_id>', methods=['POST'])
@login_required
def edit_user(user_id):
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    conn = get_db()
    try:
        username = request.form['username'].strip()
        full_name = request.form['full_name'].strip()
        role = request.form.get('role', 'user')
        password = request.form.get('password', '').strip()
        
        if password:
            conn.execute("UPDATE users SET username=?, full_name=?, role=?, password=? WHERE id=?", 
                         (username, full_name, role, generate_password_hash(password), user_id))
        else:
            conn.execute("UPDATE users SET username=?, full_name=?, role=? WHERE id=?", 
                         (username, full_name, role, user_id))
        conn.commit()
        flash('User updated successfully.', 'success')
    except sqlite3.IntegrityError:
        flash('Username already exists.', 'error')
    except Exception as e:
        flash(f'Error updating user: {str(e)}', 'error')
    conn.close()
    return redirect(url_for('users'))

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    conn = get_db()
    
    # Check if deleting the last admin
    user_to_delete = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user_to_delete and user_to_delete['role'] == 'admin':
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            flash('Cannot delete the last admin account.', 'error')
            conn.close()
            return redirect(url_for('users'))
            
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    flash('User deleted successfully.', 'success')
    conn.close()
    return redirect(url_for('users'))

@app.route('/financials')
@login_required
def financials():
    conn = get_db()
    from_date = request.args.get('from_date', '')
    to_date = request.args.get('to_date', '')
    
    date_filter_inv = ""
    date_filter_pur = ""
    params_inv = []
    params_pur = []
    
    if from_date:
        date_filter_inv += " AND invoice_date >= ?"
        date_filter_pur += " AND bill_date >= ?"
        params_inv.append(from_date)
        params_pur.append(from_date)
    if to_date:
        date_filter_inv += " AND invoice_date <= ?"
        date_filter_pur += " AND bill_date <= ?"
        params_inv.append(to_date)
        params_pur.append(to_date)
        
    # --- P&L CALCULATIONS ---
    sales_query = f"SELECT COALESCE(SUM(subtotal), 0) as gross, COALESCE(SUM(discount_amount), 0) as disc FROM invoices WHERE 1=1 {date_filter_inv}"
    sales_res = conn.execute(sales_query, params_inv).fetchone()
    gross_sales = sales_res['gross']
    sales_discounts = sales_res['disc']
    net_sales = gross_sales - sales_discounts
    
    cogs_query = f"""
        SELECT category, COALESCE(SUM(amount - discount_amount), 0) as total_cogs
        FROM purchases 
        WHERE category IN ('Inventory / Stock', 'Materials') {date_filter_pur}
        GROUP BY category
    """
    cogs_rows = conn.execute(cogs_query, params_pur).fetchall()
    total_cogs = sum(row['total_cogs'] for row in cogs_rows)
    
    gross_profit = net_sales - total_cogs
    
    exp_query = f"""
        SELECT category, COALESCE(SUM(amount - discount_amount), 0) as total_exp
        FROM purchases 
        WHERE category NOT IN ('Inventory / Stock', 'Materials') {date_filter_pur}
        GROUP BY category
    """
    expenses_rows = conn.execute(exp_query, params_pur).fetchall()
    total_expenses = sum(row['total_exp'] for row in expenses_rows)
    
    # Add regular expenses to operating expenses
    reg_exp_query = f"""
        SELECT category, COALESCE(SUM(amount), 0) as total_exp
        FROM expenses WHERE status='Paid' {date_filter_pur.replace('bill_date', 'exp_date')}
        GROUP BY category
    """
    reg_exp_rows = conn.execute(reg_exp_query, params_pur).fetchall()
    expenses_rows = list(expenses_rows) + list(reg_exp_rows)
    total_expenses += sum(row['total_exp'] for row in reg_exp_rows)
    
    # Other Expenses
    other_exp_query = f"""
        SELECT category, COALESCE(SUM(amount), 0) as total_exp
        FROM other_expenses WHERE status='Paid' {date_filter_pur.replace('bill_date', 'exp_date')}
        GROUP BY category
    """
    other_expenses_rows = conn.execute(other_exp_query, params_pur).fetchall()
    total_other_expenses = sum(row['total_exp'] for row in other_expenses_rows)
    
    # Other Incomes
    other_inc_query = f"""
        SELECT category, COALESCE(SUM(amount), 0) as total_inc
        FROM incomes WHERE status='Received' {date_filter_inv.replace('invoice_date', 'inc_date')}
        GROUP BY category
    """
    other_incomes_rows = conn.execute(other_inc_query, params_inv).fetchall()
    total_other_incomes = sum(row['total_inc'] for row in other_incomes_rows)
    
    net_profit = gross_profit + total_other_incomes - total_expenses - total_other_expenses
    
    # --- BALANCE SHEET CALCULATIONS (Always all-time) ---
    total_invoiced = conn.execute("SELECT COALESCE(SUM(total), 0) as s FROM invoices").fetchone()['s']
    total_received = conn.execute("SELECT COALESCE(SUM(amount), 0) as s FROM receipts WHERE type='receipt'").fetchone()['s']
    accounts_receivable = total_invoiced - total_received

    total_purchased = conn.execute("SELECT COALESCE(SUM(total), 0) as s FROM purchases").fetchone()['s']
    total_paid = conn.execute("SELECT COALESCE(SUM(amount), 0) as s FROM receipts WHERE type='payment'").fetchone()['s']
    accounts_payable = total_purchased - total_paid

    cash_opening_bal, cash_txs, cash_balance = get_book_transactions('Cash')
    bank_opening_bal, bank_txs, bank_balance = get_book_transactions('Bank')

    total_assets = cash_balance + bank_balance + accounts_receivable
    total_liabilities = accounts_payable

    owners_equity = total_assets - total_liabilities
    
    conn.close()
    
    return render_template('financials.html', 
                           from_date=from_date, to_date=to_date,
                           gross_sales=gross_sales, sales_discounts=sales_discounts,
                           net_sales=net_sales, cogs_rows=cogs_rows,
                           total_cogs=total_cogs, gross_profit=gross_profit,
                           other_incomes_rows=other_incomes_rows, total_other_incomes=total_other_incomes,
                           expenses=expenses_rows, total_expenses=total_expenses,
                           other_expenses_rows=other_expenses_rows, total_other_expenses=total_other_expenses,
                           net_profit=net_profit,
                           accounts_receivable=accounts_receivable,
                           accounts_payable=accounts_payable,
                           cash_balance=cash_balance,
                           bank_balance=bank_balance,
                           total_assets=total_assets,
                           total_liabilities=total_liabilities,
                           owners_equity=owners_equity)

# â”€â”€ Projects & Timesheets â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route('/projects')
@login_required
def projects():
    conn = get_db()
    all_projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template('projects.html', projects=all_projects)

@app.route('/projects/add', methods=['POST'])
@login_required
def add_project():
    name = request.form['name']
    client_name = request.form.get('client_name', '')
    description = request.form.get('description', '')
    budget = to_float(request.form.get('budget', 0))
    status = request.form.get('status', 'Active')
    
    conn = get_db()
    conn.execute('''INSERT INTO projects (name, client_name, description, budget, status) 
                    VALUES (?, ?, ?, ?, ?)''', (name, client_name, description, budget, status))
    conn.commit()
    conn.close()
    flash('Project added successfully.', 'success')
    return redirect(url_for('projects'))

@app.route('/projects/edit/<int:project_id>', methods=['POST'])
@login_required
def edit_project(project_id):
    name = request.form['name']
    client_name = request.form.get('client_name', '')
    description = request.form.get('description', '')
    budget = to_float(request.form.get('budget', 0))
    status = request.form.get('status', 'Active')
    
    conn = get_db()
    conn.execute('''UPDATE projects SET name=?, client_name=?, description=?, budget=?, status=? WHERE id=?''', 
                 (name, client_name, description, budget, status, project_id))
    conn.commit()
    conn.close()
    flash('Project updated successfully.', 'success')
    return redirect(url_for('project_details', project_id=project_id))

@app.route('/projects/delete/<int:project_id>', methods=['POST'])
@login_required
def delete_project(project_id):
    conn = get_db()
    conn.execute("DELETE FROM timesheets WHERE project_id=?", (project_id,))
    conn.execute("UPDATE invoices SET project_id=NULL WHERE project_id=?", (project_id,))
    conn.execute("UPDATE expenses SET project_id=NULL WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    flash('Project deleted successfully.', 'success')
    return redirect(url_for('projects'))

@app.route('/projects/<int:project_id>')
@login_required
def project_details(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash('Project not found.', 'error')
        return redirect(url_for('projects'))
        
    timesheets = conn.execute("SELECT * FROM timesheets WHERE project_id=? ORDER BY task_date DESC", (project_id,)).fetchall()
    invoices = conn.execute("SELECT * FROM invoices WHERE project_id=? ORDER BY invoice_date DESC", (project_id,)).fetchall()
    expenses = conn.execute("SELECT * FROM expenses WHERE project_id=? ORDER BY exp_date DESC", (project_id,)).fetchall()
    
    total_hours = sum(t['hours'] for t in timesheets)
    total_timesheet_cost = sum(t['hours'] * t['hourly_rate'] for t in timesheets)
    total_invoiced = sum(i['total'] for i in invoices)
    total_expenses = sum(e['amount'] for e in expenses)
    
    profitability = total_invoiced - (total_timesheet_cost + total_expenses)
    
    conn.close()
    return render_template('project_details.html', project=project, timesheets=timesheets, 
                           invoices=invoices, expenses=expenses, total_hours=total_hours,
                           total_timesheet_cost=total_timesheet_cost, total_invoiced=total_invoiced,
                           total_expenses=total_expenses, profitability=profitability)

@app.route('/timesheets/add', methods=['POST'])
@login_required
def add_timesheet():
    project_id = request.form['project_id']
    employee_name = request.form['employee_name']
    task_date = request.form['task_date']
    rate_type = request.form.get('rate_type', 'Hourly')
    hours = to_float(request.form.get('hours', 0))
    task_description = request.form.get('task_description', '')
    hourly_rate = to_float(request.form.get('hourly_rate', 0))
    
    conn = get_db()
    conn.execute('''INSERT INTO timesheets (project_id, employee_name, task_date, hours, task_description, hourly_rate, rate_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                 (project_id, employee_name, task_date, hours, task_description, hourly_rate, rate_type))
    conn.commit()
    conn.close()
    flash('Timesheet entry added successfully.', 'success')
    return redirect(url_for('project_details', project_id=project_id))

@app.route('/projects/<int:project_id>/link_invoice', methods=['POST'])
@login_required
def link_project_invoice(project_id):
    invoice_no = request.form.get('invoice_no', '').strip()
    conn = get_db()
    inv = conn.execute("SELECT id FROM invoices WHERE invoice_no=?", (invoice_no,)).fetchone()
    if inv:
        conn.execute("UPDATE invoices SET project_id=? WHERE id=?", (project_id, inv['id']))
        conn.commit()
        flash(f'Invoice {invoice_no} linked successfully.', 'success')
    else:
        flash(f'Invoice {invoice_no} not found.', 'error')
    conn.close()
    return redirect(url_for('project_details', project_id=project_id))

@app.route('/projects/<int:project_id>/link_expense', methods=['POST'])
@login_required
def link_project_expense(project_id):
    expense_ref = request.form.get('expense_ref', '').strip()
    conn = get_db()
    # Try by ref_no first, then by ID
    exp = conn.execute("SELECT id FROM expenses WHERE ref_no=?", (expense_ref,)).fetchone()
    if not exp and expense_ref.isdigit():
        exp = conn.execute("SELECT id FROM expenses WHERE id=?", (int(expense_ref),)).fetchone()
        
    if exp:
        conn.execute("UPDATE expenses SET project_id=? WHERE id=?", (project_id, exp['id']))
        conn.commit()
        flash('Expense linked successfully.', 'success')
    else:
        flash('Expense not found.', 'error')
    conn.close()
    return redirect(url_for('project_details', project_id=project_id))

@app.route('/save_pdf', methods=['POST'])
@login_required
def save_pdf():
    try:
        import webview
        import base64
        data = request.json
        filename = data.get('filename', 'document.pdf')
        b64_data = data.get('pdf_data')
        
        if not b64_data:
            return jsonify({'status': 'error', 'msg': 'No data received'})
            
        if ',' in b64_data:
            b64_data = b64_data.split(',')[1]
            
        pdf_bytes = base64.b64decode(b64_data)
        
        if len(webview.windows) > 0:
            window = webview.windows[0]
            result = window.create_file_dialog(
                webview.SAVE_DIALOG, 
                directory='', 
                save_filename=filename, 
                file_types=('PDF Files (*.pdf)', 'All Files (*.*)')
            )
            if result:
                file_path = result[0] if isinstance(result, (tuple, list)) else result
                with open(file_path, 'wb') as f:
                    f.write(pdf_bytes)
                return jsonify({'status': 'success', 'path': file_path})
            else:
                return jsonify({'status': 'cancelled'})
        else:
            return jsonify({'status': 'error', 'msg': 'No active desktop window found.'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/whatsapp_pdf', methods=['POST'])
@login_required
def whatsapp_pdf():
    try:
        import base64, os, subprocess
        data = request.json
        b64_data = data.get('pdf_data')
        if not b64_data:
            return jsonify({'status': 'error', 'msg': 'No data received'})
        if ',' in b64_data:
            b64_data = b64_data.split(',')[1]
        pdf_bytes = base64.b64decode(b64_data)
        
        temp_dir = os.path.join(INSTANCE_DIR, 'temp')
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        filename = data.get('filename', 'invoice.pdf')
        file_path = os.path.join(temp_dir, filename)
        
        with open(file_path, 'wb') as f:
            f.write(pdf_bytes)
            
        abs_path = os.path.abspath(file_path)
        ps_cmd = f"Add-Type -AssemblyName System.Windows.Forms; $files = New-Object System.Collections.Specialized.StringCollection; $files.Add('{abs_path}'); [System.Windows.Forms.Clipboard]::SetFileDropList($files);"
        subprocess.run(['powershell', '-sta', '-command', ps_cmd], capture_output=True, text=True)
        
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})



if __name__ == '__main__':
    init_system_db()
    init_db("accounting.db")
    print("\n" + "="*50)
    print("  ACCOUNTING SOFTWARE - Running on http://127.0.0.1:5000")
    print("  Default Login: admin / admin123")
    print("="*50 + "\n")
    app.run(debug=True, port=5000)




