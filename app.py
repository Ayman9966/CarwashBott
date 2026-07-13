"""
🚗 Car Service Tracker Bot — Render Production Version
Compatible with Python 3.14
"""

import logging
import json
import os
import csv
import asyncio
from datetime import datetime
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TimedOut, RetryAfter, BadRequest

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = os.getenv("DATA_FILE", "orders.json")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
if not RENDER_EXTERNAL_URL:
    raise ValueError("RENDER_EXTERNAL_URL environment variable is required! Example: https://carwashbott.onrender.com")

WEBHOOK_PATH = "/webhook"
PORT = int(os.environ.get("PORT", 10000))

os.makedirs(BACKUP_DIR, exist_ok=True)

# ==================== SERVICES DATA ====================
SERVICES = {
    "wash": {
        "label": "🚗 غسيل سيارات",
        "items": {
            "exterior":   {"name": "🚿 غسيل خارجي",      "price": 150},
            "interior":   {"name": "🧹 غسيل داخلي",      "price": 200},
            "full":       {"name": "✨ غسيل شامل",       "price": 300},
            "polish":     {"name": "🪞 تلميع كامل",      "price": 500},
            "engine":     {"name": "⚙️ غسيل محرك",       "price": 250},
        }
    },
    "diag": {
        "label": "🔍 كشف أعطال",
        "items": {
            "computer":   {"name": "💻 كشف كمبيوتر",     "price": 100},
            "mechanical": {"name": "🔧 كشف ميكانيكي",    "price": 150},
            "electrical": {"name": "⚡ كشف كهرباء",      "price": 150},
            "full_diag":  {"name": "🔍 كشف شامل",        "price": 300},
            "pre_purchase":{"name": "🛡️ فحص قبل الشراء", "price": 400},
        }
    }
}

# ==================== DATA LAYER ====================
class OrderStore:
    @staticmethod
    def load():
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    @staticmethod
    def save(orders):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)

    @staticmethod
    def add(service_key, service_name, price):
        orders = OrderStore.load()
        order = {
            "id": len(orders) + 1,
            "service_key": service_key,
            "service_name": service_name,
            "price": price,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "month": datetime.now().strftime("%Y-%m"),
        }
        orders.append(order)
        OrderStore.save(orders)
        return order

    @staticmethod
    def pop_last():
        orders = OrderStore.load()
        if orders:
            deleted = orders.pop()
            OrderStore.save(orders)
            return deleted
        return None

    @staticmethod
    def filter_by_date(date_str):
        return [o for o in OrderStore.load() if o["date"] == date_str]

    @staticmethod
    def filter_by_month(month_str):
        return [o for o in OrderStore.load() if o["month"] == month_str]

    @staticmethod
    def last_n(n=5):
        orders = OrderStore.load()
        return orders[-n:][::-1]

    @staticmethod
    def monthly_summary():
        monthly = defaultdict(lambda: {"total": 0, "count": 0})
        for o in OrderStore.load():
            monthly[o["month"]]["total"] += o["price"]
            monthly[o["month"]]["count"] += 1
        return dict(sorted(monthly.items(), reverse=True))


# ==================== BACKUP SYSTEM ====================
class BackupManager:
    @staticmethod
    def export_csv(filepath=None):
        orders = OrderStore.load()
        if not orders:
            return None
        if filepath is None:
            date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(BACKUP_DIR, f"backup_{date_str}.csv")
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["ID", "Service Key", "Service Name", "Price", "Date", "Time", "Month"])
            for o in orders:
                writer.writerow([
                    o["id"], o["service_key"], o["service_name"],
                    o["price"], o["date"], o["timestamp"][11:16], o["month"]
                ])
        return filepath

    @staticmethod
    def list_backups():
        if not os.path.exists(BACKUP_DIR):
            return []
        files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".csv")]
        return sorted(files, reverse=True)

    @staticmethod
    def restore_from_csv(filepath):
        if not os.path.exists(filepath):
            return False, "ملف غير موجود"
        try:
            orders = []
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    orders.append({
                        "id": int(row["ID"]),
                        "service_key": row["Service Key"],
                        "service_name": row["Service Name"],
                        "price": int(row["Price"]),
                        "timestamp": f"{row['Date']} {row['Time']}:00",
                        "date": row["Date"],
                        "month": row["Month"],
                    })
            OrderStore.save(orders)
            return True, f"تم استرجاع {len(orders)} طلب"
        except Exception as e:
            return False, f"خطأ: {str(e)}"

    @staticmethod
    def cleanup_old_backups(keep=30):
        backups = BackupManager.list_backups()
        for old in backups[keep:]:
            os.remove(os.path.join(BACKUP_DIR, old))


