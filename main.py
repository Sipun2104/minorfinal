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

# ---------- App ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# ----------------------------------------------------------
# 🚀 FIXED DB PATH FOR RAILWAY
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
        username = request.form["username"].strip()
        password = request.form["password"]
        daily_limit = request.form.get("daily_limit")
        daily_limit = float(daily_limit) if daily_limit else None

        if not username or not password:
            flash("Username and password required", "danger")
            return redirect(url_for("register"))

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO users (username, password_hash, daily_limit) VALUES (?,?,?)",
                (username, generate_password_hash(password), daily_limit)
            )
            conn.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already taken.", "danger")
        finally:
            conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
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
        ym = datetime.utcnow().strftime("%Y-%m")

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

    month = request.args.get("month") or datetime.utcnow().strftime("%Y-%m")
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

# ---------- Budget API ----------
@app.route("/api/budget_status")
def api_budget_status():
    if require_login(): return require_login()
    uid = session["user_id"]
    ym = request.args.get("month") or datetime.utcnow().strftime("%Y-%m")
    conn = get_db()
    budgets = conn.execute("""
        SELECT category, amount FROM budgets WHERE user_id=? AND month=?
    """, (uid, ym)).fetchall()
    status = []

    for b in budgets:
        cat = b["category"]
        amt = float(b["amount"])
        if cat == TOTAL_BUDGET_KEY:
            continue
        spent = _spent_in_category_month(conn, uid, cat, ym)
        pct = (spent / amt) * 100 if amt > 0 else 0
        status.append({
            "category": _display_category(cat),
            "budget": round(amt, 2),
            "spent": round(spent, 2),
            "pct": round(pct, 2),
            "exceeded": spent > amt
        })

    total_row = next((b for b in budgets if b["category"] == TOTAL_BUDGET_KEY), None)
    if total_row:
        amt = float(total_row["amount"])
        spent = _spent_total_month(conn, uid, ym)
        pct = (spent / amt) * 100 if amt > 0 else 0
        status.insert(0, {
            "category": TOTAL_BUDGET_LABEL,
            "budget": round(amt, 2),
            "spent": round(spent, 2),
            "pct": round(pct, 2),
            "exceeded": spent > amt
        })

    conn.close()
    return jsonify({"month": ym, "status": status})

# ---------- Export ----------
@app.route("/export/csv")
def export_csv():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    rows = conn.execute("""
        SELECT id, date, category, title, description, amount, split_with
        FROM expenses WHERE user_id=? ORDER BY date DESC
    """, (uid,)).fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["id","date","category","title","description","amount","split_with"])
    for r in rows:
        cw.writerow([
            r["id"],
            r["date"],
            _display_category(r["category"]),
            r["title"] or "",
            r["description"] or "",
            r["amount"],
            r["split_with"] or ""
        ])
    output = si.getvalue()
    return Response(output, mimetype="text/csv",
                    headers={"Content-Disposition":"attachment;filename=expenses.csv"})

# Excel export
@app.route("/export/excel")
def export_excel():
    if require_login(): return require_login()
    if pd is None:
        flash("Excel export requires pandas. Please install pandas first.", "warning")
        return redirect(url_for("dashboard"))

    uid = session["user_id"]
    conn = get_db()
    rows = conn.execute("""
        SELECT date, category, title, description, amount, split_with
        FROM expenses WHERE user_id=? ORDER BY date DESC
    """, (uid,)).fetchall()
    conn.close()

    df = pd.DataFrame([{
        **dict(r),
        "category": _display_category(r["category"])
    } for r in rows])
    buff = BytesIO()
    with pd.ExcelWriter(buff, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Expenses")
    buff.seek(0)
    return send_file(buff, as_attachment=True,
                     download_name="expenses.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# PDF monthly report
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

@app.route("/export/pdf/<month>")
def export_pdf(month):
    if require_login(): return require_login()
    uid = session["user_id"]
    start_d, end_d = month_bounds(month)
    conn = get_db()
    rows = conn.execute("""
        SELECT date, category, title, amount, description FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
        ORDER BY date
    """, (uid, start_d, end_d)).fetchall()
    total = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (uid, start_d, end_d)).fetchone()["s"]
    conn.close()

    buff = BytesIO()
    p = canvas.Canvas(buff, pagesize=A4)
    w, h = A4
    y = h - 40
    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y, f"Expense Report - {month}")
    y -= 30
    p.setFont("Helvetica", 11)
    for r in rows:
        line = f"{r['date']}  |  {_display_category(r['category']):<16}  |  ₹{r['amount']:<9}  |  {r['title'] or ''} {('- '+r['description']) if r['description'] else ''}"
        p.drawString(40, y, line[:110])
        y -= 16
        if y < 40:
            p.showPage(); y = h - 40; p.setFont("Helvetica", 11)
    y -= 10
    p.setFont("Helvetica-Bold", 12)
    p.drawString(40, y, f"Total for {month}: ₹{round(total,2)}")
    p.save()
    buff.seek(0)
    return send_file(buff, as_attachment=True,
                     download_name=f"expense_report_{month}.pdf",
                     mimetype="application/pdf")

