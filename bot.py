import datetime
import io
import csv
import sqlite3
import os
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
from telebot import TeleBot, types
from apscheduler.schedulers.background import BackgroundScheduler

# --- Configuration & Initialization ---
API_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '0'))
GOOGLE_APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxFZNCyFFnNgT4UrklSZ6jjHA_m0mCzpOFOf81OMIPHRDAhOY3N_ANxuKi236SRTCK2Ng/exec"

if not API_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

bot = TeleBot(API_TOKEN, parse_mode='Markdown')
scheduler = BackgroundScheduler()
scheduler.start()

DB_FILE = 'wash_and_scan.db'

# --- Thread pool for async operations ---
executor = ThreadPoolExecutor(max_workers=4)

# --- Cache for Google Sheets data ---
SERVICES_CACHE = []
CACHE_TIMESTAMP = 0
CACHE_VALIDITY = 300  # 5 minutes

# --- Database Initialization ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                chat_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'main_menu',
                pending_service TEXT,
                pending_department TEXT,
                pending_price INTEGER,
                master_msg_id INTEGER
            )
        ''')
        
        cursor.execute("PRAGMA table_info(user_states)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'pending_department' not in columns:
            cursor.execute("ALTER TABLE user_states ADD COLUMN pending_department TEXT")
        if 'master_msg_id' not in columns:
            cursor.execute("ALTER TABLE user_states ADD COLUMN master_msg_id INTEGER")
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                order_id_user INTEGER,
                department TEXT,
                service TEXT,
                price INTEGER,
                time TEXT,
                date TEXT
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_chat_date ON orders(chat_id, date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_orders_chat_id ON orders(chat_id)')
        
        conn.commit()

init_db()

# --- Connection management ---
_db_lock = threading.Lock()

def db_execute(query, params=(), fetch=False, fetchall=False, commit=True):
    """Thread-safe DB execution with automatic connection handling"""
    with _db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row if fetchall else None
        cursor = conn.cursor()
        try:
            cursor.execute(query, params)
            if commit:
                conn.commit()
            if fetchall:
                result = [dict(r) for r in cursor.fetchall()]
                return result
            if fetch:
                result = cursor.fetchone()
                return result
            return cursor.lastrowid
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

# --- Date/Time Parsing Helpers ---
def parse_sheet_date(date_val):
    """
    Parse date from Google Sheets which can be:
    - Date object (JS Date string like "Tue Jul 14 2026...")
    - String "2026-07-14"
    - String "7/14/2026"
    - String "14-07-2026"
    Returns: "YYYY-MM-DD" string or today's date if unparseable
    """
    if not date_val:
        return datetime.date.today().strftime("%Y-%m-%d")
    
    date_str = str(date_val).strip()
    
    # If it's a JS Date string like "Tue Jul 14 2026 00:00:00 GMT+0000"
    # Extract the date parts
    import re
    
    # Try to match JS Date format: "Tue Jul 14 2026 00:00:00 GMT+0000"
    js_date_match = re.search(r'([A-Za-z]{3})\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})', date_str)
    if js_date_match:
        month_map = {
            'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
            'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
        }
        try:
            month = month_map[js_date_match.group(2)]
            day = int(js_date_match.group(3))
            year = int(js_date_match.group(4))
            return f"{year:04d}-{month:02d}-{day:02d}"
        except:
            pass
    
    # Try standard formats
    formats = [
        "%Y-%m-%d",      # 2026-07-14
        "%d-%m-%Y",      # 14-07-2026
        "%m/%d/%Y",      # 7/14/2026
        "%d/%m/%Y",      # 14/7/2026
        "%Y/%m/%d",      # 2026/7/14
    ]
    
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(date_str, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    # Fallback: try to extract YYYY-MM-DD from any string
    iso_match = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', date_str)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    
    # Ultimate fallback
    print(f"⚠️ Could not parse date: '{date_str}', using today")
    return datetime.date.today().strftime("%Y-%m-%d")

def parse_sheet_time(time_val):
    """
    Parse time from Google Sheets which can be:
    - Date object (JS Date string like "Sat Dec 30 1899 13:44:21 GMT-0016")
    - String "13:44:21"
    - String "13:44"
    Returns: "HH:MM:SS" string or current time if unparseable
    """
    if not time_val:
        return datetime.datetime.now().strftime("%H:%M:%S")
    
    time_str = str(time_val).strip()
    import re
    
    # If it's a JS Date string with time like "Sat Dec 30 1899 13:44:21 GMT-0016"
    # Extract HH:MM:SS
    time_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', time_str)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3))
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    
    # Try standard formats
    formats = [
        "%H:%M:%S",      # 13:44:21
        "%H:%M",         # 13:44
        "%I:%M:%S %p",   # 01:44:21 PM
        "%I:%M %p",      # 01:44 PM
    ]
    
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(time_str, fmt)
            return parsed.strftime("%H:%M:%S")
        except ValueError:
            continue
    
    # Ultimate fallback
    print(f"⚠️ Could not parse time: '{time_str}', using now")
    return datetime.datetime.now().strftime("%H:%M:%S")

# --- Google Sheets Integration ---
def fetch_services_from_google_sheets():
    global SERVICES_CACHE, CACHE_TIMESTAMP
    
    current_time = time.time()
    if SERVICES_CACHE and (current_time - CACHE_TIMESTAMP) < CACHE_VALIDITY:
        return SERVICES_CACHE
    
    try:
        response = requests.get(GOOGLE_APPS_SCRIPT_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        SERVICES_CACHE = data
        CACHE_TIMESTAMP = current_time
        print(f"✅ Loaded {len(data)} services from Google Sheets")
        return data
    except Exception as e:
        print(f"❌ Error fetching services: {e}")
        return SERVICES_CACHE

def fetch_orders_from_google_sheets():
    try:
        url = f"{GOOGLE_APPS_SCRIPT_URL}?action=getOrders"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, list):
            print(f"📥 Fetched {len(data)} orders from Sheets")
            return data
        print(f"⚠️ Unexpected response format: {type(data)}")
        return []
    except Exception as e:
        print(f"❌ Error fetching orders: {e}")
        return []

def restore_orders_from_sheets(target_chat_id=None):
    """
    Restore orders from Google Sheets into SQLite.
    Handles Date objects from Sheets properly.
    """
    orders = fetch_orders_from_google_sheets()
    if not orders:
        return 0, "❌ لا توجد بيانات في Google Sheets"
    
    # Clear existing orders
    if target_chat_id:
        db_execute("DELETE FROM orders WHERE chat_id = ?", (target_chat_id,))
    else:
        db_execute("DELETE FROM orders")
    
    restored_count = 0
    skipped = 0
    errors = []
    
    for order in orders:
        try:
            # Parse chatId
            raw_chat_id = order.get("chatId", order.get("id", "0"))
            if raw_chat_id is None:
                raw_chat_id = "0"
            chat_id_str = str(raw_chat_id).strip()
            chat_id_str = chat_id_str.replace('.0', '').replace('e+', '').replace('E+', '')
            chat_id_str = ''.join(c for c in chat_id_str if c.isdigit() or c == '-')
            order_chat_id = int(chat_id_str) if chat_id_str else 0
            
            if target_chat_id and order_chat_id != target_chat_id:
                continue
            
            if order_chat_id == 0:
                skipped += 1
                continue
            
            order_id_user = int(order.get("num", 0))
            department = str(order.get("department", "")).strip()
            service = str(order.get("service", "")).strip()
            price = int(float(order.get("price", 0)))
            
            # CRITICAL FIX: Use robust date/time parsing for Date objects from Sheets
            date_raw = order.get("date", "")
            time_raw = order.get("time", "")
            
            date_str = parse_sheet_date(date_raw)
            time_str = parse_sheet_time(time_raw)
            
            print(f"📝 Parsed: date='{date_raw}' -> '{date_str}', time='{time_raw}' -> '{time_str}'")
            
            db_execute('''
                INSERT INTO orders (chat_id, order_id_user, department, service, price, time, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (order_chat_id, order_id_user, department, service, price, time_str, date_str))
            
            restored_count += 1
            
        except Exception as e:
            errors.append(str(e))
            skipped += 1
            continue
    
    msg = f"✅ تم استعادة *{restored_count}* طلب"
    if skipped > 0:
        msg += f"\n⚠️ تم تخطي *{skipped}* سجل"
    if errors:
        msg += f"\n❌ أخطاء: {errors[:3]}"
    
    print(f"✅ Restore complete: {restored_count} restored, {skipped} skipped")
    return restored_count, msg

