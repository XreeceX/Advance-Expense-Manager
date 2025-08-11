import os
import hashlib
from datetime import date
from typing import Optional, List, Dict

import streamlit as st
import pandas as pd
import altair as alt
from pymongo import MongoClient, ASCENDING
from bson import ObjectId

st.set_page_config(page_title="Advance Expense Manager", page_icon="💰", layout="wide")
@st.cache_resource
def get_db():
uri = os.getenv("MONGODB_URI")
if not uri:
raise RuntimeError("MONGODB_URI is not set")
client = MongoClient(uri)
db_name = os.getenv("MONGODB_DB", "expense_manager")
db = client[db_name]
# indexes/uniques
db.users.create_index("email", unique=True)
db.categories.create_index([("user_id", ASCENDING), ("name", ASCENDING)], unique=True)
db.budgets.create_index([("user_id", ASCENDING), ("month", ASCENDING), ("category", ASCENDING)], unique=True)
db.expenses.create_index([("user_id", ASCENDING), ("dt", ASCENDING)])
return db
db = get_db()
def sha256(s: str) -> str:
return hashlib.sha256(s.encode("utf-8")).hexdigest()

def make_hash(password: str, salt: str) -> str:
return sha256(salt + password)

def get_user_by_email(email: str) -> Optional[Dict]:
u = db.users.find_one({"email": email})
if not u:
return None
return {"_id": u["_id"], "name": u["name"], "email": u["email"], "pw_hash": u["pw_hash"], "salt": u["salt"]}

def create_user(name: str, email: str, password: str) -> None:
salt = sha256(email)[:16]
pw_hash = make_hash(password, salt)
db.users.insert_one({"name": name, "email": email, "pw_hash": pw_hash, "salt": salt})

def auth(email: str, password: str) -> Optional[Dict]:
u = get_user_by_email(email)
if not u:
return None
if make_hash(password, u["salt"]) == u["pw_hash"]:
return {"id": str(u["_id"]), "name": u["name"], "email": u["email"]}
return None

def ensure_default_categories(user_id: str):
defaults = ["Food", "Transport", "Bills", "Shopping", "Entertainment", "Health", "Other"]
for name in defaults:
db.categories.update_one(
{"user_id": user_id, "name": name},
{"$setOnInsert": {"user_id": user_id, "name": name}},
upsert=True,
)

@st.cache_data(ttl=60)
def load_expenses(user_id: str, month: Optional[str] = None, category: Optional[str] = None) -> pd.DataFrame:
q: Dict = {"user_id": user_id}
if month:
q["dt"] = {"$regex": f"^{month}"} # dt is 'YYYY-MM-DD'
if category:
q["category"] = category
docs = list(db.expenses.find(q).sort("dt", ASCENDING))
if not docs:
return pd.DataFrame(columns=["id", "dt", "category", "description", "amount", "payment"])
rows = []
for d in docs:
rows.append({
"id": str(d["_id"]),
"dt": d["dt"],
"category": d["category"],
"description": d.get("description", ""),
"amount": float(d["amount"]),
"payment": d.get("payment", "")
})
df = pd.DataFrame(rows)
df["dt"] = pd.to_datetime(df["dt"])
return df

def add_expense(user_id: str, dt: str, category: str, description: str, amount: float, payment: str):
db.expenses.insert_one({
"user_id": user_id, "dt": dt, "category": category,
"description": description, "amount": float(amount), "payment": payment
})
st.cache_data.clear()

def delete_expense(expense_id: str):
try:
db.expenses.delete_one({"_id": ObjectId(expense_id)})
except Exception:
# ignore invalid ObjectId formats
pass
st.cache_data.clear()

def list_categories(user_id: str) -> List[str]:
docs = list(db.categories.find({"user_id": user_id}, {"name": 1, "_id": 0}))
return [d["name"] for d in docs]

def upsert_budget(user_id: str, month: str, category: str, amount: float):
db.budgets.update_one(
{"user_id": user_id, "month": month, "category": category},
{"$set": {"amount": float(amount)}},
upsert=True,
)

