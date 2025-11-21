# main.py
import csv
import os
import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# Optional libs for export
try:
    import pandas as pd  # for Excel export + optional imports
except Exception:
    pd = None

# For PDF generation
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
except Exception:
    canvas = None  # We'll check before PDF generation and raise meaningful error

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# ----------------------------------------------------------
# 🚀 FIXED DB PATH FOR RAILWAY / LOCAL
# ----------------------------------------------------------
DB_PATH = os.path.join("instance", "database.db")
os.makedirs("instance", exist_ok=True)
# ----------------------------------------------------------

# Large expense threshold for push notification trigger
LARGE_EXPENSE_THRESHOLD = float(os.environ.get("LARGE_EXPENSE_THRESHOLD", "5000"))

# Special category key used for monthly total budget
TOTAL_BUDGET_KEY = "__TOTAL__"
TOTAL_BUDGET_LABEL = "Total (All Categories)"

# ---------- DB helpers ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_email_column(conn):
    """
    If the users table exists but doesn't have an 'email' column, add it.
    This helps migrating existing DBs gracefully.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in cur.fetchall()]  # r[1] is column name
    if "email" not in cols:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN email TEXT")
            conn.commit()
        except Exception:
            # If alter fails for any reason, ignore (table might be brand new or locked)
            pass

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Create users table with username and password_hash; email may be added later if missing
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        daily_limit REAL DEFAULT NULL
    )
    """)
    # Ensure email column exists (for older DBs this will add it)
    ensure_email_column(conn)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT,
        category TEXT,
        amount REAL NOT NULL,
        date DATE NOT NULL,
        description TEXT,
        split_with TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS incomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source TEXT,
        amount REAL NOT NULL,
        date DATE NOT NULL,
        description TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        month TEXT NOT NULL,
        amount REAL NOT NULL,
        UNIQUE(user_id, category, month)
    )
    """)

    conn.commit()
    conn.close()

init_db()

# ---------- Auth ----------
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        daily_limit = request.form.get("daily_limit")
        daily_limit = float(daily_limit) if daily_limit else None

        if not username or not password:
            flash("Username and password required", "danger")
            return redirect(url_for("register"))

        # Basic email validation (not strict)
        if email and "@" not in email:
            flash("Please enter a valid email address or leave email empty.", "danger")
            return redirect(url_for("register"))

        conn = get_db()
        cur = conn.cursor()
        try:
            # If email column doesn't exist (very old DB), ensure it first
            ensure_email_column(conn)

            # If email provided, try to insert with it; else insert NULL email
            cur.execute(
                "INSERT INTO users (username, password_hash, daily_limit, email) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), daily_limit, email or None)
            )
            conn.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError as e:
            # Determine whether username or email caused conflict
            msg = str(e).lower()
            if "username" in msg:
                flash("Username already taken. Try another one.", "danger")
            elif "email" in msg:
                flash("Email already registered. Try logging in or use a different email.", "danger")
            else:
                # Fallback: check manually
                existing = cur.execute("SELECT * FROM users WHERE username=? OR email=?", (username, email)).fetchone()
                if existing:
                    if existing["username"] == username:
                        flash("Username already taken.", "danger")
                    elif email and existing["email"] == email:
                        flash("Email already registered.", "danger")
                    else:
                        flash("Account already exists.", "danger")
                else:
                    flash("Unable to create account.", "danger")
        finally:
            conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("username", "").strip()  # could be username or email
        password = request.form.get("password", "")

        if not identifier or not password:
            flash("Provide username/email and password.", "danger")
            return redirect(url_for("login"))

        conn = get_db()
        # Try to find by username OR by email (case-insensitive for email)
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? OR lower(email) = ?",
            (identifier, identifier.lower())
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash("Logged in successfully", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

def require_login():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return None

# ---------- Helpers ----------
def month_bounds(ym: str):
    first = datetime.strptime(ym+"-01", "%Y-%m-%d").date()
    if first.month == 12:
        nxt = date(first.year+1, 1, 1)
    else:
        nxt = date(first.year, first.month+1, 1)
    last = nxt - timedelta(days=1)
    return first, last

def _spent_in_category_month(conn, uid, category, ym):
    r = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND category=? AND strftime('%Y-%m', date)=?
    """, (uid, category, ym)).fetchone()
    return float(r["s"] or 0)