# ==================== UI BUILDERS ====================
class UI:
    @staticmethod
    def button(text, callback):
        return InlineKeyboardButton(text, callback_data=callback)

    @staticmethod
    def back_button():
        return [UI.button("🔙 رجوع", "back_main")]

    @staticmethod
    def service_menu(category):
        cat = SERVICES[category]
        buttons = [
            [UI.button(f"{v['name']} — {v['price']}ج", f"order_{category}_{k}")]
            for k, v in cat["items"].items()
        ]
        buttons.append(UI.back_button())
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def main_menu():
        return InlineKeyboardMarkup([
            [UI.button(SERVICES["wash"]["label"], "menu_wash"),
             UI.button(SERVICES["diag"]["label"], "menu_diag")],
            [UI.button("🗑️ حذف آخر طلب", "delete_confirm")],
            [UI.button("📊 تقرير اليوم", "report_today"),
             UI.button("📈 تقرير الشهر", "report_month")],
            [UI.button("📅 تقرير شهري", "report_monthly")],
            [UI.button("💾 باك اب", "backup_menu")],
        ])

    @staticmethod
    def back_only():
        return InlineKeyboardMarkup([UI.back_button()])

    @staticmethod
    def confirm_delete():
        return InlineKeyboardMarkup([
            [UI.button("✅ نعم، احذف", "delete_last")],
            [UI.button("❌ لا، تراجع", "back_main")]
        ])

    @staticmethod
    def confirm_price(service_name, default_price):
        return InlineKeyboardMarkup([
            [UI.button(f"✅ تأكيد ({default_price}ج)", "confirm_default_price")],
            UI.back_button()
        ])

    @staticmethod
    def backup_menu():
        return InlineKeyboardMarkup([
            [UI.button("📤 تصدير CSV", "backup_export")],
            [UI.button("📥 استرجاع باك اب", "backup_restore_list")],
            UI.back_button()
        ])

    @staticmethod
    def backup_list():
        backups = BackupManager.list_backups()
        if not backups:
            return InlineKeyboardMarkup([
                [UI.button("📝 مفيش باك اب", "backup_menu")],
                UI.back_button()
            ])
        buttons = []
        for b in backups[:10]:
            date_part = b.replace("backup_", "").replace(".csv", "")
            display = f"📄 {date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {date_part[9:11]}:{date_part[11:13]}"
            buttons.append([UI.button(display, f"restore_{b}")])
        buttons.append(UI.back_button())
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def confirm_restore(filename):
        return InlineKeyboardMarkup([
            [UI.button("✅ نعم، استرجع", f"confirm_restore_{filename}")],
            [UI.button("❌ لا، تراجع", "backup_restore_list")]
        ])


