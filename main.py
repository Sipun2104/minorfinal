# main.py
import csv
import os
import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO

from flask import (
    Flask, Response, flash, jsonify, redirect,
    render_template, request, send_file, session, url_for, make_response
)
from werkzeug.security import check_password_hash, generate_password_hash

# Optional libs for export
try:
    import pandas as pd
except ImportError:
    pd = None

# For PDF generation
try:
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.pagesizes import A4
except ImportError:
    pdf_canvas = None

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

DB_PATH = os.path.join("instance", "database.db")
os.makedirs("instance", exist_ok=True)

TOTAL_BUDGET_KEY = "__TOTAL__"
TOTAL_BUDGET_LABEL = "Total (All Categories)"

# ---------- DB helpers ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_email_column(conn):
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in cur.fetchall()]
    if "email" not in cols:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN email TEXT")
            conn.commit()
        except Exception:
            pass

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        daily_limit REAL DEFAULT NULL
    )
    """)
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

        if email and "@" not in email:
            flash("Please enter a valid email or leave empty.", "danger")
            return redirect(url_for("register"))

        conn = get_db()
        try:
            ensure_email_column(conn)
            conn.execute(
                "INSERT INTO users (username, password_hash, daily_limit, email) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), daily_limit, email or None)
            )
            conn.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "username" in msg:
                flash("Username already taken.", "danger")
            elif "email" in msg:
                flash("Email already registered.", "danger")
            else:
                flash("Unable to create account.", "danger")
        finally:
            conn.close()
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not identifier or not password:
            flash("Provide username/email and password.", "danger")
            return redirect(url_for("login"))

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? OR lower(email)=?",
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
        nxt = date(first.year+1,1,1)
    else:
        nxt = date(first.year, first.month+1,1)
    last = nxt - timedelta(days=1)
    return first, last

def _normalize_category(cat: str) -> str:
    if not cat: return "General"
    c = cat.strip()
    if c.lower() in ("total", "overall", "all", "*"): return TOTAL_BUDGET_KEY
    return c

def _display_category(cat: str) -> str:
    return TOTAL_BUDGET_LABEL if cat==TOTAL_BUDGET_KEY else (cat or "Uncategorized")

# ---------- Add Expense ----------
@app.route("/add", methods=["GET","POST"])
def add_expense():
    if require_login(): return require_login()
    if request.method == "POST":
        title = request.form.get("title")
        category = _normalize_category(request.form.get("category") or "Uncategorized")
        amount = float(request.form.get("amount") or 0)
        date_str = request.form.get("date") or date.today().isoformat()
        try: when = datetime.strptime(date_str,"%Y-%m-%d").date().isoformat()
        except ValueError: when = date.today().isoformat()
        desc = request.form.get("description")
        split_with = request.form.get("split_with")

        conn = get_db()
        conn.execute("""
            INSERT INTO expenses (user_id, title, category, amount, date, description, split_with)
            VALUES (?,?,?,?,?,?,?)
        """,(session["user_id"], title, category, amount, when, desc, split_with))
        conn.commit()
        conn.close()
        flash("Expense added.", "success")
        return redirect(url_for("dashboard"))
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
        """,(session["user_id"], source, amount, when, desc))
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
    uid = session["user_id"]
    ym = request.form.get("month") or request.args.get("month") or datetime.now().strftime("%Y-%m")
    start_d, end_d = month_bounds(ym)

    conn = get_db()
    # Total income/expenses
    total_income = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM incomes WHERE user_id=? AND date BETWEEN ? AND ?",
        (uid, start_d, end_d)
    ).fetchone()["s"]
    total_expenses = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?",
        (uid, start_d, end_d)
    ).fetchone()["s"]
    balance = round(total_income - total_expenses, 2)

    expenses = conn.execute(
        "SELECT * FROM expenses WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY date DESC",
        (uid, start_d, end_d)
    ).fetchall()
    incomes = conn.execute(
        "SELECT * FROM incomes WHERE user_id=? AND date BETWEEN ? AND ? ORDER BY date DESC",
        (uid, start_d, end_d)
    ).fetchall()

    # Prepare data for bar chart
    day_labels, day_income, day_expense = [], [], []
    day = start_d
    while day <= end_d:
        day_labels.append(day.strftime("%Y-%m-%d"))
        day_income.append(sum(i["amount"] for i in incomes if i["date"]==day.isoformat()))
        day_expense.append(sum(e["amount"] for e in expenses if e["date"]==day.isoformat()))
        day += timedelta(days=1)

    # --- NEW: Fetch budgets and compute progress ---
    budget_rows = conn.execute(
        "SELECT category, amount FROM budgets WHERE user_id=? AND month=?",
        (uid, ym)
    ).fetchall()
    progress = []
    for b in budget_rows:
        cat = b["category"]
        budget_amt = b["amount"]
        spent_amt = sum(e["amount"] for e in expenses if e["category"] == cat)
        pct = round((spent_amt / budget_amt * 100) if budget_amt else 0, 2)
        progress.append({
            "category": cat,
            "spent": spent_amt,
            "budget": budget_amt,
            "pct": pct
        })

    conn.close()

    return render_template(
        "dashboard.html",
        expenses=expenses,
        income=incomes,
        total_income=round(total_income,2),
        total_expenses=round(total_expenses,2),
        balance=balance,
        selected_month=ym,
        chart_labels=day_labels,
        chart_income=day_income,
        chart_expense=day_expense,
        progress=progress  # <-- pass to template
    )