def _async_log_to_sheets(department, service, price, chat_id):
    try:
        now = datetime.datetime.now()
        payload = {
            "department": department,
            "service": service,
            "price": price,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "chatId": str(chat_id)
        }
        response = requests.post(GOOGLE_APPS_SCRIPT_URL, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Async Sheets log failed: {e}")

def log_order_to_google_sheets(department, service, price, chat_id):
    executor.submit(_async_log_to_sheets, department, service, price, chat_id)

# --- Helper Functions ---
def get_user_state(chat_id):
    row = db_execute(
        "SELECT state, pending_service, pending_department, pending_price, master_msg_id FROM user_states WHERE chat_id = ?",
        (chat_id,), fetch=True
    )
    
    if not row:
        db_execute("INSERT INTO user_states (chat_id) VALUES (?)", (chat_id,))
        return {
            "state": "main_menu",
            "pending_order": None,
            "pending_department": None,
            "master_msg_id": None
        }
    
    pending_order = {
        "service": row[1],
        "department": row[2],
        "price": row[3]
    } if row[1] else None
    
    return {
        "state": row[0],
        "pending_order": pending_order,
        "pending_department": row[2],
        "master_msg_id": row[4]
    }

def update_user_state(chat_id, state=None, pending_order=None, pending_department=None, master_msg_id=None, clear_pending=False):
    if state:
        db_execute("UPDATE user_states SET state = ? WHERE chat_id = ?", (state, chat_id))
    if master_msg_id is not None:
        db_execute("UPDATE user_states SET master_msg_id = ? WHERE chat_id = ?", (master_msg_id, chat_id))
    if pending_department:
        db_execute("UPDATE user_states SET pending_department = ? WHERE chat_id = ?", (pending_department, chat_id))
    if pending_order:
        db_execute(
            "UPDATE user_states SET pending_service = ?, pending_price = ? WHERE chat_id = ?",
            (pending_order["service"], pending_order["price"], chat_id)
        )
    if clear_pending:
        db_execute(
            "UPDATE user_states SET pending_service = NULL, pending_price = NULL, pending_department = NULL WHERE chat_id = ?",
            (chat_id,)
        )

def record_order(chat_id, department, service, price):
    now = datetime.datetime.now()
    
    row = db_execute("SELECT MAX(order_id_user) FROM orders WHERE chat_id = ?", (chat_id,), fetch=True)
    last_id = row[0] if row and row[0] else 0
    next_id = last_id + 1
    
    db_execute('''
        INSERT INTO orders (chat_id, order_id_user, department, service, price, time, date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (chat_id, next_id, department, service, price, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d")))
    
    log_order_to_google_sheets(department, service, price, chat_id)
    
    return {
        "id": next_id,
        "department": department,
        "service": service,
        "price": price,
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d")
    }

def get_user_orders(chat_id):
    return db_execute(
        "SELECT id, order_id_user, department, service, price, time, date FROM orders WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,), fetchall=True
    )

def get_departments_markup():
    services = fetch_services_from_google_sheets()
    
    departments = {}
    for item in services:
        dept = item.get("department", "Unknown")
        if dept not in departments:
            departments[dept] = []
        departments[dept].append(item)
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    
    for dept in sorted(departments.keys()):
        buttons.append(types.InlineKeyboardButton(f"📦 {dept}", callback_data=f"dept_{dept}"))
    
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("📊 التقارير", callback_data="menu_reports"))
    
    return markup

def get_services_by_department_markup(department):
    services = fetch_services_from_google_sheets()
    dept_services = [s for s in services if s.get("department") == department]
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for svc in dept_services:
        svc_name = svc.get("service", "Unknown")
        price = svc.get("price", 0)
        button_text = f"{svc_name} • {price}ج"
        button = types.InlineKeyboardButton(button_text, callback_data=f"service_{department}_{svc_name}_{price}")
        markup.add(button)
    
    markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
    return markup

def get_main_menu_text(chat_id):
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    orders = get_user_orders(chat_id)
    today_orders = [o for o in orders if str(o.get("date", "")) == today_str]
    
    total_rev = sum(o["price"] for o in today_orders)
    total_count = len(today_orders)
    
    text = f"📊 *نظرة عامة — اليوم*\n"
    text += f"📅 {today_str}\n"
    text += "━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 إجمالي الإيرادات: *{total_rev} ج*\n"
    text += f"📦 عدد الطلبات: *{total_count}*\n\n"
    text += "📝 *ملخص الأقسام اليوم:*\n"
    
    if today_orders:
        rows = db_execute("""
            SELECT department, COUNT(*) as cnt, SUM(price) as total
            FROM orders
            WHERE chat_id = ? AND date = ?
            GROUP BY department
        """, (chat_id, today_str), fetchall=True)
        
        for row in rows:
            dept = row["department"]
            count = row["cnt"]
            subtotal = row["total"]
            text += f"• {dept}: {count} طلب ({subtotal}ج)\n"
    else:
        text += "لا توجد طلبات حتى الآن\n"
    
    text += "\n➕ *إضافة طلب جديد:*"
    return text

def get_reports_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = types.InlineKeyboardButton("📆 اليوم", callback_data="rep_today")
    b2 = types.InlineKeyboardButton("📈 الشهر", callback_data="rep_month")
    b3 = types.InlineKeyboardButton("📤 تصدير CSV", callback_data="export_csv")
    b4 = types.InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
    
    markup.add(b1, b2)
    markup.add(b3)
    markup.add(b4)
    return markup

def clean_chat(chat_id, master_msg_id):
    if not master_msg_id:
        return
    try:
        for offset in range(1, 20):
            try:
                bot.delete_message(chat_id, master_msg_id + offset)
            except:
                pass
            try:
                if master_msg_id - offset > 0:
                    bot.delete_message(chat_id, master_msg_id - offset)
            except:
                pass
    except:
        pass

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    
    data = get_user_state(chat_id)
    master_msg_id = data.get("master_msg_id")
    if not master_msg_id or master_msg_id != msg_id:
        update_user_state(chat_id, master_msg_id=msg_id)
        master_msg_id = msg_id
    
    if call.data == "main_menu":
        text = get_main_menu_text(chat_id)
        markup = get_departments_markup()
        update_user_state(chat_id, state="main_menu", clear_pending=True)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    elif call.data.startswith("dept_"):
        department = call.data.replace("dept_", "")
        text = f"📦 *{department}*\n\n🔍 اختر الخدمة:"
        markup = get_services_by_department_markup(department)
        update_user_state(chat_id, state="selecting_service", pending_department=department)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    elif call.data.startswith("service_"):
        parts = call.data.split("_", 3)
        department = parts[1]
        service = parts[2]
        price = int(parts[3])
        
        order = record_order(chat_id, department, service, price)
        
        text = f"✅ *تم التسجيل #{order['id']}*\n\n"
        text += f"📦 {order['department']}\n"
        text += f"🛠️ {order['service']}\n"
        text += f"💰 {order['price']}ج\n\n"
        text += "⏳ رجوع تلقائي..."
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
        
        update_user_state(chat_id, state="main_menu", clear_pending=True)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
        
        scheduler.add_job(
            auto_return_to_main,
            'date',
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=1.5),
            args=[chat_id, msg_id]
        )
    
    elif call.data == "menu_reports":
        text = "📊 *التقارير والإحصائيات*\n\nاختر نطاق التقرير:"
        markup = get_reports_menu_markup()
        update_user_state(chat_id, state="reports_menu")
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    elif call.data == "rep_today":
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        orders = get_user_orders(chat_id)
        today_orders = [o for o in orders if str(o.get("date", "")) == today_str]
        
        total_rev = sum(o["price"] for o in today_orders)
        
        text = f"📆 *تقرير اليوم*\n"
        text += f"📅 {today_str}\n"
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += f"📦 عدد الطلبات: *{len(today_orders)}*\n"
        text += f"💰 إجمالي الإيرادات: *{total_rev} ج*\n\n"
        
        if today_orders:
            text += "*تفاصيل الطلبات:*\n"
            for o in today_orders:
                text += f"#{o['order_id_user']} • {o['service']} ({o['department']}) • {o['price']}ج • {o['time']}\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports"))
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    elif call.data == "rep_month":
        today = datetime.date.today()
        month_start = datetime.date(today.year, today.month, 1)
        
        orders = get_user_orders(chat_id)
        month_orders = []
        for o in orders:
            try:
                order_date = datetime.datetime.strptime(str(o["date"]), "%Y-%m-%d").date()
                if order_date >= month_start:
                    month_orders.append(o)
            except:
                continue
        
        total_rev = sum(o["price"] for o in month_orders)
        
        text = f"📈 *تقرير الشهر*\n"
        text += f"📅 {month_start.strftime('%B %Y')}\n"
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += f"📦 عدد الطلبات: *{len(month_orders)}*\n"
        text += f"💰 إجمالي الإيرادات: *{total_rev} ج*\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports"))
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    elif call.data == "export_csv":
        orders = get_user_orders(chat_id)
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        
        csv_buffer = io.StringIO()
        csv_buffer.write('\ufeff')
        writer = csv.writer(csv_buffer)
        writer.writerow(["رقم الطلب", "القسم", "نوع الخدمة", "السعر", "الوقت", "التاريخ"])
        for o in orders:
            writer.writerow([o["order_id_user"], o["department"], o["service"], o["price"], o["time"], o["date"]])
        
        csv_buffer.seek(0)
        bio = io.BytesIO(csv_buffer.getvalue().encode('utf-8'))
        bio.name = f"تقرير_{chat_id}_{today_str}.csv"
        
        text = "📤 جاري تصدير البيانات..."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports"))
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
        
        try:
            doc_msg = bot.send_document(chat_id, bio, caption="📤 تم تصدير البيانات بنجاح!")
            scheduler.add_job(
                lambda: bot.delete_message(chat_id, doc_msg.message_id),
                'date',
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=3)
            )
        except Exception as e:
            print(f"Error sending CSV: {e}")
        
        scheduler.add_job(
            lambda: bot.edit_message_text(
                chat_id=chat_id, 
                message_id=msg_id, 
                text="📊 *التقارير والإحصائيات*\n\nاختر نطاق التقرير:",
                reply_markup=get_reports_menu_markup()
            ),
            'date',
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=0.5)
        )

def auto_return_to_main(chat_id, msg_id):
    try:
        text = get_main_menu_text(chat_id)
        markup = get_departments_markup()
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
        update_user_state(chat_id, state="main_menu")
    except Exception:
        pass

# --- Message Handling ---
@bot.message_handler(commands=['start'])
def start_handler(message):
    chat_id = message.chat.id
    
    try:
        bot.delete_message(chat_id, message.message_id)
    except:
        pass
    
    data = get_user_state(chat_id)
    master_msg_id = data.get("master_msg_id")
    
    text = get_main_menu_text(chat_id)
    markup = get_departments_markup()
    
    if master_msg_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=master_msg_id, text=text, reply_markup=markup)
            clean_chat(chat_id, master_msg_id)
            return
        except:
            pass
    
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    update_user_state(chat_id, state="main_menu", master_msg_id=msg.message_id)
    clean_chat(chat_id, msg.message_id)

@bot.message_handler(commands=['restore'])
def restore_handler(message):
    """
    Handle /restore command:
    1. Delete /restore message immediately
    2. Show loading on master message
    3. Fetch ALL records from Google Sheets
    4. Rebuild SQLite with restored data (with proper Date parsing)
    5. Update master message with actual restored data
    """
    chat_id = message.chat.id
    
    # Step 1: Delete /restore command
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception as e:
        print(f"⚠️ Could not delete /restore: {e}")
    
    data = get_user_state(chat_id)
    master_msg_id = data.get("master_msg_id")
    
    # Step 2: Show loading state
    if master_msg_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=master_msg_id,
                text="🔄 *جاري استعادة البيانات...*\n\n⏳ جاري الاتصال بـ Google Sheets والمزامنة...",
                reply_markup=None
            )
        except:
            pass
    
    # Step 3 & 4: Restore from Google Sheets
    restored_count, restore_msg = restore_orders_from_sheets(target_chat_id=chat_id)
    
    # Step 5: Get fresh data and update master message
    text = get_main_menu_text(chat_id)
    markup = get_departments_markup()
    
    full_text = f"📥 *حالة الاستعادة*\n{restore_msg}\n\n{text}"
    
    if master_msg_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=master_msg_id,
                text=full_text,
                reply_markup=markup
            )
            update_user_state(chat_id, state="main_menu", clear_pending=True)
        except Exception as e:
            print(f"⚠️ Could not edit master: {e}")
            msg = bot.send_message(chat_id, full_text, reply_markup=markup)
            update_user_state(chat_id, state="main_menu", master_msg_id=msg.message_id)
            clean_chat(chat_id, msg.message_id)
    else:
        msg = bot.send_message(chat_id, full_text, reply_markup=markup)
        update_user_state(chat_id, state="main_menu", master_msg_id=msg.message_id)
        clean_chat(chat_id, msg.message_id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'sticker', 'voice', 'location', 'contact'])
def handle_all_messages(message):
    """Delete ANY message from user immediately"""
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    
    data = get_user_state(message.chat.id)
    master_msg_id = data.get("master_msg_id")
    if master_msg_id:
        clean_chat(message.chat.id, master_msg_id)

# --- Health Check Server ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# --- Startup ---
if __name__ == '__main__':
    print("🚀 Bot is starting...")
    
    fetch_services_from_google_sheets()
    
    print("✅ Bot is running — Date-safe restore + Single Message")
    bot.infinity_polling()
