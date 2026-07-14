import datetime
import io
import csv
import sqlite3
import os
import time
import requests
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
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
                last_bot_msg_id INTEGER
            )
        ''')
        
        cursor.execute("PRAGMA table_info(user_states)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'pending_department' not in columns:
            cursor.execute("ALTER TABLE user_states ADD COLUMN pending_department TEXT")
        
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
        
        conn.commit()

init_db()

# --- Google Sheets Integration ---
def fetch_services_from_google_sheets():
    """Fetch services from Google Sheets via Apps Script"""
    global SERVICES_CACHE, CACHE_TIMESTAMP
    
    current_time = time.time()
    
    # Return cache if valid
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
        print(f"❌ Error fetching from Google Sheets: {e}")
        return SERVICES_CACHE  # Return cache on error

def log_order_to_google_sheets(department, service, price, chat_id):
    """Log order to Google Sheets via Apps Script"""
    try:
        now = datetime.datetime.now()
        payload = {
            "department": department,
            "service": service,
            "price": price,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "chatId": chat_id
        }
        
        response = requests.post(GOOGLE_APPS_SCRIPT_URL, json=payload, timeout=5)
        response.raise_for_status()
        result = response.json()
        
        if result.get("status") == "success":
            return result.get("num")
    except Exception as e:
        print(f"❌ Error logging to Google Sheets: {e}")
    
    return None

# --- Helper Functions ---
def get_user_state(chat_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state, pending_service, pending_department, pending_price, last_bot_msg_id FROM user_states WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        
        if not row:
            cursor.execute("INSERT INTO user_states (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
            return {
                "state": "main_menu",
                "pending_order": None,
                "pending_department": None,
                "last_bot_msg_id": None
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
            "last_bot_msg_id": row[4]
        }

def update_user_state(chat_id, state=None, pending_order=None, pending_department=None, last_bot_msg_id=None, clear_pending=False):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if state:
            cursor.execute("UPDATE user_states SET state = ? WHERE chat_id = ?", (state, chat_id))
        if last_bot_msg_id is not None:
            cursor.execute("UPDATE user_states SET last_bot_msg_id = ? WHERE chat_id = ?", (last_bot_msg_id, chat_id))
        if pending_department:
            cursor.execute("UPDATE user_states SET pending_department = ? WHERE chat_id = ?", (pending_department, chat_id))
        if pending_order:
            cursor.execute("UPDATE user_states SET pending_service = ?, pending_price = ? WHERE chat_id = ?", 
                           (pending_order["service"], pending_order["price"], chat_id))
        if clear_pending:
            cursor.execute("UPDATE user_states SET pending_service = NULL, pending_price = NULL, pending_department = NULL WHERE chat_id = ?", (chat_id,))
        conn.commit()

def record_order(chat_id, department, service, price):
    """Record order to both SQLite and Google Sheets"""
    now = datetime.datetime.now()
    
    # Get next order ID from SQLite
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(order_id_user) FROM orders WHERE chat_id = ?", (chat_id,))
        last_id = cursor.fetchone()[0]
        next_id = (last_id or 0) + 1
        
        # Log to SQLite
        cursor.execute('''
            INSERT INTO orders (chat_id, order_id_user, department, service, price, time, date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (chat_id, next_id, department, service, price, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d")))
        conn.commit()
    
    # Log to Google Sheets (async-like, don't block on failure)
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
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, order_id_user, department, service, price, time, date FROM orders WHERE chat_id = ? ORDER BY id ASC", (chat_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def get_departments_markup():
    """Create inline keyboard for departments"""
    services = fetch_services_from_google_sheets()
    
    # Group by department
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
    
    # Add reports button
    markup.add(types.InlineKeyboardButton("📊 التقارير", callback_data="menu_reports"))
    
    return markup

def get_services_by_department_markup(department):
    """Create inline keyboard for services in a department"""
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
    """Generate main menu overview"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    orders = get_user_orders(chat_id)
    today_orders = [o for o in orders if o["date"] == today_str]
    
    total_rev = sum(o["price"] for o in today_orders)
    total_count = len(today_orders)
    
    text = f"📊 *نظرة عامة — اليوم*\n"
    text += f"📅 {today_str}\n"
    text += "━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 إجمالي الإيرادات: *{total_rev} ج*\n"
    text += f"📦 عدد الطلبات: *{total_count}*\n\n"
    text += "📝 *ملخص الأقسام اليوم:*\n"
    
    if today_orders:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT department, COUNT(*), SUM(price)
                FROM orders
                WHERE chat_id = ? AND date = ?
                GROUP BY department
            """, (chat_id, today_str))
            rows = cursor.fetchall()
            
            for dept, count, subtotal in rows:
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

def ensure_master_message(chat_id, text, markup=None):
    """Ensure there's a master message to edit"""
    try:
        bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        print(f"Error sending master message: {e}")

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = get_user_state(chat_id)
    
    try:
        bot.answer_callback_query(call.id)
    except:
        pass
    
    # Main menu
    if call.data == "main_menu":
        text = get_main_menu_text(chat_id)
        markup = get_departments_markup()
        update_user_state(chat_id, state="main_menu", last_bot_msg_id=msg_id, clear_pending=True)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    # Department selection
    elif call.data.startswith("dept_"):
        department = call.data.replace("dept_", "")
        text = f"📦 *{department}*\n\n🔍 اختر الخدمة:"
        markup = get_services_by_department_markup(department)
        update_user_state(chat_id, state="selecting_service", pending_department=department, last_bot_msg_id=msg_id)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    # Service selection
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
        
        # Auto return to main menu after 2 seconds
        scheduler.add_job(
            auto_return_to_main,
            'date',
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=2),
            args=[chat_id, msg_id]
        )
    
    # Reports menu
    elif call.data == "menu_reports":
        text = "📊 *التقارير والإحصائيات*\n\nاختر نطاق التقرير:"
        markup = get_reports_menu_markup()
        update_user_state(chat_id, state="reports_menu", last_bot_msg_id=msg_id)
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    # Report - Today
    elif call.data == "rep_today":
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        orders = get_user_orders(chat_id)
        today_orders = [o for o in orders if o["date"] == today_str]
        
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
    
    # Report - Month
    elif call.data == "rep_month":
        today = datetime.date.today()
        month_start = datetime.date(today.year, today.month, 1)
        
        orders = get_user_orders(chat_id)
        month_orders = [o for o in orders if datetime.datetime.strptime(o["date"], "%Y-%m-%d").date() >= month_start]
        
        total_rev = sum(o["price"] for o in month_orders)
        
        text = f"📈 *تقرير الشهر*\n"
        text += f"📅 {month_start.strftime('%B %Y')}\n"
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += f"📦 عدد الطلبات: *{len(month_orders)}*\n"
        text += f"💰 إجمالي الإيرادات: *{total_rev} ج*\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports"))
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    
    # Export CSV
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
            bot.send_document(chat_id, bio, caption="📤 تم تصدير البيانات بنجاح!")
        except Exception as e:
            print(f"Error sending CSV: {e}")

def auto_return_to_main(chat_id, msg_id):
    """Auto-return to main menu"""
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
    text = get_main_menu_text(chat_id)
    markup = get_departments_markup()
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    update_user_state(chat_id, state="main_menu", last_bot_msg_id=msg.message_id)
    
    try:
        bot.delete_message(chat_id, message.message_id)
    except:
        pass

@bot.message_handler(content_types=['text'])
def handle_messages(message):
    """Handle general messages - keep chat clean"""
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

# --- Health Check Server ---
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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
    print(f"📊 Google Apps Script URL configured")
    
    # Pre-fetch services on startup
    fetch_services_from_google_sheets()
    
    print("✅ Bot is running...")
    bot.infinity_polling()