# ---------- Budgets ----------
@app.route('/budgets', methods=['GET', 'POST'])
def budgets():
    if require_login(): return require_login()
    uid = session["user_id"]
    month = request.args.get('month') or datetime.today().strftime("%Y-%m")

    conn = get_db()
    if request.method == 'POST':
        category = request.form.get('category')
        amount = request.form.get('amount')
        month_form = request.form.get('month')

        if category and amount and month_form:
            # Insert or replace to avoid duplicate for same user/category/month
            conn.execute("""
                INSERT OR REPLACE INTO budgets (user_id, category, month, amount)
                VALUES (?,?,?,?)
            """, (uid, category, month_form, float(amount)))
            conn.commit()
            flash("Budget saved successfully!", "success")
            return redirect(url_for('budgets', month=month_form))

    # GET request → fetch budgets for this month
    budgets_list = conn.execute("SELECT * FROM budgets WHERE user_id=? AND month=?", (uid, month)).fetchall()
    conn.close()
    return render_template('budgets.html', budgets=budgets_list, month=month)

# ---------- Delete Budget ----------
@app.route("/delete_budget/<int:budget_id>", methods=["POST"])
def delete_budget(budget_id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM budgets WHERE id=? AND user_id=?", (budget_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Budget deleted.", "success")
    return redirect(url_for("budgets"))

# ---------- Delete ----------
@app.route("/delete/income/<int:id>", methods=["POST"])
def delete_income(id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM incomes WHERE id=? AND user_id=?", (id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Income deleted.", "success")
    return redirect(url_for("dashboard"))

@app.route("/delete/expense/<int:id>", methods=["POST"])
def delete_expense(id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Expense deleted.", "success")
    return redirect(url_for("dashboard"))

@app.route("/delete/all", methods=["POST"])
def delete_all():
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM incomes WHERE user_id=?", (session["user_id"],))
    conn.execute("DELETE FROM expenses WHERE user_id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    flash("All history cleared.", "success")
    return redirect(url_for("dashboard"))

# ---------- Export CSV ----------
@app.route("/export/csv")
def export_csv():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    expenses = conn.execute("SELECT * FROM expenses WHERE user_id=?", (uid,)).fetchall()
    incomes  = conn.execute("SELECT * FROM incomes WHERE user_id=?", (uid,)).fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Expense ID","Title","Category","Amount","Date","Description","Split With"])
    for e in expenses: cw.writerow([e["id"], e["title"], e["category"], e["amount"], e["date"], e["description"], e["split_with"]])
    cw.writerow([])
    cw.writerow(["Income ID","Source","Amount","Date","Description"])
    for i in incomes: cw.writerow([i["id"], i["source"], i["amount"], i["date"], i["description"]])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=finance_export.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# ---------- Export PDF ----------
@app.route("/export/pdf")
def export_pdf():
    if require_login(): return require_login()
    if pdf_canvas is None:
        flash("PDF export requires reportlab.", "danger")
        return redirect(url_for("dashboard"))

    uid = session["user_id"]
    conn = get_db()
    expenses = conn.execute("SELECT * FROM expenses WHERE user_id=?", (uid,)).fetchall()
    incomes = conn.execute("SELECT * FROM incomes WHERE user_id=?", (uid,)).fetchall()
    conn.close()

    buffer = BytesIO()
    c = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, f"Finance Report")
    y -= 30

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Expenses:")
    y -= 20
    for e in expenses:
        text = f"{e['date']} | {e['title']} | {e['category']} | ₹{e['amount']}"
        c.drawString(60, y, text)
        y -= 15
        if y < 50:
            c.showPage()
            y = height - 50

    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Incomes:")
    y -= 20
    for i in incomes:
        text = f"{i['date']} | {i['source']} | ₹{i['amount']}"
        c.drawString(60, y, text)
        y -= 15
        if y < 50:
            c.showPage()
            y = height - 50

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="finance_report.pdf", mimetype="application/pdf")

# ---------- APIs for charts ----------
@app.route("/api/trend/30")
def api_trend():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    today = date.today()
    labels, data = [], []
    for i in range(30):
        d = today - timedelta(days=29-i)
        labels.append(d.strftime("%d-%b"))
        amt = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND date=?", (uid,d.isoformat())).fetchone()[0]
        data.append(amt)
    conn.close()
    return jsonify({"labels": labels, "data": data})

@app.route("/api/category-breakdown")
def api_category_breakdown():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    rows = conn.execute("SELECT category, COALESCE(SUM(amount),0) AS s FROM expenses WHERE user_id=? GROUP BY category", (uid,)).fetchall()
    labels = [r["category"] for r in rows]
    data = [r["s"] for r in rows]
    conn.close()
    return jsonify({"labels": labels, "data": data})

# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