def get_budgets(user_id: str, month: str) -> pd.DataFrame:
docs = list(db.budgets.find({"user_id": user_id, "month": month}, {"_id": 0, "category": 1, "amount": 1}))
if not docs:
return pd.DataFrame(columns=["category", "amount"])
return pd.DataFrame(docs)

def export_csv(user_id: str) -> bytes:
df = load_expenses(user_id)
return df.to_csv(index=False).encode("utf-8")

def import_csv(user_id: str, file):
df = pd.read_csv(file)
needed = {"dt", "category", "description", "amount", "payment"}
if not needed.issubset(set(df.columns)):
st.error(f"CSV must have columns: {', '.join(needed)}")
return
records = []
for _, r in df.iterrows():
records.append({
"user_id": user_id,
"dt": str(r["dt"]),
"category": str(r["category"]),
"description": str(r.get("description", "")),
"amount": float(r["amount"]),
"payment": str(r.get("payment", "")),
})
if records:
db.expenses.insert_many(records)
st.success("Imported!")
st.cache_data.clear()

def dashboard(user):
st.subheader(f"Dashboard — {user['name']}")
today = date.today()
months = pd.date_range("2022-01-01", today, freq="MS").strftime("%Y-%m").tolist()[::-1]
month = st.sidebar.selectbox("Month", options=months, index=0)
df = load_expenses(user["id"], month=month)
if df.empty:
    st.info("No expenses yet for this month.")
    return

col1, col2, col3 = st.columns(3)
total = df["amount"].sum()
col1.metric("Total Spent", f"₹{total:,.0f}")
by_cat = df.groupby("category")["amount"].sum().reset_index().sort_values("amount", ascending=False)
if not by_cat.empty:
    col2.metric("Top Category", f"{by_cat.iloc[0]['category']} — ₹{by_cat.iloc[0]['amount']:,.0f}")

budgets = get_budgets(user["id"], month)
if not budgets.empty:
    merged = by_cat.merge(budgets, how="left", on="category", suffixes=("_spent", "_budget")).fillna(0)
    over = (merged["amount_spent"] - merged["amount_budget"]).clip(lower=0).sum()
    col3.metric("Over Budget", f"₹{over:,.0f}")

st.markdown("### Spend by Category")
st.altair_chart(
    alt.Chart(by_cat).mark_bar().encode(
        x=alt.X("amount:Q", title="Amount (₹)"),
        y=alt.Y("category:N", sort="-x", title="Category"),
        tooltip=["category", "amount"]
    ),
    use_container_width=True,
)

st.markdown("### Daily Trend")
daily = df.groupby(df["dt"].dt.date)["amount"].sum().reset_index()
daily.columns = ["date", "amount"]
st.altair_chart(
    alt.Chart(daily).mark_line(point=True).encode(x="date:T", y="amount:Q", tooltip=["date", "amount"]),
    use_container_width=True,
)

def page_add(user):
st.subheader("Add Expense")
ensure_default_categories(user["id"])
categories = list_categories(user["id"]) or ["Other"]

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
months = pd.date_range("2022-01-01", date.today(), freq="MS").strftime("%Y-%m").tolist()[::-1]
month = st.selectbox("Month", options=months)
df = load_expenses(user["id"], month=month)
if df.empty:
st.info("No transactions yet.")
return
# Show id for delete
df_view = df.copy()
st.dataframe(df_view.sort_values("dt", ascending=False), use_container_width=True)
st.markdown("#### Delete a transaction")
to_del = st.selectbox("Select ID to delete", options=df["id"].tolist())
if st.button("Delete"):
delete_expense(to_del)
st.success("Deleted.")
st.cache_data.clear()

def page_budgets(user):
st.subheader("Budgets")
months = pd.date_range("2022-01-01", date.today(), freq="MS").strftime("%Y-%m").tolist()[::-1]
month = st.selectbox("Budget month", options=months)
categories = list_categories(user["id"]) or ["Other"]
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
except Exception as e:
st.error("Email may already exist.")

def app():
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

if page == "Dashboard":
    dashboard(user)
elif page == "Add Expense":
    page_add(user)
elif page == "Transactions":
    page_list(user)
elif page == "Budgets":
    page_budgets(user)
elif page == "Import/Export":
    page_import_export(user)

if name == "main":
app()