# ---------- Import from CSV ----------
@app.route("/import/csv", methods=["POST"])
def import_csv():
    if require_login(): return require_login()
    file = request.files.get("csv_file")
    if not file or file.filename.strip() == "":
        flash("No CSV file selected.", "warning")
        return redirect(url_for("dashboard"))

    # Expected headers: id(optional),date,category,title,description,amount,split_with
    try:
        stream = StringIO(file.stream.read().decode("utf-8"))
        reader = csv.DictReader(stream)
        conn = get_db()
        cur = conn.cursor()
        count = 0
        for row in reader:
            try:
                when = row.get("date") or date.today().isoformat()
                amt = float(row.get("amount") or 0)
                cat = _normalize_category(row.get("category") or "Uncategorized")
                cur.execute("""
                    INSERT INTO expenses (user_id, title, category, amount, date, description, split_with)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    session["user_id"],
                    row.get("title"),
                    cat,
                    amt,
                    when,
                    row.get("description"),
                    row.get("split_with")
                ))
                count += 1
            except Exception:
                # skip bad rows
                continue
        conn.commit()
        conn.close()
        flash(f"Imported {count} expenses from CSV.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "danger")
    return redirect(url_for("dashboard"))

# ---------- Analytics APIs ----------
@app.route("/api/trend/<int:days>")
def api_trend(days):
    if require_login(): return require_login()
    uid = session["user_id"]
    end = date.today()
    start = end - timedelta(days=days-1)
    conn = get_db()
    rows = conn.execute("""
        SELECT date, COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND date BETWEEN ? AND ?
        GROUP BY date ORDER BY date
    """,(uid, start, end)).fetchall()
    conn.close()
    by_day = {str(r["date"]): float(r["s"]) for r in rows}
    labels, data = [], []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        labels.append(d)
        data.append(round(by_day.get(d, 0.0), 2))
    return jsonify({"labels": labels, "data": data})

@app.route("/api/category-breakdown")
def api_category_breakdown():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    rows = conn.execute("""
        SELECT category, COALESCE(SUM(amount),0) AS s
        FROM expenses WHERE user_id=? GROUP BY category
    """, (uid,)).fetchall()
    conn.close()
    labels = [_display_category(r["category"]) for r in rows]
    data = [round(float(r["s"]),2) for r in rows]
    return jsonify({"labels": labels, "data": data})

@app.route("/api/daily-limit")
def api_daily_limit():
    if require_login(): return require_login()
    uid = session["user_id"]
    today = date.today()
    conn = get_db()
    u = conn.execute("SELECT daily_limit FROM users WHERE id=?", (uid,)).fetchone()
    spent = conn.execute("""
        SELECT COALESCE(SUM(amount),0) AS s FROM expenses
        WHERE user_id=? AND date=?
    """,(uid, today)).fetchone()["s"]
    conn.close()
    limit = float(u["daily_limit"]) if u and u["daily_limit"] is not None else 0
    return jsonify({"limit": limit, "spent": round(float(spent),2), "exceeded": limit>0 and spent>limit})

# ---------- Bill Splitting helper ----------
@app.route("/api/split/<int:expense_id>")
def api_split(expense_id):
    if require_login(): return require_login()
    conn = get_db()
    e = conn.execute("SELECT * FROM expenses WHERE id=? AND user_id=?", (expense_id, session["user_id"])).fetchone()
    conn.close()
    if not e:
        return jsonify({"error": "not found"}), 404
    people = [p.strip() for p in (e["split_with"] or "").split(",") if p.strip()]
    count = 1 + len(people)  # including owner
    per = round(float(e["amount"]) / count, 2) if count else float(e["amount"])
    owes = dict.fromkeys(people, per)
    return jsonify({"total": e["amount"], "per_person": per, "owes": owes})

# ---------- AI-ish Insights ----------
@app.route("/api/predict_next_month")
def predict_next_month():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    rows = conn.execute("""
      SELECT strftime('%Y-%m', date) AS ym, COALESCE(SUM(amount),0) AS s
      FROM expenses
      WHERE user_id=? GROUP BY ym ORDER BY ym
    """, (uid,)).fetchall()
    conn.close()
    totals = [float(r["s"]) for r in rows]
    if not totals:
        return jsonify({"prediction": 0, "advice":"Add some data to see insights"})
    if len(totals) >= 2:
        diffs = [totals[i]-totals[i-1] for i in range(1,len(totals))]
        avg_change = sum(diffs)/len(diffs)
        pred = round(totals[-1] + avg_change, 2)
    else:
        pred = round(totals[-1], 2)
    avg = sum(totals)/len(totals)
    advice = "On track"
    if pred > avg * 1.1:
        advice = "Spending likely to grow. Try reducing top categories by ~10%."
    elif pred < avg * 0.9:
        advice = "Great job! Predicted spend below average."
    return jsonify({"prediction": pred, "advice": advice})

# ---------- Delete Expense ----------
@app.route("/delete/expense/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Expense deleted successfully.", "success")
    return redirect(url_for("dashboard"))

# ---------- Delete Income ----------
@app.route("/delete/income/<int:income_id>", methods=["POST"])
def delete_income(income_id):
    if require_login(): return require_login()
    conn = get_db()
    conn.execute("DELETE FROM incomes WHERE id=? AND user_id=?", (income_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Income deleted successfully.", "success")
    return redirect(url_for("dashboard"))

@app.route("/delete/all", methods=["POST"])
def delete_all():
    if require_login(): return require_login()
    uid = session["user_id"]
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM incomes WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    flash("All history cleared.", "warning")
    return redirect(url_for("dashboard"))


# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