# ==================== REPORT BUILDERS ====================
class Reports:
    @staticmethod
    def header(title, subtitle=""):
        line = "━" * 18
        if subtitle:
            return title + "\n" + subtitle + line + "\n"
        return title + "\n" + line + "\n"

    @staticmethod
    def order_line(order):
        time_str = order["timestamp"][11:16]
        return "  `#" + str(order['id']) + "` " + order['service_name'] + " — *" + str(order['price']) + "ج* (" + time_str + ")\n"

    @staticmethod
    def category_summary(orders):
        summary = defaultdict(lambda: {"count": 0, "total": 0})
        for o in orders:
            cat_key = o["service_key"].split("_")[0]
            summary[cat_key]["count"] += 1
            summary[cat_key]["total"] += o["price"]
        return summary

    @staticmethod
    def overview():
        today = datetime.now().strftime("%Y-%m-%d")
        today_orders = OrderStore.filter_by_date(today)
        total = sum(o["price"] for o in today_orders)
        count = len(today_orders)
        last_orders = OrderStore.last_n(5)

        text = Reports.header("📊 *نظرة عامة — اليوم*", "📅 " + today + "\n")
        text += "💰 إجمالي الإيرادات: *" + str(total) + " ج*\n"
        text += "📦 عدد الطلبات: *" + str(count) + "*\n\n"

        cat_summary = Reports.category_summary(today_orders)
        if cat_summary:
            for cat_key, stats in cat_summary.items():
                label = SERVICES.get(cat_key, {}).get("label", cat_key)
                text += label + "\n"
                text += "  📦 " + str(stats['count']) + " عدد | 💰 " + str(stats['total']) + " ج\n\n"
        else:
            text += "📭 مفيش طلبات النهاردة\n\n"

        text += "🕐 *آخر 5 تسجيلات:*\n"
        if last_orders:
            for o in last_orders:
                text += Reports.order_line(o)
        else:
            text += "  مفيش تسجيلات\n"

        text += "\n📌 *اختار:*"
        return text

    @staticmethod
    def daily():
        today = datetime.now().strftime("%Y-%m-%d")
        orders = OrderStore.filter_by_date(today)
        total = sum(o["price"] for o in orders)
        text = Reports.header("📊 *تقرير اليوم*", "📅 " + today + "\n")
        text += "💰 الإجمالي: *" + str(total) + " ج*\n"
        text += "📦 الطلبات: *" + str(len(orders)) + "*\n\n"
        if orders:
            text += "📝 *التفاصيل:*\n"
            for o in orders:
                text += Reports.order_line(o)
        else:
            text += "📝 مفيش طلبات"
        return text

    @staticmethod
    def monthly():
        month = datetime.now().strftime("%Y-%m")
        orders = OrderStore.filter_by_month(month)
        total = sum(o["price"] for o in orders)
        text = Reports.header("📈 *تقرير الشهر: " + month + "*", "")
        text += "💰 الإجمالي: *" + str(total) + " ج*\n"
        text += "📦 الطلبات: *" + str(len(orders)) + "*\n\n"
        if orders:
            text += "📝 *آخر 10 طلبات:*\n"
            for o in orders[-10:][::-1]:
                text += "  `#" + str(o['id']) + "` " + o['service_name'] + " — " + str(o['price']) + "ج (" + o['date'] + ")\n"
        else:
            text += "📝 مفيش طلبات"
        return text

    @staticmethod
    def all_months():
        monthly = OrderStore.monthly_summary()
        text = Reports.header("📅 *التقرير الشهري*", "إجمالي الإيرادات لكل شهر:\n")
        if monthly:
            grand_total = sum(v["total"] for v in monthly.values())
            grand_count = sum(v["count"] for v in monthly.values())
            for month, stats in monthly.items():
                text += "📆 `" + month + "`: *" + str(stats['total']) + "ج* (" + str(stats['count']) + " طلب)\n"
            text += "━" * 18 + "\n"
            text += "💰 الإجمالي الكلي: *" + str(grand_total) + "ج*\n"
            text += "📦 إجمالي الطلبات: *" + str(grand_count) + "*"
        else:
            text += "📝 مفيش بيانات"
        return text

    @staticmethod
    def confirmation(order, action="تم التسجيل"):
        return (
            "✅ *" + action + " #" + str(order['id']) + "*\n\n"
            "🛠️ " + order['service_name'] + "\n"
            "💰 " + str(order['price']) + "ج\n"
            "🕐 " + datetime.now().strftime('%H:%M') + "\n\n"
            "⏳ رجوع تلقائي للقائمة بعد 2 ثانية..."
        )

    @staticmethod
    def price_prompt(service_name, default_price):
        return (
            "💰 *تأكيد السعر*\n\n"
            "🛠️ " + service_name + "\n"
            "السعر الافتراضي: *" + str(default_price) + "ج*\n\n"
            "✅ اضغط تأكيد أو اكتب سعر جديد خلال 10 ثواني..."
        )

    @staticmethod
    def delete_confirmation_preview():
        orders = OrderStore.load()
        if not orders:
            return "🗑️ *تأكيد الحذف*\n\nمفيش طلبات للحذف"
        last = orders[-1]
        return (
            "🗑️ *تأكيد الحذف*\n\n"
            "هل أنت متأكد من حذف آخر طلب؟\n\n"
            "`#" + str(last['id']) + "` " + last['service_name'] + "\n"
            "💰 " + str(last['price']) + "ج — " + last['date'] + " " + last['timestamp'][11:16]
        )