def _spent_total_month(conn, uid, ym):
    first, last = month_bounds(ym)
    r = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (uid, first, last)).fetchone()
    return float(r["s"] or 0)

def _normalize_category(cat: str) -> str:
    if not cat:
        return "General"
    c = cat.strip()
    if c.lower() in ("total", "overall", "all", "*"):
        return TOTAL_BUDGET_KEY
    return c

def _display_category(cat: str) -> str:
    return TOTAL_BUDGET_LABEL if cat == TOTAL_BUDGET_KEY else (cat or "Uncategorized")

# ---------- Add Income/Expense ----------
@app.route("/add", methods=["GET", "POST"])
def add_expense():
    if require_login():
        return require_login()

    if request.method == "POST":
        title = request.form.get("title")
        category = _normalize_category(request.form.get("category") or "Uncategorized")
        amount = float(request.form.get("amount") or 0)

        date_str = request.form.get("date") or date.today().isoformat()
        try:
            when = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
        except ValueError:
            when = date.today().isoformat()

        desc = request.form.get("description")
        split_with = request.form.get("split_with")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO expenses (user_id, title, category, amount, date, description, split_with)
            VALUES (?,?,?,?,?,?,?)
        """, (session["user_id"], title, category, amount, when, desc, split_with))
        conn.commit()

        ym = when[:7]

        # Category budget check
        budget_cat = cur.execute("""
            SELECT amount FROM budgets WHERE user_id=? AND category=? AND month=?
        """, (session["user_id"], category, ym)).fetchone()

        if budget_cat and category != TOTAL_BUDGET_KEY:
            spent_now = _spent_in_category_month(conn, session["user_id"], category, ym)
            budget_amt = float(budget_cat["amount"])
            if spent_now > budget_amt:
                flash(
                    f"⚠️ Budget exceeded for {_display_category(category)} in {ym}! "
                    f"Spent ₹{round(spent_now, 2)} / Budget ₹{round(budget_amt, 2)}",
                    "danger"
                )
            elif spent_now >= 0.75 * budget_amt:
                flash(
                    f"⚠️ Warning: You’ve already spent ₹{round(spent_now, 2)} "
                    f"out of your ₹{round(budget_amt, 2)} {_display_category(category)} budget for {ym}.",
                    "warning"
                )

        # Total budget check
        budget_total = cur.execute("""
            SELECT amount FROM budgets WHERE user_id=? AND category=? AND month=?
        """, (session["user_id"], TOTAL_BUDGET_KEY, ym)).fetchone()

        if budget_total:
            total_spent_now = _spent_total_month(conn, session["user_id"], ym)
            total_budget_amt = float(budget_total["amount"])
            if total_spent_now > total_budget_amt:
                flash(
                    f"⚠️ Total monthly budget exceeded in {ym}! "
                    f"Spent ₹{round(total_spent_now, 2)} / Budget ₹{round(total_budget_amt, 2)}",
                    "danger"
                )
            elif total_spent_now >= 0.75 * total_budget_amt:
                flash(
                    f"⚠️ Warning: You’ve already spent ₹{round(total_spent_now, 2)} "
                    f"out of your total budget ₹{round(total_budget_amt, 2)} for {ym}.",
                    "warning"
                )

        conn.close()

        notify_flag = 1 if amount >= LARGE_EXPENSE_THRESHOLD else 0
        return redirect(url_for("dashboard", notify_large=notify_flag, amt=amount))

    return render_template("add_expense.html", current_date=date.today().isoformat())

# ---------- Add Income ----------
@app.route("/add_income", methods=["GET","POST"])
def add_income():
    if require_login(): return require_login()
    if request.method == "POST":
        source = request.form.get("source")
        amount = float(request.form.get("amount") or 0)
        when = request.form.get("date") or date.today().isoformat()
        desc = request.form.get("description")

        conn = get_db()
        conn.execute("""
            INSERT INTO incomes (user_id, source, amount, date, description)
            VALUES (?,?,?,?,?)
        """, (session["user_id"], source, amount, when, desc))
        conn.commit()
        conn.close()
        flash("Income added.", "success")
        return redirect(url_for("dashboard"))
    return render_template("add_income.html")

# ---------- Dashboard ----------
@app.route("/", methods=["GET","POST"])
@app.route("/dashboard", methods=["GET","POST"])
def dashboard():
    if require_login(): return require_login()

    ym = request.form.get("month") or request.args.get("month")
    if not ym:
        # use local time now to avoid datetime.utcnow deprecation warnings
        ym = datetime.now().strftime("%Y-%m")

    start_d, end_d = month_bounds(ym)
    uid = session["user_id"]

    conn = get_db()
    total_income = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM incomes
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (uid, start_d, end_d)).fetchone()["s"]

    total_expenses = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (uid, start_d, end_d)).fetchone()["s"]

    balance = round(total_income - total_expenses, 2)

    expenses = conn.execute("""
        SELECT * FROM expenses WHERE user_id=? AND date BETWEEN ? AND ? 
        ORDER BY date DESC
    """, (uid, start_d, end_d)).fetchall()

    incomes = conn.execute("""
        SELECT * FROM incomes WHERE user_id=? AND date BETWEEN ? AND ? 
        ORDER BY date DESC
    """, (uid, start_d, end_d)).fetchall()

    # Budgets and progress for the month
    budgets = conn.execute("""
      SELECT category, amount FROM budgets
      WHERE user_id=? AND month=?
    """, (uid, ym)).fetchall()

    progress = []
    for b in budgets:
        cat = b["category"]
        amt = float(b["amount"])
        if cat == TOTAL_BUDGET_KEY:
            continue
        spent = conn.execute("""
            SELECT COALESCE(SUM(amount),0) AS s FROM expenses
            WHERE user_id=? AND category=? AND strftime('%Y-%m', date)=?
        """, (uid, cat, ym)).fetchone()["s"]
        pct = round((spent / amt) * 100, 2) if amt else 0
        progress.append({
            "category": _display_category(cat),
            "budget": round(amt, 2),
            "spent": round(float(spent),2),
            "pct": pct
        })

    total_budget_row = next((b for b in budgets if b["category"] == TOTAL_BUDGET_KEY), None)
    if total_budget_row:
        total_budget_amt = float(total_budget_row["amount"])
        total_spent = _spent_total_month(conn, uid, ym)
        total_pct = round((total_spent / total_budget_amt) * 100, 2) if total_budget_amt else 0
        progress.insert(0, {
            "category": TOTAL_BUDGET_LABEL,
            "budget": round(total_budget_amt, 2),
            "spent": round(total_spent, 2),
            "pct": total_pct
        })

    # charts
    cur = conn.execute("""
        SELECT date, COALESCE(SUM(amount),0) AS total
        FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
        GROUP BY date ORDER BY date
    """, (uid, start_d, end_d))
    chart_labels, chart_expense = [], []
    for r in cur.fetchall():
        chart_labels.append(str(r["date"]))
        chart_expense.append(round(r["total"],2))

    cur = conn.execute("""
        SELECT date, COALESCE(SUM(amount),0) AS total
        FROM incomes
        WHERE user_id=? AND date BETWEEN ? AND ?
        GROUP BY date ORDER BY date
    """, (uid, start_d, end_d))
    chart_income = [round(r["total"],2) for r in cur.fetchall()]

    # user daily limit
    u = conn.execute("SELECT daily_limit FROM users WHERE id=?", (uid,)).fetchone()
    daily_limit = u["daily_limit"] if u else None

    conn.close()

    notify_large = request.args.get("notify_large", "0") == "1"
    last_amt = request.args.get("amt", None)

    return render_template(
        "dashboard.html",
        selected_month=ym,
        total_income=round(total_income,2),
        total_expenses=round(total_expenses,2),
        balance=balance,
        expenses=expenses,
        income=incomes,
        progress=progress,
        chart_labels=chart_labels,
        chart_income=chart_income,
        chart_expense=chart_expense,
        daily_limit=daily_limit,
        notify_large=notify_large,
        last_amt=last_amt,
        large_threshold=LARGE_EXPENSE_THRESHOLD
    )

# ---------- Export CSV ----------
@app.route('/export/csv')
def export_csv():
    try:
        if "user_id" not in session:
            return redirect(url_for("login"))

        uid = session["user_id"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT date, category, description, amount
            FROM expenses
            WHERE user_id = ?
            ORDER BY date DESC
        """, (uid,))
        rows = cur.fetchall()
        conn.close()

        output = "Date,Category,Description,Amount\n"
        for r in rows:
            # Use safe escaping for commas/newlines in description/title by quoting if needed
            date_s = r["date"]
            cat_s = r["category"] or ""
            desc_s = (r["description"] or "").replace("\n", " ").replace("\r", " ")
            amt_s = r["amount"]
            output += f'{date_s},{cat_s},"{desc_s}",{amt_s}\n'

        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=expenses.csv"}
        )

    except Exception as e:
        return str(e)

