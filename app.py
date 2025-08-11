import streamlit as st
import sqlite3
import pandas as pd
import altair as alt
from datetime import date, datetime
import hashlib
from contextlib import contextmanager

st.set_page_config(page_title="Advance Expense Manager", page_icon="💰", layout="wide")
import os
DB_PATH = os.getenv("DB_PATH", "data.db")

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          email TEXT UNIQUE NOT NULL,
          pw_hash TEXT NOT NULL,
          salt TEXT NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          UNIQUE(user_id, name)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          dt TEXT NOT NULL,
          category TEXT NOT NULL,
          description TEXT,
          amount REAL NOT NULL,
          payment TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          month TEXT NOT NULL,      -- 'YYYY-MM'
          category TEXT NOT NULL,
          amount REAL NOT NULL,
          UNIQUE(user_id, month, category)
        )""")

def sha256(s):  # simple salted hash (for demo)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def make_hash(password, salt):
    return sha256(salt + password)

def create_user(name, email, password):
    salt = sha256(email)[0:16]
    pw_hash = make_hash(password, salt)
    with get_conn() as conn:
        conn.execute("INSERT INTO users(name, email, pw_hash, salt) VALUES (?, ?, ?, ?)",
                     (name, email, pw_hash, salt))

def get_user_by_email(email):
    with get_conn() as conn:
        cur = conn.execute("SELECT id, name, email, pw_hash, salt FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2], "pw_hash": row[3], "salt": row[4]}

def auth(email, password):
    user = get_user_by_email(email)
    if not user:
        return None
    if make_hash(password, user["salt"]) == user["pw_hash"]:
        return {"id": user["id"], "name": user["name"], "email": user["email"]}
    return None

@st.cache_data(ttl=60)
def load_expenses(user_id, month=None, category=None):
    q = "SELECT id, dt, category, description, amount, payment FROM expenses WHERE user_id=?"
    params = [user_id]
    if month:
        q += " AND substr(dt,1,7)=?"
        params.append(month)
    if category:
        q += " AND category=?"
        params.append(category)
    with get_conn() as conn:
        df = pd.read_sql_query(q, conn, params=params)
    if not df.empty:
        df["dt"] = pd.to_datetime(df["dt"])
    return df

def add_expense(user_id, dt, category, description, amount, payment):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses(user_id, dt, category, description, amount, payment) VALUES (?,?,?,?,?,?)",
            (user_id, dt, category, description, float(amount), payment)
        )
    st.cache_data.clear()

def delete_expense(expense_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    st.cache_data.clear()

def upsert_budget(user_id, month, category, amount):
    with get_conn() as conn:
        conn.execute("""
          INSERT INTO budgets(user_id, month, category, amount)
          VALUES(?,?,?,?)
          ON CONFLICT(user_id, month, category) DO UPDATE SET amount=excluded.amount
        """, (user_id, month, category, float(amount)))

def get_budgets(user_id, month):
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT category, amount FROM budgets WHERE user_id=? AND month=?",
                               conn, params=(user_id, month))
    return df

def ensure_default_categories(user_id):
    defaults = ["Food", "Transport", "Bills", "Shopping", "Entertainment", "Health", "Other"]
    with get_conn() as conn:
        for name in defaults:
            try:
                conn.execute("INSERT INTO categories(user_id, name) VALUES (?,?)", (user_id, name))
            except sqlite3.IntegrityError:
                pass

def list_categories(user_id):
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT name FROM categories WHERE user_id=?", conn, params=(user_id,))
    if df.empty:
        return []
    return df["name"].tolist()

def export_csv(user_id):
    df = load_expenses(user_id)
    return df.to_csv(index=False).encode("utf-8")

def import_csv(user_id, file):
    df = pd.read_csv(file)
    needed = {"dt", "category", "description", "amount", "payment"}
    if not needed.issubset(set(df.columns)):
        st.error(f"CSV must have columns: {', '.join(needed)}")
        return
    with get_conn() as conn:
        for _, r in df.iterrows():
            conn.execute("INSERT INTO expenses(user_id, dt, category, description, amount, payment) VALUES (?,?,?,?,?,?)",
                         (user_id, str(r["dt"]), str(r["category"]), str(r.get("description","")),
                          float(r["amount"]), str(r.get("payment",""))))
    st.success("Imported!")
    st.cache_data.clear()

def dashboard(user):
    st.subheader(f"Dashboard — {user['name']}")
    col1, col2, col3 = st.columns(3)
    today = date.today()
    month = st.sidebar.selectbox("Month", options=pd.date_range("2022-01-01", today, freq="MS").strftime("%Y-%m").tolist()[::-1], index=0)
    df = load_expenses(user["id"], month=month)
    if df.empty:
        st.info("No expenses yet for this month.")
        return

    total = df["amount"].sum()
    col1.metric("Total Spent", f"₹{total:,.0f}")
    by_cat = df.groupby("category")["amount"].sum().reset_index().sort_values("amount", ascending=False)
    if not by_cat.empty:
        col2.metric("Top Category", f"{by_cat.iloc[0]['category']} — ₹{by_cat.iloc[0]['amount']:,.0f}")

    # Budget compare
    budgets = get_budgets(user["id"], month)
    if not budgets.empty:
        merged = by_cat.merge(budgets, how="left", on="category", suffixes=("_spent", "_budget")).fillna(0)
        over = (merged["amount_spent"] - merged["amount_budget"]).clip(lower=0).sum()
        col3.metric("Over Budget", f"₹{over:,.0f}")

    st.markdown("### Spend by Category")
    chart1 = alt.Chart(by_cat).mark_bar().encode(
        x=alt.X("amount:Q", title="Amount (₹)"),
        y=alt.Y("category:N", sort="-x", title="Category"),
        tooltip=["category", "amount"]
    )
    st.altair_chart(chart1, use_container_width=True)

    st.markdown("### Daily Trend")
    daily = df.groupby(df["dt"].dt.date)["amount"].sum().reset_index()
    daily.columns = ["date", "amount"]
    chart2 = alt.Chart(daily).mark_line(point=True).encode(x="date:T", y="amount:Q", tooltip=["date","amount"])
    st.altair_chart(chart2, use_container_width=True)

def page_add(user):
    st.subheader("Add Expense")
    ensure_default_categories(user["id"])
    categories = list_categories(user["id"])
    with st.form("add_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        dt = c1.date_input("Date", value=date.today())
        category = c2.selectbox("Category", categories)
        description = st.text_input("Description")
        c3, c4 = st.columns(2)
        amount = c3.number_input("Amount (₹)", min_value=0.0, step=50.0)
        payment = c4.selectbox("Payment Method", ["Card", "Cash", "UPI", "Other"])
        submitted = st.form_submit_button("Add")
    if submitted:
        add_expense(user["id"], dt.isoformat(), category, description, amount, payment)
        st.success("Added!")

def page_list(user):
    st.subheader("Transactions")
    month = st.selectbox("Month", options=pd.date_range("2022-01-01", date.today(), freq="MS").strftime("%Y-%m").tolist()[::-1])
    df = load_expenses(user["id"], month=month)
    if df.empty:
        st.info("No transactions yet.")
        return
    st.dataframe(df.sort_values("dt", ascending=False), use_container_width=True)

    st.markdown("#### Delete a transaction")
    to_del = st.selectbox("Select ID to delete", options=df["id"].tolist())
    if st.button("Delete"):
        delete_expense(int(to_del))
        st.success("Deleted.")

def page_budgets(user):
    st.subheader("Budgets")
    month = st.selectbox("Budget month", options=pd.date_range("2022-01-01", date.today(), freq="MS").strftime("%Y-%m").tolist()[::-1])
    categories = list_categories(user["id"])
    cat = st.selectbox("Category", categories)
    amount = st.number_input("Monthly Budget (₹)", min_value=0.0, step=100.0)
    if st.button("Save/Update Budget"):
        upsert_budget(user["id"], month, cat, amount)
        st.success("Saved!")

    df = get_budgets(user["id"], month)
    if not df.empty:
        st.markdown("#### Budgets for " + month)
        st.table(df)

def page_import_export(user):
    st.subheader("Import / Export")
    csv = export_csv(user["id"])
    st.download_button("Download CSV", csv, "expenses.csv", "text/csv")
    st.divider()
    up = st.file_uploader("Upload CSV to import", type=["csv"])
    if up is not None:
        import_csv(user["id"], up)

def login_ui():
    st.header("Advance Expense Manager")
    tab1, tab2 = st.tabs(["Login", "Register"])
    with tab1:
        email = st.text_input("Email")
        pw = st.text_input("Password", type="password")
        if st.button("Login"):
            user = auth(email, pw)
            if user:
                st.session_state.user = user
                st.experimental_rerun()
            else:
                st.error("Invalid credentials")
    with tab2:
        name = st.text_input("Name", key="r_name")
        email2 = st.text_input("Email", key="r_email")
        pw2 = st.text_input("Password", type="password", key="r_pw")
        if st.button("Create account"):
            try:
                create_user(name, email2, pw2)
                st.success("Account created. Please login.")
            except sqlite3.IntegrityError:
                st.error("Email already exists.")

def app():
    init_db()
    user = st.session_state.get("user")
    if not user:
        login_ui()
        return

    with st.sidebar:
        st.markdown(f"Hello, **{user['name']}**")
        page = st.selectbox("Navigate", ["Dashboard", "Add Expense", "Transactions", "Budgets", "Import/Export"])
        if st.button("Logout"):
            st.session_state.user = None
            st.experimental_rerun()

    if page == "Dashboard": dashboard(user)
    elif page == "Add Expense": page_add(user)
    elif page == "Transactions": page_list(user)
    elif page == "Budgets": page_budgets(user)
    elif page == "Import/Export": page_import_export(user)

if __name__ == "__main__":
    app()