# ==================== SAFE API HELPERS ====================
async def safe_edit_message(context, chat_id, message_id, text, markup, max_retries=3):
    for attempt in range(max_retries):
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                parse_mode="Markdown", reply_markup=markup,
                read_timeout=30, write_timeout=30, connect_timeout=30,
            )
            return True
        except TimedOut:
            logger.warning(f"TimedOut edit (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
        except RetryAfter as e:
            logger.warning(f"Rate limited, retrying after {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            return await safe_edit_message(context, chat_id, message_id, text, markup, max_retries=1)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            raise
    return False


async def safe_edit_callback(query, text, markup, max_retries=3):
    for attempt in range(max_retries):
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return True
        except TimedOut:
            logger.warning(f"TimedOut callback (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
        except RetryAfter as e:
            logger.warning(f"Rate limited, retrying after {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            return await safe_edit_callback(query, text, markup, max_retries=1)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            raise
    return False


async def safe_send_document(context, chat_id, filepath, caption="", max_retries=3):
    for attempt in range(max_retries):
        try:
            with open(filepath, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f, caption=caption,
                    read_timeout=60, write_timeout=60, connect_timeout=30,
                )
            return True
        except TimedOut:
            logger.warning(f"TimedOut sending doc (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        except RetryAfter as e:
            logger.warning(f"Rate limited, retrying after {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            return await safe_send_document(context, chat_id, filepath, caption, max_retries=1)
    return False


# ==================== CLEAN CHAT HELPERS ====================
async def ensure_single_master_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup):
    chat_id = update.effective_chat.id
    master_msg_id = context.user_data.get("master_msg_id")

    if master_msg_id:
        try:
            if update.callback_query:
                await safe_edit_callback(update.callback_query, text, markup)
            else:
                await safe_edit_message(context, chat_id, master_msg_id, text, markup)
            return master_msg_id
        except Exception as e:
            logger.warning(f"Could not edit master msg {master_msg_id}: {e}")
            context.user_data.pop("master_msg_id", None)

    try:
        if update.message:
            msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            msg = await context.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)
        context.user_data["master_msg_id"] = msg.message_id
        return msg.message_id
    except Exception as e:
        logger.error(f"Failed to send new message: {e}")
        raise


async def auto_return_to_main(context: ContextTypes.DEFAULT_TYPE, chat_id: int, master_msg_id: int, delay: int = 2):
    await asyncio.sleep(delay)
    try:
        text = Reports.overview()
        markup = UI.main_menu()
        await safe_edit_message(context, chat_id, master_msg_id, text, markup)
    except Exception as e:
        logger.warning(f"Auto-return failed: {e}")


# ==================== PRICE CONFIRMATION FLOW ====================
async def start_price_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str, key: str):
    service = SERVICES[category]["items"][key]
    service_name = service["name"]
    default_price = service["price"]

    context.user_data["pending_order"] = {
        "category": category,
        "key": key,
        "service_name": service_name,
        "default_price": default_price,
    }
    context.user_data["awaiting_price"] = True

    text = Reports.price_prompt(service_name, default_price)
    master_msg_id = await ensure_single_master_message(update, context, text, UI.confirm_price(service_name, default_price))

    chat_id = update.effective_chat.id
    asyncio.create_task(_price_timeout(context, chat_id, master_msg_id))


async def _price_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, master_msg_id: int):
    await asyncio.sleep(10)

    if not context.user_data.get("awaiting_price"):
        return

    pending = context.user_data.get("pending_order")
    if not pending:
        return

    category = pending["category"]
    key = pending["key"]
    service_name = pending["service_name"]
    default_price = pending["default_price"]

    context.user_data.pop("awaiting_price", None)
    context.user_data.pop("pending_order", None)

    order = OrderStore.add(f"{category}_{key}", service_name, default_price)
    text = Reports.confirmation(order)

    try:
        await safe_edit_message(context, chat_id, master_msg_id, text, UI.back_only())
        asyncio.create_task(auto_return_to_main(context, chat_id, master_msg_id, delay=2))
    except Exception as e:
        logger.warning(f"Auto-confirm failed: {e}")


async def handle_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_price"):
        return False

    text = update.message.text.strip()
    chat_id = update.message.chat_id
    master_msg_id = context.user_data.get("master_msg_id")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except:
        pass

    pending = context.user_data.get("pending_order")
    if not pending:
        return True

    category = pending["category"]
    key = pending["key"]
    service_name = pending["service_name"]

    try:
        new_price = int(text)
        if new_price <= 0:
            raise ValueError("Price must be positive")
    except ValueError:
        if master_msg_id:
            error_text = (
                "❌ *سعر غير صحيح*\n\n"
                "اكتب رقم صحيح أو اضغط تأكيد\n\n"
                "🛠️ " + service_name + "\n"
                "السعر: *" + str(pending['default_price']) + "ج*"
            )
            await safe_edit_message(context, chat_id, master_msg_id, error_text, UI.confirm_price(service_name, pending['default_price']))
        return True

    context.user_data.pop("awaiting_price", None)
    context.user_data.pop("pending_order", None)

    order = OrderStore.add(f"{category}_{key}", service_name, new_price)
    text = Reports.confirmation(order)

    if master_msg_id:
        await safe_edit_message(context, chat_id, master_msg_id, text, UI.back_only())
        asyncio.create_task(auto_return_to_main(context, chat_id, master_msg_id, delay=2))

    return True


# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = Reports.overview()
    markup = UI.main_menu()
    await ensure_single_master_message(update, context, text, markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Failed to answer callback: {e}")

    data = query.data
    chat_id = query.message.chat_id

    if data == "back_main":
        context.user_data.pop("awaiting_price", None)
        context.user_data.pop("pending_order", None)
        return await start(update, context)

    if data == "confirm_default_price":
        pending = context.user_data.get("pending_order")
        if pending:
            context.user_data.pop("awaiting_price", None)
            category = pending["category"]
            key = pending["key"]
            service_name = pending["service_name"]
            default_price = pending["default_price"]
            context.user_data.pop("pending_order", None)

            order = OrderStore.add(f"{category}_{key}", service_name, default_price)
            text = Reports.confirmation(order)
            master_msg_id = await ensure_single_master_message(update, context, text, UI.back_only())
            asyncio.create_task(auto_return_to_main(context, chat_id, master_msg_id, delay=2))
        return

    if data == "delete_confirm":
        text = Reports.delete_confirmation_preview()
        if "مفيش" in text:
            master_msg_id = await ensure_single_master_message(update, context, text, UI.back_only())
            asyncio.create_task(auto_return_to_main(context, chat_id, master_msg_id, delay=2))
        else:
            await ensure_single_master_message(update, context, text, UI.confirm_delete())
        return

    if data.startswith("menu_"):
        category = data.replace("menu_", "")
        if category in SERVICES:
            label = SERVICES[category]["label"]
            text = label + "\n\nاضغط لتسجيل الطلب:"
            await ensure_single_master_message(update, context, text, UI.service_menu(category))
        return

    if data.startswith("order_"):
        parts = data.replace("order_", "").split("_", 1)
        if len(parts) == 2:
            category, key = parts
            if category in SERVICES and key in SERVICES[category]["items"]:
                await start_price_confirmation(update, context, category, key)
        return

    if data == "delete_last":
        deleted = OrderStore.pop_last()
        if deleted:
            text = (
                "🗑️ *تم الحذف*\n\n"
                "#" + str(deleted['id']) + " " + deleted['service_name'] + "\n"
                "💰 " + str(deleted['price']) + "ج\n\n"
                "⏳ رجوع تلقائي للقائمة بعد 2 ثانية..."
            )
        else:
            text = "🗑️ *مفيش طلبات للحذف*\n\n⏳ رجوع تلقائي..."
        master_msg_id = await ensure_single_master_message(update, context, text, UI.back_only())
        asyncio.create_task(auto_return_to_main(context, chat_id, master_msg_id, delay=2))
        return

    report_map = {
        "report_today":   Reports.daily(),
        "report_month":   Reports.monthly(),
        "report_monthly": Reports.all_months(),
    }

    if data in report_map:
        await ensure_single_master_message(update, context, report_map[data], UI.back_only())
        return

    if data == "backup_menu":
        text = "💾 *إدارة الباك اب*\n\nاختار الإجراء:"
        await ensure_single_master_message(update, context, text, UI.backup_menu())
        return

    if data == "backup_export":
        filepath = BackupManager.export_csv()
        if filepath:
            BackupManager.cleanup_old_backups(keep=30)
            await safe_send_document(context, chat_id, filepath, caption="📤 تصدير البيانات\n\nملف CSV بكل الطلبات.")
            text = "✅ *تم التصدير*\n\nملف CSV تم إرساله."
        else:
            text = "📝 *مفيش بيانات للتصدير*"
        await ensure_single_master_message(update, context, text, UI.backup_menu())
        return

    if data == "backup_restore_list":
        backups = BackupManager.list_backups()
        if backups:
            text = "📥 *استرجاع باك اب*\n\nاختار الملف:"
        else:
            text = "📝 *مفيش باك اب متاح*"
        await ensure_single_master_message(update, context, text, UI.backup_list())
        return

    if data.startswith("restore_"):
        filename = data.replace("restore_", "")
        filepath = os.path.join(BACKUP_DIR, filename)
        text = (
            "⚠️ *تأكيد الاسترجاع*\n\n"
            "ملف: `" + filename + "`\n"
            "هيتم استبدال كل البيانات الحالية!\n\n"
            "متأكد؟"
        )
        await ensure_single_master_message(update, context, text, UI.confirm_restore(filename))
        return

    if data.startswith("confirm_restore_"):
        filename = data.replace("confirm_restore_", "")
        filepath = os.path.join(BACKUP_DIR, filename)
        success, msg = BackupManager.restore_from_csv(filepath)
        if success:
            text = "✅ *تم الاسترجاع*\n\n" + msg
        else:
            text = "❌ *فشل الاسترجاع*\n\n" + msg
        await ensure_single_master_message(update, context, text, UI.backup_menu())
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_price_input(update, context):
        return
    try:
        await context.bot.delete_message(
            chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )
    except:
        pass


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}", exc_info=True)


# ==================== MAIN ENTRY POINT ====================
def main():
    logger.info("Starting bot with built-in webhook server...")

    # CRITICAL FIX for Python 3.14:
    # Explicitly create and set the event loop before PTB tries to get it
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)

    webhook_url = RENDER_EXTERNAL_URL + WEBHOOK_PATH

    logger.info(f"Webhook URL: {webhook_url}")
    logger.info(f"Listening on port: {PORT}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