# ---------- PDF Export (Full Monthly Report) ----------
@app.route("/export/pdf/<month>")
def export_pdf(month):
    """
    Generates a monthly PDF report for the logged-in user.
    Report includes:
      - Total income for the month
      - A list of expenses (date, category, description, amount)
      - A category summary: total spent per category
    """
    if "user_id" not in session:
        return redirect(url_for("login"))

    if canvas is None:
        return "PDF generation library (reportlab) is not installed on the server.", 500

    # validate month format YYYY-MM
    try:
        datetime.strptime(month + "-01", "%Y-%m-%d")
    except Exception:
        return "Invalid month format. Use YYYY-MM (e.g. 2025-11).", 400

    uid = session["user_id"]
    start_d, end_d = month_bounds(month)

    conn = get_db()
    # total income
    income_row = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM incomes
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (uid, start_d, end_d)).fetchone()
    total_income = float(income_row["s"] or 0)

    # expenses list
    expenses = conn.execute("""
        SELECT date, category, description, amount
        FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
        ORDER BY date ASC
    """, (uid, start_d, end_d)).fetchall()

    # category summary
    cat_rows = conn.execute("""
        SELECT COALESCE(category,'Uncategorized') AS category, COALESCE(SUM(amount),0) AS total
        FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
        GROUP BY category
        ORDER BY total DESC
    """, (uid, start_d, end_d)).fetchall()

    conn.close()

    # Build PDF
    buffer = BytesIO()
    page_w, page_h = A4
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Expense_Report_{month}")
    left_margin = 40
    right_margin = 40
    y = page_h - 50
    line_height = 14

    # Header
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left_margin, y, f"Monthly Expense Report — {month}")
    y -= 25

    pdf.setFont("Helvetica", 11)
    pdf.drawString(left_margin, y, f"User: {session.get('username', 'Unknown')}")
    pdf.drawRightString(page_w - right_margin, y, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    y -= 20

    # Income summary
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left_margin, y, f"Total Income: ₹{round(total_income,2)}")
    y -= 20

    # Expense list header
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left_margin, y, "Expenses:")
    y -= 16
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left_margin, y, "Date")
    pdf.drawString(left_margin + 80, y, "Category")
    pdf.drawString(left_margin + 220, y, "Description")
    pdf.drawRightString(page_w - right_margin, y, "Amount (₹)")
    y -= 12
    pdf.line(left_margin, y, page_w - right_margin, y)
    y -= 8

    pdf.setFont("Helvetica", 10)
    for r in expenses:
        date_s = str(r["date"])
        cat_s = (r["category"] or "Uncategorized")
        desc_s = (r["description"] or "")
        amt_s = float(r["amount"] or 0)

        # wrap description to fit in the available width; simple wrap by length
        max_desc_chars = 60
        desc_lines = []
        if len(desc_s) <= max_desc_chars:
            desc_lines = [desc_s]
        else:
            # naive wrap
            while desc_s:
                desc_lines.append(desc_s[:max_desc_chars])
                desc_s = desc_s[max_desc_chars:]

        # print first line with date/category/first desc
        pdf.drawString(left_margin, y, date_s)
        pdf.drawString(left_margin + 80, y, cat_s[:28])
        pdf.drawString(left_margin + 220, y, desc_lines[0][:80])
        pdf.drawRightString(page_w - right_margin, y, f"{round(amt_s,2)}")
        y -= line_height

        # additional desc lines
        for extra in desc_lines[1:]:
            if y < 70:
                pdf.showPage()
                pdf.setFont("Helvetica", 10)
                y = page_h - 50
            pdf.drawString(left_margin + 220, y, extra[:80])
            y -= line_height

        if y < 90:
            pdf.showPage()
            pdf.setFont("Helvetica", 10)
            y = page_h - 50

    # Category summary
    if y < 140:
        pdf.showPage()
        y = page_h - 50

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left_margin, y, "Category Summary:")
    y -= 16
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left_margin, y, "Category")
    pdf.drawRightString(page_w - right_margin, y, "Total (₹)")
    y -= 10
    pdf.line(left_margin, y, page_w - right_margin, y)
    y -= 8
    pdf.setFont("Helvetica", 10)
    for cr in cat_rows:
        cat = cr["category"] or "Uncategorized"
        total = float(cr["total"] or 0)
        pdf.drawString(left_margin, y, cat[:60])
        pdf.drawRightString(page_w - right_margin, y, f"{round(total,2)}")
        y -= line_height
        if y < 70:
            pdf.showPage()
            pdf.setFont("Helvetica", 10)
            y = page_h - 50

    pdf.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"expenses_{month}.pdf",
        mimetype="application/pdf"
    )

