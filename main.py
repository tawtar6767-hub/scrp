import logging
import re
import os
import asyncio
import tempfile
import shutil
import uuid
import time
import datetime as dt
import zipfile
import base64
import aiofiles
import sqlite3
from functools import wraps
from enum import Enum, auto
from dateutil.relativedelta import relativedelta 

# --- Third-party Libraries ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)
from telegram.request import HTTPXRequest
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.sessions import StringSession

# =================================================================
# CONFIGURATION & SETUP
# =================================================================
BOT_TOKEN = "8808287130:AAFfodgI7JfeRMlQOv3L9_g4ev18uHXboDs"  # <-- Put your bot token here
API_ID = 33441469
API_HASH = "95ec76c716581fd8231b1dbfda540239"

# Directories
SESSIONS_DIR = "user_sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

LOCAL_SCAN_FILE = "cards.txt" 
DB_FILE = "bot_database.db"

# --- Advanced Logging ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_errors.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- SQLite Database Initialization ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        # Global Deduplication Table
        c.execute('''
            CREATE TABLE IF NOT EXISTS global_cards (
                card TEXT PRIMARY KEY,
                date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Scraped Cards Table
        c.execute('''
            CREATE TABLE IF NOT EXISTS scraped_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card TEXT,
                encoded TEXT,
                channel_id TEXT,
                channel_name TEXT,
                message_id INTEGER,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        # Monitored Channels Table
        c.execute('''
            CREATE TABLE IF NOT EXISTS monitored_channels (
                channel_id TEXT PRIMARY KEY,
                channel_name TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1
            )
        ''')
        conn.commit()

init_db()

# --- Regex & Normalization ---
CARD_REGEX_STR = r"\b\d{15,16}[\s\|\\/-]+\d{2}[\s\|\\/-]+\d{2,4}[\s\|\\/-]+\d{3,4}\b"
CARD_PATTERN = re.compile(CARD_REGEX_STR)

# --- States ---
class State(Enum):
    MAIN_MENU = auto()
    LOGIN_PHONE = auto()
    LOGIN_CODE = auto()
    LOGIN_PASSWORD = auto()
    
    SCAN_SELECT_SOURCE = auto()
    SCAN_INPUT_USERNAME = auto()
    SCAN_SELECT_CHAT = auto()
    SCAN_SELECT_DIRECTION = auto()
    SCAN_SELECT_TYPE = auto()
    SCAN_GET_BIN = auto()
    SCAN_SELECT_MODE = auto()
    SCAN_GET_LIMIT = auto()
    SCAN_FILTER_EXPIRED = auto()
    SCAN_CONFIRMATION = auto()

    CLEAN_SELECT_METHOD = auto() 
    CLEAN_FILE_UPLOAD = auto()
    CLEAN_ASK_LOCAL_FILENAME = auto()
    CLEAN_GET_SPLIT_LIMIT = auto()

    # Scraper States
    SCRAPER_MAIN = auto()
    SCRAPER_SELECT_SOURCE = auto()
    SCRAPER_SELECT_CHAT = auto()
    SCRAPER_CONFIRM = auto()

    # BIN Filter States
    BIN_FILTER_GET_BINS = auto()
    BIN_FILTER_WAIT_FILE = auto()

# =================================================================
# HELPER FUNCTIONS
# =================================================================

def get_session_path(user_id):
    return os.path.join(SESSIONS_DIR, f"{user_id}.session")

def get_session_string(user_id):
    path = get_session_path(user_id)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read().strip()
    return None

async def delete_after_delay(message, delay=5):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass

def normalize_card(raw_card: str) -> str:
    return re.sub(r'[\s\|\\/-]+', '|', raw_card.strip())

def get_card_expiry_date(card_string: str) -> dt.date | None:
    try:
        parts = card_string.split('|')
        if len(parts) != 4: return None
        m, y_str = int(parts[1]), parts[2]
        if not (1 <= m <= 12): return None
        y = int(f"20{y_str}") if len(y_str) == 2 else int(y_str)
        return dt.date(y, m, 1) + relativedelta(months=1, days=-1)
    except: return None

def is_card_expired(card_string: str) -> bool:
    exp = get_card_expiry_date(card_string)
    return False if exp is None else dt.date.today() > exp

def is_card_in_db(card_string: str) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM global_cards WHERE card = ?", (card_string,))
        return c.fetchone() is not None

def save_cards_to_db(cards_list: list):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.executemany("INSERT OR IGNORE INTO global_cards (card) VALUES (?)", [(card,) for card in cards_list])
        conn.commit()

def create_zip_sync(zip_path, files):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for fp in files: 
            z.write(fp, os.path.basename(fp))

# =================================================================
# BASE64 CARD DECODER
# =================================================================

def decode_base64_card(encoded_text: str) -> str | None:
    """Base64 encoded card ကို decode လုပ်ပြီး card format ပြန်ပေးခြင်း"""
    try:
        decoded = base64.b64decode(encoded_text).decode('utf-8')
        card_pattern = re.compile(r'\d{15,16}[|/: ]\d{1,2}[|/: ]\d{2,4}[|/: ]\d{3,4}')
        if card_pattern.match(decoded):
            cleaned = re.sub(r'[/: ]', '|', decoded)
            return cleaned
    except Exception as e:
        logger.debug(f"Base64 decode failed: {e}")
    return None

def extract_cards_from_text(text: str) -> list:
    """Text ထဲက Base64 encoded နဲ့ plain card တွေကို ထုတ်ယူခြင်း"""
    cards = []
    
    # Base64 encoded card ရှာပါ
    base64_pattern = re.compile(r'[A-Za-z0-9+/=]{20,}')
    base64_matches = base64_pattern.findall(text)
    
    for encoded in base64_matches:
        card = decode_base64_card(encoded)
        if card:
            cards.append({
                'card': card,
                'encoded': encoded,
                'type': 'base64'
            })
    
    # Plain text card ရှာပါ
    plain_matches = CARD_PATTERN.findall(text)
    for card in plain_matches:
        cleaned = normalize_card(card)
        if cleaned not in [c['card'] for c in cards]:
            cards.append({
                'card': cleaned,
                'encoded': card,
                'type': 'plain'
            })
    
    return cards

# =================================================================
# SCRAPER DATABASE CLASS
# =================================================================

class ChannelScraperDB:
    def __init__(self):
        self.db_file = DB_FILE
    
    def save_scraped_cards(self, cards: list, channel_id: str, channel_name: str, message_id: int):
        """Scraped cards တွေကို database မှာ သိမ်းခြင်း"""
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            for card_data in cards:
                c.execute('''
                    INSERT OR IGNORE INTO scraped_cards 
                    (card, encoded, channel_id, channel_name, message_id)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    card_data['card'],
                    card_data['encoded'],
                    channel_id,
                    channel_name,
                    message_id
                ))
                c.execute('INSERT OR IGNORE INTO global_cards (card) VALUES (?)', (card_data['card'],))
            conn.commit()
    
    def get_scraped_cards(self, limit: int = 100) -> list:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('''
                SELECT card, encoded, channel_name, scraped_at 
                FROM scraped_cards 
                ORDER BY scraped_at DESC 
                LIMIT ?
            ''', (limit,))
            return c.fetchall()
    
    def get_scraped_cards_by_channel(self, channel_name: str) -> list:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('''
                SELECT card, encoded, scraped_at 
                FROM scraped_cards 
                WHERE channel_name = ?
                ORDER BY scraped_at DESC
            ''', (channel_name,))
            return c.fetchall()
    
    def get_total_scraped(self) -> int:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM scraped_cards')
            return c.fetchone()[0]
    
    def get_total_scraped_by_channel(self, channel_name: str) -> int:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM scraped_cards WHERE channel_name = ?', (channel_name,))
            return c.fetchone()[0]
    
    def clear_scraped_cards(self):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('DELETE FROM scraped_cards')
            conn.commit()
    
    def add_monitored_channel(self, channel_id: str, channel_name: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO monitored_channels (channel_id, channel_name, active)
                VALUES (?, ?, 1)
            ''', (channel_id, channel_name))
            conn.commit()
    
    def remove_monitored_channel(self, channel_id: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('UPDATE monitored_channels SET active = 0 WHERE channel_id = ?', (channel_id,))
            conn.commit()
    
    def get_monitored_channels(self) -> list:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute('SELECT channel_id, channel_name FROM monitored_channels WHERE active = 1')
            return c.fetchall()

scraper_db = ChannelScraperDB()

# =================================================================
# 1. LOGIN SYSTEM (MULTI-USER SUPPORTED)
# =================================================================
def build_login_keypad(user_input: str = "") -> InlineKeyboardMarkup:
    display = " ".join(user_input) if user_input else "Type Code Below"
    rows = [
        [InlineKeyboardButton(f"🔐 {display}", callback_data='login_noop')],
        [InlineKeyboardButton("1", callback_data='login_digit_1'), InlineKeyboardButton("2", callback_data='login_digit_2'), InlineKeyboardButton("3", callback_data='login_digit_3')],
        [InlineKeyboardButton("4", callback_data='login_digit_4'), InlineKeyboardButton("5", callback_data='login_digit_5'), InlineKeyboardButton("6", callback_data='login_digit_6')],
        [InlineKeyboardButton("7", callback_data='login_digit_7'), InlineKeyboardButton("8", callback_data='login_digit_8'), InlineKeyboardButton("9", callback_data='login_digit_9')],
        [InlineKeyboardButton("🔙 Del", callback_data='login_clear'), InlineKeyboardButton("0", callback_data='login_digit_0'), InlineKeyboardButton("✅ Submit", callback_data='login_submit')]
    ]
    return InlineKeyboardMarkup(rows)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    target_msg = update.callback_query.message if update.callback_query else update.message

    if os.path.exists(get_session_path(user_id)):
        await target_msg.reply_text("✅ <b>You are already Logged In.</b>", parse_mode=ParseMode.HTML)
        return await show_main_menu(target_msg, context)
    
    await target_msg.reply_text(
        "📱 <b>Telegram Login</b>\nPlease enter your phone number (e.g., +959...):", 
        parse_mode=ParseMode.HTML, 
        reply_markup=ReplyKeyboardRemove()
    )
    return State.LOGIN_PHONE

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    context.user_data['phone'] = update.message.text
    msg = await update.message.reply_text("🔄 Connecting to Telegram...")
    try:
        await client.connect()
        sent = await client.send_code_request(context.user_data['phone'])
        context.user_data['client'] = client 
        context.user_data['phone_code_hash'] = sent.phone_code_hash
        context.user_data['login_code_input'] = ''
        await msg.edit_text("📩 <b>Code Sent!</b>\nPlease enter the code:", reply_markup=build_login_keypad(), parse_mode=ParseMode.HTML)
        return State.LOGIN_CODE
    except Exception as e:
        logger.error(f"Login Phone Error: {e}")
        await msg.edit_text(f"❗️ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)
        await client.disconnect()
        return ConversationHandler.END

async def handle_login_keypad_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    action = query.data
    inp = context.user_data.get('login_code_input', '')

    if action == 'login_submit': return await process_login_code(update, context)
    elif action == 'login_clear': inp = inp[:-1]
    elif action.startswith('login_digit_'): 
        if len(inp) < 6: inp += action.split('_')[2]

    context.user_data['login_code_input'] = inp
    await query.edit_message_reply_markup(reply_markup=build_login_keypad(inp))
    return State.LOGIN_CODE

async def process_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    client = context.user_data['client']
    code = context.user_data['login_code_input']
    if not code: return State.LOGIN_CODE

    await query.edit_message_text("🔄 Verifying Code...")
    try:
        await client.sign_in(phone=context.user_data['phone'], code=code, phone_code_hash=context.user_data['phone_code_hash'])
        await query.message.reply_text("✅ <b>Login Successful!</b>", parse_mode=ParseMode.HTML)
        return await finalize_login(query.message, context, client)
    except SessionPasswordNeededError:
        await query.edit_message_text("🔐 <b>2FA Enabled.</b>\nPlease enter your Cloud Password:", parse_mode=ParseMode.HTML)
        return State.LOGIN_PASSWORD
    except Exception as e:
        logger.error(f"Login Code Error: {e}")
        await query.edit_message_text(f"❗️ Error: {e}")
        await client.disconnect()
        return ConversationHandler.END

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    client = context.user_data['client']
    await update.message.delete()
    msg = await context.bot.send_message(update.effective_chat.id, "🔄 Verifying Password...")
    try:
        await client.sign_in(password=update.message.text)
        await msg.edit_text("✅ <b>Login Successful!</b>", parse_mode=ParseMode.HTML)
        return await finalize_login(msg, context, client)
    except Exception as e:
        logger.error(f"Login Password Error: {e}")
        await msg.edit_text(f"❗️ Error: {e}")
        await client.disconnect()
        return ConversationHandler.END

async def finalize_login(message, context, client) -> State:
    user_id = message.chat.id
    session_file = get_session_path(user_id)
    with open(session_file, 'w') as f: 
        f.write(client.session.save())
    if 'client' in context.user_data:
        await context.user_data['client'].disconnect()
        del context.user_data['client']
    context.user_data.clear()
    return await show_main_menu(message, context)

# =================================================================
# 2. MAIN MENU & DASHBOARD
# =================================================================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    return await show_main_menu(update.message, context)

async def show_main_menu(message, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data.clear()
    kb = [
        [KeyboardButton("🚀 Start Scanner"), KeyboardButton("🧼 Clean / Split File")],
        [KeyboardButton("🕷️ Channel Scraper"), KeyboardButton("🎯 BIN Filter")],
        [KeyboardButton("📊 Active Tasks"), KeyboardButton("⚙️ Account Settings")]
    ]
    await message.reply_text(
        "🤖 <b>Bot Control Panel</b>\nSelect an action below:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=False),
        parse_mode=ParseMode.HTML
    )
    return State.MAIN_MENU

async def account_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    is_logged_in = os.path.exists(get_session_path(user_id))
    status = "🟢 <b>Connected</b>" if is_logged_in else "🔴 <b>Disconnected</b>"
    kb = [[InlineKeyboardButton("🚪 Logout", callback_data="logout")]] if is_logged_in else [[InlineKeyboardButton("➕ Login", callback_data="login")]]
    await update.message.reply_text(f"📡 <b>Account Status (User {user_id}):</b> {status}", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.MAIN_MENU

async def handle_login_logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    
    if q.data == "logout":
        session_path = get_session_path(user_id)
        if os.path.exists(session_path): 
            os.remove(session_path)
        kb = [
            [KeyboardButton("🚀 Start Scanner"), KeyboardButton("🧼 Clean / Split File")],
            [KeyboardButton("🕷️ Channel Scraper"), KeyboardButton("🎯 BIN Filter")],
            [KeyboardButton("📊 Active Tasks"), KeyboardButton("⚙️ Account Settings")]
        ]
        await q.message.reply_text("✅ Logged out successfully.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return State.MAIN_MENU
    else:
        return await login_start(update, context)

async def show_active_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    if 'active_tasks' not in context.bot_data: context.bot_data['active_tasks'] = {}
    user_tasks = [(tid, info) for tid, info in context.bot_data['active_tasks'].items() if info.get('user_id') == user_id]

    if not user_tasks:
        await update.message.reply_text("💤 No active background tasks.")
        return State.MAIN_MENU
    
    txt = "<b>📊 Your Active Tasks:</b>\n"
    kb = []
    for tid, info in user_tasks:
        txt += f"• {info['type']}: {info['status']}\n"
        kb.append([InlineKeyboardButton(f"⏹️ Stop: {info['type']}", callback_data=f"stop_task_{tid}")])
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.MAIN_MENU

async def stop_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = update.effective_user.id
    tid = q.data.split('_')[2]
    task_info = context.bot_data.get('active_tasks', {}).get(tid)
    if task_info and task_info.get('user_id') == user_id:
        context.bot_data['active_tasks'][tid]['stop_signal'] = True
        await q.answer("🛑 Stopping task...")
        await q.edit_message_text(f"⚠️ Task {tid} stopped.")
    else:
        await q.answer("Task not found or permission denied.")

# =================================================================
# 3. BACKGROUND TASK ENGINES
# =================================================================

async def run_scan_task(task_id, user_id, settings, bot, context_data):
    """Background Task: Scans for cards with Global Deduplication & Anti-Spam."""
    status_msg = await bot.send_message(
        user_id, 
        f"🚀 <b>Scan Started</b>\nTarget: {settings.get('chat_name', 'Unknown')}\nMode: {settings['scan_type']}", 
        parse_mode=ParseMode.HTML
    )

    cards_found = []
    scanned_count = 0
    duplicate_count = 0
    start_time = time.time()
    temp_dir = tempfile.mkdtemp()
    session_string = get_session_string(user_id)
    
    try:
        if settings['source_type'] == 'local_txt':
            async with aiofiles.open(LOCAL_SCAN_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                async for line in f:
                    if context_data['active_tasks'].get(task_id, {}).get('stop_signal'): break
                    for raw_card in CARD_PATTERN.findall(line):
                        clean_card = normalize_card(raw_card)
                        
                        if settings.get('filter_expired') and is_card_expired(clean_card): continue
                        if settings['scan_type'] == 'bin' and not any(clean_card.startswith(b) for b in settings['bins_to_find']): continue
                        
                        if is_card_in_db(clean_card):
                            duplicate_count += 1
                            continue

                        cards_found.append(clean_card)
                        if settings.get('limit') and len(cards_found) >= settings['limit']: break
                    
                    scanned_count += 1
                    if settings.get('limit') and len(cards_found) >= settings['limit']: break

        else:
            if not session_string: raise Exception("Session file not found. Please login again.")
            
            async with TelegramClient(StringSession(session_string), API_ID, API_HASH) as client:
                
                if settings['source_type'] == 'private_user':
                    await status_msg.edit_text("🔄 <b>Resolving User Cache...</b>", parse_mode=ParseMode.HTML)
                    await client.get_dialogs(limit=None)

                entity = await client.get_entity(settings['chat_id'])
                
                async for m in client.iter_messages(entity, reverse=(settings['scan_direction'] == 'reverse')):
                    if context_data['active_tasks'].get(task_id, {}).get('stop_signal'): break
                    
                    txt = getattr(m, 'text', "") or getattr(m, 'message', "") or ""
                    
                    for raw_card in CARD_PATTERN.findall(txt):
                        clean_card = normalize_card(raw_card)

                        if settings.get('filter_expired') and is_card_expired(clean_card): continue
                        if settings['scan_type'] == 'bin' and not any(clean_card.startswith(b) for b in settings['bins_to_find']): continue
                        
                        if is_card_in_db(clean_card):
                            duplicate_count += 1
                            continue

                        cards_found.append(clean_card)
                    
                    scanned_count += 1
                    
                    if scanned_count % 500 == 0:
                        await asyncio.sleep(0.5)
                    
                    if scanned_count % 100 == 0:
                        elapsed = time.time() - start_time
                        rate = scanned_count / elapsed if elapsed > 0 else 0
                        try:
                            await status_msg.edit_text(
                                f"📡 <b>Scanning...</b>\nTarget: {settings.get('chat_name')}\nFound (New): <code>{len(cards_found)}</code>\nSkipped (DB Dup): <code>{duplicate_count}</code>\nSpeed: {rate:.1f} msg/s",
                                parse_mode=ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Stop Task", callback_data=f"stop_task_{task_id}")]])
                            )
                        except FloodWaitError as e:
                            await asyncio.sleep(e.seconds)
                        except: pass
                    
                    if settings.get('limit') and len(cards_found) >= settings['limit']: break

        unique = list(set(cards_found))
        
        if unique:
            save_cards_to_db(unique)

        fname = os.path.join(temp_dir, f"Result_{str(settings.get('chat_name', 'Scan')).replace(' ','_')}_{len(unique)}.txt")
        with open(fname, 'w') as f: f.write('\n'.join(unique))
        
        caption_txt = f"✅ <b>Scan Complete</b>\nTarget: {settings.get('chat_name')}\nFound New: {len(unique)}\nSkipped (Already in DB): {duplicate_count}\nTime: {int(time.time()-start_time)}s"
        
        await bot.send_document(
            user_id, document=open(fname, 'rb'),
            caption=caption_txt,
            parse_mode=ParseMode.HTML
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Scan Task Error: {e}")
        await bot.send_message(user_id, f"❌ <b>Scan Failed:</b> {e}", parse_mode=ParseMode.HTML)
    finally:
        shutil.rmtree(temp_dir)
        if task_id in context_data['active_tasks']: del context_data['active_tasks'][task_id]

async def run_clean_task(task_id, user_id, input_file, split_limit, bot, context_data):
    """Background Task: Cleans file & Splits with Asyncio Threading."""
    status_msg = await bot.send_message(user_id, "🧼 <b>Cleaning Started...</b>", parse_mode=ParseMode.HTML)
    unique_cards = set()
    temp_dir = tempfile.mkdtemp()
    
    try:
        async with aiofiles.open(input_file, 'r', encoding='utf-8', errors='ignore') as f:
            async for line in f:
                if context_data['active_tasks'].get(task_id, {}).get('stop_signal'): break
                for raw_card in CARD_PATTERN.findall(line):
                    unique_cards.add(normalize_card(raw_card))
        
        unique_list = list(unique_cards)
        total = len(unique_list)
        
        if total == 0:
            await status_msg.edit_text("❌ No cards found in file.")
            return

        files_for_zip = []
        if split_limit > 0 and total > split_limit:
            await status_msg.edit_text(f"✅ Found {total:,} cards.\n📦 Splitting and sending...", parse_mode=ParseMode.HTML)
            for i in range(0, total, split_limit):
                if context_data['active_tasks'].get(task_id, {}).get('stop_signal'): break
                chunk = unique_list[i : i + split_limit]
                part_num = (i // split_limit) + 1
                part_path = os.path.join(temp_dir, f"Cleaned_Part_{part_num}.txt")
                async with aiofiles.open(part_path, 'w') as f: await f.write('\n'.join(chunk))
                files_for_zip.append(part_path)
                try:
                    await bot.send_document(user_id, document=open(part_path, 'rb'), caption=f"📄 Part {part_num}")
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    await bot.send_document(user_id, document=open(part_path, 'rb'), caption=f"📄 Part {part_num}")
            
            if not context_data['active_tasks'].get(task_id, {}).get('stop_signal'):
                await status_msg.edit_text("📦 Zipping all parts...")
                zip_path = os.path.join(temp_dir, f"Cleaned_All_{total}.zip")
                
                await asyncio.to_thread(create_zip_sync, zip_path, files_for_zip)
                
                await bot.send_document(user_id, document=open(zip_path, 'rb'), caption=f"✅ <b>Clean Complete!</b>\nTotal: {total:,}\nSplit: {split_limit}", parse_mode=ParseMode.HTML)
        else:
            out_path = os.path.join(temp_dir, f"Cleaned_Full_{total}.txt")
            async with aiofiles.open(out_path, 'w') as f: await f.write('\n'.join(unique_list))
            await bot.send_document(user_id, document=open(out_path, 'rb'), caption=f"✅ <b>Clean Complete!</b>\nTotal: {total:,}", parse_mode=ParseMode.HTML)
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Clean Task Error: {e}")
        await bot.send_message(user_id, f"❌ Error: {e}")
    finally:
        shutil.rmtree(temp_dir)
        if task_id in context_data['active_tasks']: del context_data['active_tasks'][task_id]

# =================================================================
# 4. CHANNEL SCRAPER ENGINES (SAVE TO FILE)
# =================================================================

async def run_scraper_task(task_id, user_id, chat_id, chat_name, bot, context_data):
    """Background Task: Scrapes base64 encoded cards from channel - Save to File"""
    
    status_msg = await bot.send_message(
        user_id,
        f"🕷️ <b>Channel Scraper Started</b>\n"
        f"📢 Channel: {chat_name}\n\n"
        f"🔄 Scanning messages...",
        parse_mode=ParseMode.HTML
    )
    
    cards_found = []
    scanned_count = 0
    start_time = time.time()
    temp_dir = tempfile.mkdtemp()
    
    try:
        session_string = get_session_string(user_id)
        if not session_string:
            raise Exception("Please login first!")
        
        async with TelegramClient(StringSession(session_string), API_ID, API_HASH) as client:
            # Get entity
            entity = await client.get_entity(int(chat_id))
            channel_id = str(entity.id)
            channel_name = entity.title or chat_name
            
            # Add to monitored channels
            scraper_db.add_monitored_channel(channel_id, channel_name)
            
            # Scan messages
            async for message in client.iter_messages(entity, limit=None):
                if context_data['active_tasks'].get(task_id, {}).get('stop_signal'):
                    break
                
                scanned_count += 1
                
                if message.text:
                    # Extract cards (including base64)
                    cards = extract_cards_from_text(message.text)
                    
                    if cards:
                        # Filter new cards only
                        new_cards = []
                        for card_data in cards:
                            if not is_card_in_db(card_data['card']):
                                new_cards.append(card_data)
                        
                        if new_cards:
                            cards_found.extend(new_cards)
                            scraper_db.save_scraped_cards(new_cards, channel_id, channel_name, message.id)
                
                # Progress update (every 100 messages)
                if scanned_count % 100 == 0:
                    elapsed = time.time() - start_time
                    rate = scanned_count / elapsed if elapsed > 0 else 0
                    try:
                        await status_msg.edit_text(
                            f"🕷️ <b>Channel Scraper</b>\n"
                            f"📢 {channel_name}\n"
                            f"📊 Scanned: {scanned_count} msgs\n"
                            f"💳 Found: {len(cards_found)} new\n"
                            f"⚡ {rate:.1f} msg/s\n\n"
                            f"⏹️ <code>/stop_scrape</code> to stop",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_task_{task_id}")]
                            ])
                        )
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                    except:
                        pass
                
                # Anti-spam
                if scanned_count % 50 == 0:
                    await asyncio.sleep(0.1)
            
            # =========================================================
            # SEND ALL CARDS AS A SINGLE FILE
            # =========================================================
            
            if cards_found:
                # Create a single file with all cards
                fname = os.path.join(temp_dir, f"Scraped_{channel_name.replace(' ', '_')}_{len(cards_found)}.txt")
                
                with open(fname, 'w', encoding='utf-8') as f:
                    # Header
                    f.write("=" * 60 + "\n")
                    f.write(f"SCRAPED CARDS FROM: {channel_name}\n")
                    f.write(f"DATE: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"TOTAL: {len(cards_found)} cards\n")
                    f.write("=" * 60 + "\n\n")
                    
                    # Write each card with details
                    for i, card_data in enumerate(cards_found, 1):
                        f.write(f"{i}. Card: {card_data['card']}\n")
                        f.write(f"   Encoded: {card_data['encoded']}\n")
                        f.write(f"   Type: {card_data['type']}\n")
                        f.write("-" * 40 + "\n")
                    
                    # Write clean cards only (for easy copy-paste)
                    f.write("\n" + "=" * 60 + "\n")
                    f.write("CLEAN CARDS (Copy-Paste Ready):\n")
                    f.write("=" * 60 + "\n\n")
                    for card_data in cards_found:
                        f.write(f"{card_data['card']}\n")
                
                # Send the file
                await bot.send_document(
                    user_id,
                    document=open(fname, 'rb'),
                    caption=(
                        f"✅ <b>Scraping Complete!</b>\n"
                        f"📢 {channel_name}\n"
                        f"💳 Total Cards: <b>{len(cards_found)}</b>\n"
                        f"📊 Scanned: {scanned_count} messages\n"
                        f"⏱️ Time: {int(time.time() - start_time)}s\n\n"
                        f"<i>All cards are in the file above</i>"
                    ),
                    parse_mode=ParseMode.HTML
                )
                
            else:
                await status_msg.edit_text(
                    f"✅ <b>Scraping Complete!</b>\n"
                    f"No new cards found in {channel_name}\n"
                    f"📊 Scanned: {scanned_count} messages",
                    parse_mode=ParseMode.HTML
                )
            
            await status_msg.delete()
    
    except Exception as e:
        logger.error(f"Scraper Task Error: {e}")
        await bot.send_message(
            user_id,
            f"❌ <b>Scraper Error:</b>\n{str(e)}",
            parse_mode=ParseMode.HTML
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if task_id in context_data['active_tasks']:
            del context_data['active_tasks'][task_id]

# =================================================================
# 5. SCRAPER WIZARD (Channel Selection Like Scanner)
# =================================================================

async def start_scraper_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Start scraper wizard with channel selection"""
    user_id = update.effective_user.id
    
    if not os.path.exists(get_session_path(user_id)):
        msg_text = "⚠️ Please <b>Login</b> first to use Channel Scraper."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
        return State.MAIN_MENU
    
    kb = [
        [InlineKeyboardButton("📢 Channel", callback_data="scraper_source_channel")],
        [InlineKeyboardButton("👥 Group", callback_data="scraper_source_group")],
        [InlineKeyboardButton("🔄 Monitored Channels", callback_data="scraper_monitored")],
        [InlineKeyboardButton("📊 Scraped Cards", callback_data="scraper_show_cards")],
        [InlineKeyboardButton("📥 Export All", callback_data="scraper_export_all")],
        [InlineKeyboardButton("📅 Auto Export (24h)", callback_data="scraper_auto_export")],
        [InlineKeyboardButton("🗑️ Clear Scraped", callback_data="scraper_clear_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="scraper_cancel")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "🕷️ <b>Channel Scraper</b>\n\n"
            "Scrape base64 encoded cards from channels/groups.\n"
            "All cards will be saved to a <b>single file</b>.\n\n"
            "<b>Options:</b>\n"
            "• Scrape new channel/group\n"
            "• Export all scraped cards\n"
            "• Auto export last 24 hours",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            "🕷️ <b>Channel Scraper</b>\n\n"
            "Scrape base64 encoded cards from channels/groups.\n"
            "All cards will be saved to a <b>single file</b>.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
    return State.SCRAPER_MAIN

async def scraper_select_source_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Select channel/group from list (like scanner)"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    stype = query.data.split('_')[2]  # channel or group
    context.user_data['scraper_settings'] = {'source_type': stype}
    
    msg = await query.edit_message_text("🔄 <b>Fetching Chats...</b>", parse_mode=ParseMode.HTML)
    try:
        session_string = get_session_string(user_id)
        if not session_string:
            await msg.edit_text("❌ Session error. Please login again.")
            return State.MAIN_MENU

        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        async with client:
            dialogs = await client.get_dialogs()
        
        chats = [d for d in dialogs if (stype == 'channel' and d.is_channel) or (stype == 'group' and d.is_group)]
        if not chats:
            await msg.edit_text(
                "❌ No chats found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="scraper_back")]])
            )
            return State.SCRAPER_MAIN
            
        context.user_data['scraper_chats'] = sorted([{'id': d.id, 'name': d.name} for d in chats], key=lambda x: x['name'])
        context.user_data['scraper_chat_page'] = 0
        await show_scraper_chat_page(update, context)
        return State.SCRAPER_SELECT_CHAT
    except Exception as e:
        await msg.edit_text(f"❗️ Error: {e}")
        return State.SCRAPER_MAIN

async def show_scraper_chat_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated chat list (like scanner)"""
    query = update.callback_query
    chats = context.user_data['scraper_chats']
    page = context.user_data.get('scraper_chat_page', 0)
    limit = 24
    start, end = page * limit, (page + 1) * limit
    current_chats = chats[start:end]
    
    btns = []
    for i in range(0, len(current_chats), 2):
        row_items = current_chats[i:i+2]
        row = [InlineKeyboardButton(c['name'], callback_data=f"scraper_chat_{c['id']}") for c in row_items]
        btns.append(row)

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️ Prev", callback_data="scraper_page_prev"))
    if end < len(chats): nav.append(InlineKeyboardButton("Next ▶️", callback_data="scraper_page_next"))
    if nav: btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="scraper_back")])
    await query.edit_message_text(
        f"🕷️ <b>Select Channel/Group</b> (Page {page+1}):\n\n"
        f"<i>Bot will scrape base64 encoded cards and save to file</i>",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode=ParseMode.HTML
    )

async def scraper_chat_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    act = query.data.split('_')[2]  # prev or next
    context.user_data['scraper_chat_page'] += 1 if act == 'next' else -1
    await show_scraper_chat_page(update, context)
    return State.SCRAPER_SELECT_CHAT

async def scraper_select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Confirm selected chat and start scraper"""
    query = update.callback_query
    await query.answer()
    
    chat_id = int(query.data.split('_')[2])
    chat_name = next((c['name'] for c in context.user_data['scraper_chats'] if c['id'] == chat_id), "Unknown")
    
    context.user_data['scraper_settings']['chat_id'] = chat_id
    context.user_data['scraper_settings']['chat_name'] = chat_name
    
    # Show confirmation
    kb = [
        [InlineKeyboardButton("🚀 Start Scraping", callback_data="scraper_start")],
        [InlineKeyboardButton("🔙 Back", callback_data="scraper_back_to_chats")]
    ]
    
    await query.edit_message_text(
        f"🕷️ <b>Confirm Scraper</b>\n\n"
        f"📢 Channel: <b>{chat_name}</b>\n"
        f"🆔 ID: <code>{chat_id}</code>\n\n"
        f"<i>Bot will scan all messages and extract base64 encoded cards</i>\n"
        f"<i>All cards will be saved to a single file</i>\n\n"
        f"⚠️ This may take time for large channels.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML
    )
    return State.SCRAPER_CONFIRM

async def scraper_launch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Launch scraper background task"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    settings = context.user_data.get('scraper_settings', {})
    chat_id = settings.get('chat_id')
    chat_name = settings.get('chat_name', 'Unknown')
    
    task_id = str(uuid.uuid4())[:8]
    if 'active_tasks' not in context.bot_data:
        context.bot_data['active_tasks'] = {}
    
    context.bot_data['active_tasks'][task_id] = {
        'type': 'Scraper',
        'status': 'Running',
        'stop_signal': False,
        'user_id': user_id,
        'channel': chat_name
    }
    
    await query.message.delete()
    
    msg = await query.message.reply_text(
        f"✅ <b>Scraper Started!</b>\n"
        f"📢 Channel: {chat_name}\n"
        f"🆔 Task: <code>{task_id}</code>\n\n"
        f"⏹️ Use <code>/stop_scrape</code> to stop\n"
        f"<i>Cards will be saved to a single file</i>",
        parse_mode=ParseMode.HTML
    )
    
    # Run background task
    asyncio.create_task(
        run_scraper_task(
            task_id,
            user_id,
            chat_id,
            chat_name,
            context.bot,
            context.bot_data
        )
    )
    
    await asyncio.create_task(delete_after_delay(msg, 5))
    return await show_main_menu(query.message, context)

# =================================================================
# 6. SCRAPER COMMANDS & EXPORT
# =================================================================

async def scraper_show_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Show scraped cards with export options"""
    
    # Handle both callback query and direct command
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message = query.message
    else:
        message = update.message
    
    total = scraper_db.get_total_scraped()
    cards = scraper_db.get_scraped_cards(20)
    
    if not cards:
        await message.reply_text(
            "📭 <b>No scraped cards found!</b>\n\n"
            "Use scraper to start scraping base64 encoded cards.\n"
            "All cards will be saved as a single file.",
            parse_mode=ParseMode.HTML
        )
        return State.MAIN_MENU
    
    # Group by channel
    channels = {}
    for card, encoded, channel, date in cards:
        if channel not in channels:
            channels[channel] = 0
        channels[channel] += 1
    
    channel_list = "\n".join([f"• {ch}: {count} cards" for ch, count in list(channels.items())[:5]])
    if len(channels) > 5:
        channel_list += f"\n… and {len(channels) - 5} more channels"
    
    text = f"📊 <b>Scraped Cards: {total}</b>\n"
    text += "─" * 30 + "\n\n"
    
    # Show first 10 cards
    for i, (card, encoded, channel, date) in enumerate(cards[:10], 1):
        text += f"{i}. <code>{card}</code>\n"
        text += f"   📢 {channel}\n"
        text += f"   🕐 {date[:16]}\n\n"
    
    if len(cards) > 10:
        text += f"… and {len(cards) - 10} more\n"
    
    text += "\n<b>📁 Channels:</b>\n" + channel_list + "\n"
    
    kb = [
        [InlineKeyboardButton("📥 Export All", callback_data="scraper_export_all")],
        [InlineKeyboardButton("📅 Export Last 24h", callback_data="scraper_auto_export")],
        [InlineKeyboardButton("🗑️ Clear All", callback_data="scraper_clear_confirm")],
        [InlineKeyboardButton("🔙 Back", callback_data="scraper_back")]
    ]
    
    if update.callback_query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCRAPER_MAIN

async def scraper_export_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export all scraped cards to a single file"""
    query = update.callback_query
    await query.answer()
    
    cards = scraper_db.get_scraped_cards(100000)  # Get all
    
    if not cards:
        await query.edit_message_text("📭 No cards to export!")
        return
    
    temp_dir = tempfile.mkdtemp()
    fname = os.path.join(temp_dir, f"scraped_cards_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.txt")
    
    with open(fname, 'w', encoding='utf-8') as f:
        # Header
        f.write("=" * 70 + "\n")
        f.write(f"SCRAPED CARDS EXPORT\n")
        f.write(f"Date: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total: {len(cards)} cards\n")
        f.write("=" * 70 + "\n\n")
        
        # Cards with details
        f.write("DETAILED CARDS:\n")
        f.write("-" * 70 + "\n\n")
        for i, (card, encoded, channel, date) in enumerate(cards, 1):
            f.write(f"{i}. CARD: {card}\n")
            f.write(f"   ENCODED: {encoded}\n")
            f.write(f"   CHANNEL: {channel}\n")
            f.write(f"   DATE: {date}\n")
            f.write("-" * 40 + "\n")
        
        # Clean cards only
        f.write("\n" + "=" * 70 + "\n")
        f.write("CLEAN CARDS ONLY (Copy-Paste Ready):\n")
        f.write("=" * 70 + "\n\n")
        for card, encoded, channel, date in cards:
            f.write(f"{card}\n")
    
    await query.message.reply_document(
        document=open(fname, 'rb'),
        caption=f"📥 <b>Scraped Cards Export</b>\n"
                f"Total: {len(cards)} cards\n"
                f"Date: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"<i>File contains both detailed and clean cards</i>",
        parse_mode=ParseMode.HTML
    )
    
    shutil.rmtree(temp_dir, ignore_errors=True)
    await query.edit_message_text("✅ Export completed!")

async def scraper_auto_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-export scraped cards from last 24 hours"""
    query = update.callback_query
    await query.answer()
    
    # Get cards from last 24 hours
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''
            SELECT card, encoded, channel_name, scraped_at 
            FROM scraped_cards 
            WHERE scraped_at >= datetime('now', '-1 day')
            ORDER BY scraped_at DESC
        ''')
        cards = c.fetchall()
    
    if not cards:
        await query.edit_message_text("📭 No cards from last 24 hours!")
        return
    
    temp_dir = tempfile.mkdtemp()
    fname = os.path.join(temp_dir, f"auto_export_{dt.datetime.now().strftime('%Y%m%d')}.txt")
    
    with open(fname, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write(f"AUTO EXPORT - {dt.datetime.now().strftime('%Y-%m-%d')}\n")
        f.write("=" * 70 + "\n\n")
        
        # Group by channel
        channels = {}
        for card, encoded, channel, date in cards:
            if channel not in channels:
                channels[channel] = []
            channels[channel].append((card, encoded, date))
        
        for channel, card_list in channels.items():
            f.write(f"\n📢 CHANNEL: {channel}\n")
            f.write("-" * 50 + "\n")
            for card, encoded, date in card_list:
                f.write(f"{card}\n")
        
        # Clean cards only at the end
        f.write("\n" + "=" * 70 + "\n")
        f.write("ALL CARDS (Copy-Paste Ready):\n")
        f.write("=" * 70 + "\n\n")
        for card, encoded, channel, date in cards:
            f.write(f"{card}\n")
    
    await query.message.reply_document(
        document=open(fname, 'rb'),
        caption=f"📊 <b>Auto Export - Last 24 Hours</b>\n"
                f"Total: {len(cards)} cards\n"
                f"Channels: {len(channels)}\n"
                f"Date: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        parse_mode=ParseMode.HTML
    )
    
    shutil.rmtree(temp_dir, ignore_errors=True)
    await query.edit_message_text("✅ Auto export completed!")

async def scraper_clear_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear scraped cards"""
    query = update.callback_query
    await query.answer()
    
    total = scraper_db.get_total_scraped()
    scraper_db.clear_scraped_cards()
    
    await query.edit_message_text(
        f"🗑️ <b>Cleared!</b>\n"
        f"Removed {total} scraped cards.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="scraper_back")]
        ])
    )

async def scraper_monitored_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show monitored channels"""
    query = update.callback_query
    await query.answer()
    
    channels = scraper_db.get_monitored_channels()
    
    if not channels:
        await query.edit_message_text(
            "📋 <b>No monitored channels</b>\n\n"
            "Scrape a channel to add it to the list.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="scraper_back")]
            ])
        )
        return
    
    text = "📋 <b>Monitored Channels</b>\n"
    text += "─" * 30 + "\n\n"
    
    for channel_id, channel_name in channels:
        count = scraper_db.get_total_scraped_by_channel(channel_name)
        text += f"• {channel_name}\n"
        text += f"  🆔 <code>{channel_id}</code>\n"
        text += f"  💳 {count} cards\n\n"
    
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)

async def scraper_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Back to scraper main"""
    query = update.callback_query
    await query.answer()
    
    return await start_scraper_wizard(update, context)

async def scraper_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Cancel scraper"""
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    
    msg = await query.message.reply_text("❌ Cancelled.")
    await asyncio.create_task(delete_after_delay(msg, 5))
    return await show_main_menu(query.message, context)

async def stop_scraper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop scraper task"""
    user_id = update.effective_user.id
    
    stopped = 0
    for task_id, task_info in context.bot_data.get('active_tasks', {}).items():
        if task_info.get('user_id') == user_id and task_info.get('type') == 'Scraper':
            context.bot_data['active_tasks'][task_id]['stop_signal'] = True
            stopped += 1
    
    if stopped > 0:
        await update.message.reply_text(f"🛑 Stopped {stopped} scraper task(s)!")
    else:
        await update.message.reply_text("💤 No active scraper tasks found.")

# =================================================================
# 7. BIN FILTER FROM FILE FEATURE
# =================================================================

async def bin_filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Start BIN filter process"""
    user_id = update.effective_user.id
    
    # Check login
    if not os.path.exists(get_session_path(user_id)):
        await update.message.reply_text(
            "⚠️ Please <b>Login</b> first to use BIN Filter.",
            parse_mode=ParseMode.HTML
        )
        return State.MAIN_MENU
    
    kb = [
        [InlineKeyboardButton("📤 Upload File", callback_data="binfilter_upload")],
        [InlineKeyboardButton("📂 Use Server File", callback_data="binfilter_server")],
        [InlineKeyboardButton("❌ Cancel", callback_data="binfilter_cancel")]
    ]
    
    await update.message.reply_text(
        "🎯 <b>BIN Filter</b>\n\n"
        "Filter cards from a file by specific BINs.\n\n"
        "<b>How to use:</b>\n"
        "1️⃣ Upload a file containing cards\n"
        "2️⃣ Enter BINs you want to filter (e.g., 411111, 522222)\n"
        "3️⃣ Bot will extract matching cards\n\n"
        "<b>Supported formats:</b>\n"
        "• cc|mm|yy|cvv\n"
        "• cc/mm/yy/cvv\n"
        "• cc mm yy cvv\n"
        "• Base64 encoded cards",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.HTML
    )
    return State.BIN_FILTER_GET_BINS

async def bin_filter_get_bins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Get BINs from user input"""
    query = update.callback_query
    await query.answer()
    
    context.user_data['binfilter_source'] = query.data.split('_')[1]  # upload or server
    
    await query.edit_message_text(
        "🎯 <b>Enter BINs</b>\n\n"
        "Send BINs you want to filter (comma separated):\n"
        "Example: <code>411111, 522222, 412345</code>\n\n"
        "<i>You can enter multiple BINs</i>\n"
        "<i>Only first 6 digits will be used</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="binfilter_back")]
        ])
    )
    return State.BIN_FILTER_GET_BINS

async def bin_filter_handle_bins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Handle BIN input and ask for file"""
    bins_text = update.message.text.strip()
    
    # Extract BINs (6 digits)
    bin_pattern = re.compile(r'\b\d{6}\b')
    bins = bin_pattern.findall(bins_text)
    
    if not bins:
        await update.message.reply_text(
            "❌ <b>Invalid BINs!</b>\n\n"
            "Please enter valid 6-digit BINs.\n"
            "Example: <code>411111, 522222, 412345</code>",
            parse_mode=ParseMode.HTML
        )
        return State.BIN_FILTER_GET_BINS
    
    # Remove duplicates
    bins = list(set(bins))
    context.user_data['binfilter_bins'] = bins
    
    # Show summary
    msg = f"""✅ <b>BINs Received</b>
    
📊 Total BINs: <b>{len(bins)}</b>
🔢 BINs: <code>{', '.join(bins)}</code>

📤 <b>Now send the file:</b>
• Upload a .txt file
• Or use server file with /binfilter_server
"""
    
    kb = [
        [InlineKeyboardButton("📤 Upload File", callback_data="binfilter_upload_ready")],
        [InlineKeyboardButton("📂 Use Server File", callback_data="binfilter_server_ready")],
        [InlineKeyboardButton("🔙 Back", callback_data="binfilter_back_bins")]
    ]
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.BIN_FILTER_WAIT_FILE

async def bin_filter_upload_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """User ready to upload file"""
    query = update.callback_query
    await query.answer()
    context.user_data['binfilter_source'] = 'upload'
    
    await query.edit_message_text(
        "📤 <b>Upload Your File</b>\n\n"
        "Send any .txt file containing cards.\n"
        "Max size: 10MB\n\n"
        "<i>Supported formats:</i>\n"
        "• cc|mm|yy|cvv\n"
        "• cc/mm/yy/cvv\n"
        "• Base64 encoded",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="binfilter_back_to_bins")]
        ])
    )
    return State.BIN_FILTER_WAIT_FILE

async def bin_filter_server_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """User wants to use server file"""
    query = update.callback_query
    await query.answer()
    context.user_data['binfilter_source'] = 'server'
    
    await query.edit_message_text(
        "📂 <b>Server File</b>\n\n"
        "Enter the filename on server:\n"
        "Example: <code>cards.txt</code> or <code>/path/to/file.txt</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="binfilter_back_to_bins")]
        ])
    )
    return State.BIN_FILTER_WAIT_FILE

async def bin_filter_handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Handle uploaded file or server file"""
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("🔄 <b>Processing file...</b>", parse_mode=ParseMode.HTML)
    
    temp_dir = tempfile.mkdtemp()
    input_file = None
    
    try:
        # Check source type
        if context.user_data.get('binfilter_source') == 'upload':
            # Handle uploaded file
            if not update.message.document:
                await status_msg.edit_text("❌ Please upload a file!")
                return State.BIN_FILTER_WAIT_FILE
            
            doc = update.message.document
            if not doc.file_name.endswith('.txt'):
                await status_msg.edit_text("❌ Please upload a .txt file!")
                return State.BIN_FILTER_WAIT_FILE
            
            input_file = os.path.join(temp_dir, doc.file_name or "uploaded.txt")
            file_obj = await doc.get_file()
            await file_obj.download_to_drive(input_file)
            
        else:
            # Handle server file
            filename = update.message.text.strip()
            if not os.path.exists(filename):
                await status_msg.edit_text(f"❌ File not found: <code>{filename}</code>", parse_mode=ParseMode.HTML)
                return State.BIN_FILTER_WAIT_FILE
            input_file = filename
        
        # Get BINs
        bins = context.user_data.get('binfilter_bins', [])
        if not bins:
            await status_msg.edit_text("❌ No BINs found. Please start over.")
            return State.MAIN_MENU
        
        # Process file and filter by BINs
        matched_cards = []
        total_cards = 0
        base64_cards = 0
        
        with open(input_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
            # Extract all cards
            all_cards = CARD_PATTERN.findall(content)
            
            for raw_card in all_cards:
                total_cards += 1
                clean_card = normalize_card(raw_card)
                
                # Check if card starts with any BIN
                if any(clean_card.startswith(bin_prefix) for bin_prefix in bins):
                    matched_cards.append(clean_card)
            
            # Also check for Base64 encoded cards
            base64_pattern = re.compile(r'[A-Za-z0-9+/=]{20,}')
            base64_matches = base64_pattern.findall(content)
            
            for encoded in base64_matches:
                card = decode_base64_card(encoded)
                if card:
                    base64_cards += 1
                    if any(card.startswith(bin_prefix) for bin_prefix in bins):
                        if card not in matched_cards:
                            matched_cards.append(card)
        
        if not matched_cards:
            await status_msg.edit_text(
                f"📭 <b>No matching cards found!</b>\n\n"
                f"📊 Total cards: {total_cards}\n"
                f"🎯 BINs: {len(bins)}\n\n"
                f"<i>Try different BINs or check file format.</i>",
                parse_mode=ParseMode.HTML
            )
            return State.MAIN_MENU
        
        # Create output file
        output_file = os.path.join(temp_dir, f"filtered_{len(matched_cards)}_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.txt")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write(f"FILTERED CARDS BY BIN\n")
            f.write(f"Date: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"BINs: {', '.join(bins)}\n")
            f.write(f"Total: {len(matched_cards)} cards\n")
            f.write("=" * 60 + "\n\n")
            
            for i, card in enumerate(matched_cards, 1):
                f.write(f"{i}. {card}\n")
            
            f.write("\n" + "=" * 60 + "\n")
            f.write("CLEAN CARDS (Copy-Paste Ready):\n")
            f.write("=" * 60 + "\n\n")
            for card in matched_cards:
                f.write(f"{card}\n")
        
        # Send result
        await status_msg.delete()
        
        await update.message.reply_document(
            document=open(output_file, 'rb'),
            caption=f"✅ <b>BIN Filter Complete!</b>\n\n"
                    f"🎯 BINs: <code>{', '.join(bins[:10])}</code>\n"
                    f"📊 Total cards: {total_cards:,}\n"
                    f"✅ Matched: <b>{len(matched_cards):,}</b>\n"
                    f"🔷 Base64 decoded: {base64_cards}\n\n"
                    f"<i>File contains both detailed and clean cards</i>",
            parse_mode=ParseMode.HTML
        )
        
        return await show_main_menu(update.message, context)
        
    except Exception as e:
        logger.error(f"BIN Filter Error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
        return State.MAIN_MENU
    
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        context.user_data.pop('binfilter_bins', None)
        context.user_data.pop('binfilter_source', None)

async def bin_filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Cancel BIN filter"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('binfilter_bins', None)
    context.user_data.pop('binfilter_source', None)
    await query.message.delete()
    await query.message.reply_text("❌ Cancelled.")
    return await show_main_menu(query.message, context)

async def bin_filter_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Go back to main menu"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('binfilter_bins', None)
    context.user_data.pop('binfilter_source', None)
    return await show_main_menu(query.message, context)

async def bin_filter_back_to_bins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Go back to BIN input"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('binfilter_source', None)
    
    await query.edit_message_text(
        "🎯 <b>Enter BINs Again</b>\n\n"
        "Send BINs you want to filter (comma separated):\n"
        "Example: <code>411111, 522222, 412345</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="binfilter_back")]
        ])
    )
    return State.BIN_FILTER_GET_BINS

# =================================================================
# 8. SCAN WIZARD HANDLERS
# =================================================================

async def start_scan_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    if not os.path.exists(get_session_path(user_id)):
        msg_text = "⚠️ Please <b>Login</b> first to use Scanner."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML)
        return State.MAIN_MENU
    
    kb = [
        [InlineKeyboardButton("📢 Channel", callback_data="source_channel"), InlineKeyboardButton("👥 Group", callback_data="source_group")],
        [InlineKeyboardButton("👤 Private User / DM", callback_data="source_private_user")], 
        [InlineKeyboardButton(f"📂 Local File ({LOCAL_SCAN_FILE})", callback_data="source_local_txt")],
        [InlineKeyboardButton("❌ Cancel", callback_data="scan_cancel")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text("⚙️ <b>Scan Setup</b> Select source:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text("⚙️ <b>Scan Setup</b> Select source:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_SELECT_SOURCE

async def ask_private_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    context.user_data['scan_settings'] = {'source_type': 'private_user'}
    
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_source_select")]]
    await query.edit_message_text(
        "👤 <b>Target Username?</b>\n\nEnter the username (e.g., <code>@Hub_Offical</code>) or Phone number:", 
        reply_markup=InlineKeyboardMarkup(kb), 
        parse_mode=ParseMode.HTML
    )
    return State.SCAN_INPUT_USERNAME

async def resolve_private_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_input = update.message.text.strip()
    user_id = update.effective_user.id
    msg = await update.message.reply_text("🔄 <b>Checking User...</b>", parse_mode=ParseMode.HTML)
    
    try:
        session_string = get_session_string(user_id)
        if not session_string:
             await msg.edit_text("❌ Session error. Please login again.")
             return State.MAIN_MENU

        async with TelegramClient(StringSession(session_string), API_ID, API_HASH) as client:
            try:
                entity = await client.get_entity(user_input)
                context.user_data['scan_settings']['chat_id'] = entity.id
                chat_name = getattr(entity, 'first_name', None) or getattr(entity, 'title', "Unknown User")
                if getattr(entity, 'username', None):
                    chat_name += f" (@{entity.username})"
                context.user_data['scan_settings']['chat_name'] = chat_name
            except ValueError:
                await msg.edit_text("❌ <b>User not found!</b>\nPlease check the username and try again.", parse_mode=ParseMode.HTML)
                return State.SCAN_INPUT_USERNAME
            except Exception as e:
                await msg.edit_text(f"❗️ Error: {e}")
                return State.SCAN_INPUT_USERNAME

        await msg.delete()
        kb = [[InlineKeyboardButton("⬇️ Oldest ➔ Newest", callback_data="direction_reverse")],
              [InlineKeyboardButton("⬆️ Newest ➔ Oldest", callback_data="direction_normal")],
              [InlineKeyboardButton("🔙 Back", callback_data="back_to_source_select")]]
        await update.message.reply_text(
            f"✅ <b>Target Found:</b> {context.user_data['scan_settings']['chat_name']}\n\n🧭 <b>Scan Direction?</b>", 
            reply_markup=InlineKeyboardMarkup(kb), 
            parse_mode=ParseMode.HTML
        )
        return State.SCAN_SELECT_DIRECTION

    except Exception as e:
        logger.error(f"Resolve Username Error: {e}")
        await msg.edit_text(f"❗️ System Error: {e}")
        return State.MAIN_MENU

async def select_source_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    stype = query.data.split('_')[1]
    context.user_data['scan_settings'] = {'source_type': stype}
    
    msg = await query.edit_message_text("🔄 <b>Fetching Chats...</b>", parse_mode=ParseMode.HTML)
    try:
        session_string = get_session_string(user_id)
        if not session_string:
             await msg.edit_text("❌ Session error. Please login again.")
             return State.MAIN_MENU

        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        async with client:
            dialogs = await client.get_dialogs()
        
        chats = [d for d in dialogs if (stype == 'channel' and d.is_channel) or (stype == 'group' and d.is_group)]
        if not chats:
            await msg.edit_text("❌ No chats found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_source_select")]]))
            return State.SCAN_SELECT_SOURCE
            
        context.user_data['chats'] = sorted([{'id': d.id, 'name': d.name} for d in chats], key=lambda x: x['name'])
        context.user_data['chat_page'] = 0
        await show_chat_page(update, context)
        return State.SCAN_SELECT_CHAT
    except Exception as e:
        await msg.edit_text(f"❗️ Error: {e}")
        return State.SCAN_SELECT_SOURCE

async def show_chat_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chats = context.user_data['chats']
    page = context.user_data.get('chat_page', 0)
    limit = 24
    start, end = page * limit, (page + 1) * limit
    current_chats = chats[start:end]
    
    btns = []
    for i in range(0, len(current_chats), 2):
        row_items = current_chats[i:i+2]
        row = [InlineKeyboardButton(c['name'], callback_data=f"chat_{c['id']}") for c in row_items]
        btns.append(row)

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️ Prev", callback_data="page_prev"))
    if end < len(chats): nav.append(InlineKeyboardButton("Next ▶️", callback_data="page_next"))
    if nav: btns.append(nav)
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_source_select")])
    await query.edit_message_text(f"📂 <b>Select Chat</b> (Page {page+1}):", reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)

async def chat_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    act = query.data.split('_')[1]
    context.user_data['chat_page'] += 1 if act == 'next' else -1
    await show_chat_page(update, context)
    return State.SCAN_SELECT_CHAT

async def select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    settings = context.user_data['scan_settings']
    settings['chat_id'] = int(query.data.split('_')[1])
    settings['chat_name'] = next((c['name'] for c in context.user_data['chats'] if c['id'] == settings['chat_id']), "Unknown")
    return await ask_scan_direction(update, context)

async def select_source_local(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    context.user_data['scan_settings'] = {'source_type': 'local_txt', 'chat_id': None, 'chat_name': f"Local ({LOCAL_SCAN_FILE})"}
    kb = [[InlineKeyboardButton("💳 All Cards", callback_data="scantype_normal")],
          [InlineKeyboardButton("🎯 Target BINs", callback_data="scantype_bin")],
          [InlineKeyboardButton("🔙 Back", callback_data="back_to_source_select")]]
    await q.edit_message_text("🛠 <b>Scan Type?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_SELECT_TYPE

async def ask_scan_direction(update: Update, context: ContextTypes.DEFAULT_TYPE, from_back=False) -> State:
    q = update.callback_query
    kb = [[InlineKeyboardButton("⬇️ Oldest ➔ Newest", callback_data="direction_reverse")],
          [InlineKeyboardButton("⬆️ Newest ➔ Oldest", callback_data="direction_normal")],
          [InlineKeyboardButton("🔙 Back", callback_data="back_to_chatlist")]]
    await q.edit_message_text("🧭 <b>Scan Direction?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_SELECT_DIRECTION

async def handle_scan_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    context.user_data['scan_settings']['scan_direction'] = q.data.split('_')[1]
    kb = [[InlineKeyboardButton("💳 All Cards", callback_data="scantype_normal")],
          [InlineKeyboardButton("🎯 Target BINs", callback_data="scantype_bin")],
          [InlineKeyboardButton("🔙 Back", callback_data="back_to_direction_select")]]
    await q.edit_message_text("🛠 <b>Scan Type?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_SELECT_TYPE

async def select_scan_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    settings = context.user_data['scan_settings']
    settings['scan_type'] = q.data.split('_')[1]
    back = "back_to_direction_select" if settings['source_type'] != 'local_txt' else "back_to_source_select"
    if settings['scan_type'] == 'bin':
        await q.edit_message_text("🎯 <b>BIN Mode</b>\nSend BINs (e.g. <code>41111, 52222</code>):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=back)]]), parse_mode=ParseMode.HTML)
        return State.SCAN_GET_BIN
    else:
        return await ask_limit_mode(q, back)

async def ask_limit_mode(message, back_callback_data):
    kb = [[InlineKeyboardButton("♾️ Unlimited", callback_data="mode_all")],
          [InlineKeyboardButton("🔢 Custom Limit", callback_data="mode_custom")],
          [InlineKeyboardButton("🔙 Back", callback_data=back_callback_data)]]
    func = message.edit_message_text if hasattr(message, 'edit_message_text') else message.reply_text
    await func("🔢 <b>How many cards?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_SELECT_MODE

async def get_bin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    txt = update.message.text.strip()
    bins = [b.strip() for b in txt.replace(' ', '').split(',') if b.strip().isdigit()]
    if not bins:
        await update.message.reply_text("❗️ <b>Invalid Format.</b>", parse_mode=ParseMode.HTML)
        return State.SCAN_GET_BIN
    context.user_data['scan_settings']['bins_to_find'] = bins
    back = "back_to_direction_select" if context.user_data['scan_settings']['source_type'] != 'local_txt' else "back_to_source_select"
    return await ask_limit_mode(update.message, back)

async def get_scan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    if q.data == 'mode_custom':
        await q.edit_message_text("⌨️ <b>Enter Limit:</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_scantype_normal")]]), parse_mode=ParseMode.HTML)
        return State.SCAN_GET_LIMIT
    context.user_data['scan_settings']['limit'] = None
    return await ask_filter_expired(update, context)

async def get_limit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        context.user_data['scan_settings']['limit'] = int(update.message.text)
        return await ask_filter_expired(update, context, from_message=True)
    except:
        await update.message.reply_text("❗️ Numbers only.")
        return State.SCAN_GET_LIMIT

async def ask_filter_expired(update: Update, context: ContextTypes.DEFAULT_TYPE, from_message=False, from_back=False) -> State:
    msg = update.effective_message
    kb = [[InlineKeyboardButton("✅ Yes (Filter)", callback_data="filter_yes"), InlineKeyboardButton("❌ No (Keep All)", callback_data="filter_no")],
          [InlineKeyboardButton("🔙 Back", callback_data="back_to_mode")]] 
    func = msg.reply_text if from_message or from_back else msg.edit_text
    await func("📅 <b>Filter Expired Cards?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_FILTER_EXPIRED

async def handle_filter_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    settings = context.user_data['scan_settings']
    settings['filter_expired'] = (q.data == 'filter_yes')
    txt = (f"📝 <b>Confirm</b>\nSource: <code>{settings['chat_name']}</code>\nType: <code>{settings['scan_type']}</code>\nLimit: <code>{settings.get('limit','All')}</code>\nFilter: <code>{settings['filter_expired']}</code>")
    kb = [[InlineKeyboardButton("🚀 Start Scan", callback_data="confirm_start")], [InlineKeyboardButton("🔙 Back", callback_data="back_to_filter_select")]]
    await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.SCAN_CONFIRMATION

async def launch_scan_background(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    user_id = update.effective_user.id
    await q.answer("🚀 Launching...")
    task_id = str(uuid.uuid4())[:8]
    settings = context.user_data['scan_settings'].copy()
    if 'active_tasks' not in context.bot_data: context.bot_data['active_tasks'] = {}
    context.bot_data['active_tasks'][task_id] = {
        'type': 'Scan', 'status': 'Running', 'stop_signal': False, 'user_id': user_id
    }
    asyncio.create_task(run_scan_task(task_id, user_id, settings, context.bot, context.bot_data))
    await q.message.delete()
    msg = await q.message.reply_text(f"✅ <b>Scan Started!</b>\nTask ID: <code>{task_id}</code>\n<i>(Auto-deleting...)</i>", parse_mode=ParseMode.HTML)
    asyncio.create_task(delete_after_delay(msg, 5))
    return await show_main_menu(q.message, context)

# =================================================================
# 9. CLEAN WIZARD HANDLERS
# =================================================================

async def start_clean_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    kb = [[InlineKeyboardButton("📤 Upload File", callback_data="clean_mode_upload")],
          [InlineKeyboardButton("📂 Use Server File", callback_data="clean_mode_local")],
          [InlineKeyboardButton("❌ Cancel", callback_data="scan_cancel")]]
    await update.message.reply_text("🧼 <b>Cleaner</b> Select input:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.CLEAN_SELECT_METHOD

async def handle_clean_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    if q.data == 'clean_mode_upload':
        await q.edit_message_text("📤 <b>Upload File:</b>\nSend any .txt file (Max 2GB).")
        return State.CLEAN_FILE_UPLOAD
    else:
        await q.edit_message_text("⌨️ <b>Filename:</b>\nReply with filename on server:")
        return State.CLEAN_ASK_LOCAL_FILENAME

async def handle_clean_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    doc = update.message.document
    if not doc: return State.CLEAN_FILE_UPLOAD
    status = await update.message.reply_text("📥 Downloading...")
    temp_dir = tempfile.mkdtemp()
    fname = os.path.join(temp_dir, doc.file_name or "uploaded.txt")
    file_obj = await doc.get_file()
    await file_obj.download_to_drive(fname)
    context.user_data['clean_temp_dir'] = temp_dir
    context.user_data['clean_filename'] = fname
    return await ask_split_limit(status, context)

async def handle_clean_local_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    fname = update.message.text.strip()
    if not os.path.exists(fname):
        await update.message.reply_text("❌ File not found.")
        return State.CLEAN_ASK_LOCAL_FILENAME
    context.user_data['clean_filename'] = fname
    return await ask_split_limit(update.message, context)

async def ask_split_limit(message, context):
    kb = [[InlineKeyboardButton("📦 Single File", callback_data="split_unlimited")],
          [InlineKeyboardButton("✂️ Split Output", callback_data="split_custom")]]
    func = message.reply_text if hasattr(message, 'reply_text') else message.edit_text
    await func("✂️ <b>Split Options?</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return State.CLEAN_GET_SPLIT_LIMIT

async def handle_clean_split_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    q = update.callback_query
    await q.answer()
    if q.data == 'split_unlimited':
        context.user_data['clean_split_limit'] = 0
        return await launch_clean_background(update, context)
    else:
        await q.edit_message_text("⌨️ <b>Enter Cards Per File:</b>\nExample: 5000", parse_mode=ParseMode.HTML)
        return State.CLEAN_GET_SPLIT_LIMIT

async def handle_clean_split_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        context.user_data['clean_split_limit'] = int(update.message.text)
        return await launch_clean_background(update, context)
    except:
        await update.message.reply_text("❗️ Numbers only.")
        return State.CLEAN_GET_SPLIT_LIMIT

async def launch_clean_background(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message if update.message else update.callback_query.message
    user_id = update.effective_user.id
    task_id = str(uuid.uuid4())[:8]
    fname = context.user_data['clean_filename']
    split = context.user_data.get('clean_split_limit', 0)
    if 'active_tasks' not in context.bot_data: context.bot_data['active_tasks'] = {}
    context.bot_data['active_tasks'][task_id] = {
        'type': 'Clean', 'status': 'Processing', 'stop_signal': False, 'user_id': user_id
    }
    asyncio.create_task(run_clean_task(task_id, user_id, fname, split, context.bot, context.bot_data))
    if update.callback_query: await update.callback_query.message.delete()
    sent_msg = await msg.reply_text(f"✅ <b>Cleaning Started!</b>\nTask ID: <code>{task_id}</code>\n<i>(Auto-deleting...)</i>", parse_mode=ParseMode.HTML)
    asyncio.create_task(delete_after_delay(sent_msg, 5))
    return await show_main_menu(msg, context)

# --- Common Back Handlers ---
async def back_to_source_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State: 
    return await start_scan_wizard(update, context)

async def back_to_chatlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State: 
    if context.user_data.get('scan_settings', {}).get('source_type') == 'private_user':
        return await start_scan_wizard(update, context)
    update.callback_query.data = f"source_{context.user_data['scan_settings']['source_type']}"
    return await select_source_chat(update, context)

async def back_to_direction_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State: 
    return await ask_scan_direction(update, context, from_back=True)

async def back_to_scantype_normal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State: 
    context.user_data['scan_settings']['scan_type'] = 'normal' 
    return await select_scan_type(update, context)

async def back_to_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    settings = context.user_data['scan_settings']
    back = "back_to_direction_select" if settings['source_type'] != 'local_txt' else "back_to_source_select"
    return await ask_limit_mode(update.callback_query, back)

async def back_to_filter_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State: 
    return await ask_filter_expired(update, context, from_back=True)

async def scan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.message.edit_text("❌ Cancelled.")
    return await show_main_menu(update.callback_query.message, context)

# =================================================================
# ERROR HANDLER
# =================================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log the error and send a telegram message to notify the user."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
    # Send error message to user if possible
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ <b>An error occurred!</b>\n"
                "Please try again later.\n\n"
                f"<i>{str(context.error)[:200]}</i>",
                parse_mode=ParseMode.HTML
            )
    except:
        pass

# =================================================================
# MAIN EXECUTION
# =================================================================

def main():
    # Create application with custom request timeout settings
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60,
        write_timeout=60,
        connect_timeout=60,
    )
    
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    states = {
        State.MAIN_MENU: [
            MessageHandler(filters.Regex("^🚀 Start Scanner$"), start_scan_wizard),
            MessageHandler(filters.Regex("^🧼 Clean / Split File$"), start_clean_wizard),
            MessageHandler(filters.Regex("^🕷️ Channel Scraper$"), start_scraper_wizard),
            MessageHandler(filters.Regex("^🎯 BIN Filter$"), bin_filter_start),
            MessageHandler(filters.Regex("^📊 Active Tasks$"), show_active_tasks),
            MessageHandler(filters.Regex("^⚙️ Account Settings$"), account_settings),
            CallbackQueryHandler(handle_login_logout_callback, pattern='^(login|logout)$'),
            CallbackQueryHandler(start_scraper_wizard, pattern='^scraper_main$'),
            # BIN Filter callbacks from main menu
            CallbackQueryHandler(bin_filter_get_bins, pattern='^binfilter_(upload|server)$'),
            CallbackQueryHandler(bin_filter_cancel, pattern='^binfilter_cancel$'),
            CallbackQueryHandler(bin_filter_back, pattern='^binfilter_back$'),
        ],
        State.LOGIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone)],
        State.LOGIN_CODE: [CallbackQueryHandler(handle_login_keypad_callback, pattern='^login_')],
        State.LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
        
        State.SCAN_SELECT_SOURCE: [
            CallbackQueryHandler(select_source_chat, pattern='^source_(channel|group)$'),
            CallbackQueryHandler(ask_private_username, pattern='^source_private_user$'),
            CallbackQueryHandler(select_source_local, pattern='^source_local_txt$'),
            CallbackQueryHandler(scan_cancel, pattern='^scan_cancel$')
        ],
        State.SCAN_INPUT_USERNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, resolve_private_username),
            CallbackQueryHandler(back_to_source_select, pattern='^back_to_source_select$')
        ],
        State.SCAN_SELECT_CHAT: [
            CallbackQueryHandler(select_chat, pattern='^chat_'),
            CallbackQueryHandler(chat_page_callback, pattern='^page_'),
            CallbackQueryHandler(back_to_source_select, pattern='^back_to_source_select$')
        ],
        State.SCAN_SELECT_DIRECTION: [
            CallbackQueryHandler(handle_scan_direction, pattern='^direction_'),
            CallbackQueryHandler(back_to_chatlist, pattern='^back_to_chatlist$')
        ],
        State.SCAN_SELECT_TYPE: [
            CallbackQueryHandler(select_scan_type, pattern='^scantype_'),
            CallbackQueryHandler(back_to_direction_select, pattern='^back_to_direction_select$'),
            CallbackQueryHandler(back_to_source_select, pattern='^back_to_source_select$')
        ],
        State.SCAN_GET_BIN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_bin_input),
            CallbackQueryHandler(back_to_direction_select, pattern='^back_to_direction_select$')
        ],
        State.SCAN_SELECT_MODE: [
            CallbackQueryHandler(get_scan_mode, pattern='^mode_'),
            CallbackQueryHandler(back_to_scantype_normal, pattern='^back_to_scantype_normal$'),
            CallbackQueryHandler(back_to_direction_select, pattern='^back_to_direction_select$')
        ],
        State.SCAN_GET_LIMIT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_limit_input),
            CallbackQueryHandler(back_to_mode, pattern='^back_to_mode$')
        ],
        State.SCAN_FILTER_EXPIRED: [
            CallbackQueryHandler(handle_filter_expired, pattern='^filter_'),
            CallbackQueryHandler(back_to_mode, pattern='^back_to_mode$')
        ],
        State.SCAN_CONFIRMATION: [
            CallbackQueryHandler(launch_scan_background, pattern='^confirm_start'),
            CallbackQueryHandler(back_to_filter_select, pattern='^back_to_filter_select$')
        ],
        
        # CLEAN STATES
        State.CLEAN_SELECT_METHOD: [
            CallbackQueryHandler(handle_clean_method, pattern='^clean_mode_'), 
            CallbackQueryHandler(scan_cancel, pattern='^scan_cancel$')
        ],
        State.CLEAN_FILE_UPLOAD: [
            MessageHandler(filters.ATTACHMENT, handle_clean_file_upload)
        ],
        State.CLEAN_ASK_LOCAL_FILENAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clean_local_filename)
        ],
        State.CLEAN_GET_SPLIT_LIMIT: [
            CallbackQueryHandler(handle_clean_split_selection, pattern='^split_'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clean_split_input)
        ],
        
        # SCRAPER STATES
        State.SCRAPER_MAIN: [
            CallbackQueryHandler(scraper_select_source_chat, pattern='^scraper_source_(channel|group)$'),
            CallbackQueryHandler(scraper_monitored_channels, pattern='^scraper_monitored$'),
            CallbackQueryHandler(scraper_show_cards, pattern='^scraper_show_cards$'),
            CallbackQueryHandler(scraper_export_all, pattern='^scraper_export_all$'),
            CallbackQueryHandler(scraper_auto_export, pattern='^scraper_auto_export$'),
            CallbackQueryHandler(scraper_clear_cards, pattern='^scraper_clear_confirm$'),
            CallbackQueryHandler(scraper_cancel, pattern='^scraper_cancel$'),
            CallbackQueryHandler(scraper_back, pattern='^scraper_back$'),
        ],
        State.SCRAPER_SELECT_CHAT: [
            CallbackQueryHandler(scraper_select_chat, pattern='^scraper_chat_'),
            CallbackQueryHandler(scraper_chat_page_callback, pattern='^scraper_page_'),
            CallbackQueryHandler(scraper_back, pattern='^scraper_back$'),
        ],
        State.SCRAPER_CONFIRM: [
            CallbackQueryHandler(scraper_launch, pattern='^scraper_start$'),
            CallbackQueryHandler(scraper_back, pattern='^scraper_back_to_chats$'),
            CallbackQueryHandler(scraper_back, pattern='^scraper_back$'),
        ],

        # BIN FILTER STATES
        State.BIN_FILTER_GET_BINS: [
            CallbackQueryHandler(bin_filter_back, pattern='^binfilter_back$'),
            CallbackQueryHandler(bin_filter_back_to_bins, pattern='^binfilter_back_bins$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, bin_filter_handle_bins),
        ],
        State.BIN_FILTER_WAIT_FILE: [
            CallbackQueryHandler(bin_filter_upload_ready, pattern='^binfilter_upload_ready$'),
            CallbackQueryHandler(bin_filter_server_ready, pattern='^binfilter_server_ready$'),
            CallbackQueryHandler(bin_filter_back_to_bins, pattern='^binfilter_back_to_bins$'),
            CallbackQueryHandler(bin_filter_back, pattern='^binfilter_back$'),
            MessageHandler(filters.ATTACHMENT, bin_filter_handle_file),
            MessageHandler(filters.TEXT & ~filters.COMMAND, bin_filter_handle_file),
        ],
    }

    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start_handler), 
            CommandHandler('login', login_start),
            CommandHandler('scraper', start_scraper_wizard),
            CommandHandler('binfilter', bin_filter_start),
            MessageHandler(filters.Regex("^(🚀 Start Scanner|🧼 Clean / Split File|🕷️ Channel Scraper|🎯 BIN Filter|📊 Active Tasks|⚙️ Account Settings)$"), start_handler)
        ],
        states=states,
        fallbacks=[CommandHandler('cancel', start_handler), CallbackQueryHandler(scan_cancel, pattern='^scan_cancel$')],
        per_message=False
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(stop_task_handler, pattern='^stop_task_'))
    
    # Scraper Commands
    app.add_handler(CommandHandler('scraped', scraper_show_cards))
    app.add_handler(CommandHandler('monitored', scraper_monitored_channels))
    app.add_handler(CommandHandler('export_scraped', scraper_export_all))
    app.add_handler(CommandHandler('autoexport', scraper_auto_export))
    app.add_handler(CommandHandler('clear_scraped', scraper_clear_cards))
    app.add_handler(CommandHandler('stop_scrape', stop_scraper_command))
    
    # BIN Filter Command
    app.add_handler(CommandHandler('binfilter', bin_filter_start))
    
    print("🤖 Bot is Running with Channel Scraper & BIN Filter!")
    print("🕷️ Scraper will auto-detect base64 encoded cards")
    print("🎯 BIN Filter extracts cards by BIN from file")
    print("📁 All cards will be saved to a single file")
    print("=" * 50)
    
    try:
        app.run_polling(
            poll_interval=1.0,
            bootstrap_retries=5,
            allowed_updates=['message', 'callback_query']
        )
    except Exception as e:
        print(f"❌ Bot Error: {e}")
        print("🔄 Retrying in 5 seconds...")
        time.sleep(5)
        main()

if __name__ == '__main__':
    main()