# ---------- Budgets ----------
@app.route("/budgets", methods=["GET","POST"])
def budgets():
    if require_login(): return require_login()
    uid = session["user_id"]
    if request.method == "POST":
        cat_raw = request.form["category"]
        cat = _normalize_category(cat_raw)
        month = request.form["month"]
        amount = float(request.form["amount"])
        conn = get_db()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO budgets (user_id, category, month, amount)
                VALUES (?,?,?,?)
            """, (uid, cat, month, amount))
            conn.commit()
            if cat == TOTAL_BUDGET_KEY:
                flash(f"Total monthly budget saved for {month}.", "success")
            else:
                flash("Budget saved.", "success")
        finally:
            conn.close()
        return redirect(url_for("budgets", month=month))

    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    conn = get_db()
    rows = conn.execute("""
        SELECT id, category, amount FROM budgets WHERE user_id=? AND month=?
    """, (uid, month)).fetchall()
    conn.close()

    display_rows = []
    for r in rows:
        display_rows.append({
            "id": r["id"],
            "category": _display_category(r["category"]),
            "amount": r["amount"]
        })
    return render_template("budget.html", month=month, budgets=display_rows)

@app.route("/delete_budget/<int:budget_id>", methods=["POST"])
def delete_budget(budget_id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM budgets WHERE id=? AND user_id=?", (budget_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Budget deleted successfully.", "success")
    return redirect(url_for("budgets"))

# ---------- (rest of your app routes, APIs, delete handlers, etc.)
# If you have other routes below in your original file, keep them here.
# For example APIs that the dashboard JS calls (category breakdown / trend / daily-limit),
# ensure those endpoints exist and return JSON; if they're missing, add them similarly to CSV/PDF above.

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
