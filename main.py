#!/usr/bin/env python3
"""
Telegram OSINT Bot - النسخة المتكاملة مع البحث عبر بوتات خارجية وروابط الدعوة
"""

import asyncio
import sqlite3
import re
import os
import random
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from telethon import TelegramClient
from telethon.errors import UsernameNotOccupiedError, FloodWaitError
from telethon.tl.functions.channels import GetParticipantRequest
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
import aiohttp

# ===================== توكن البوت (ضعه هنا) =====================
BOT_TOKEN = "7969100821:AAG1uu-1uYcuQxpqu1KcfKwlKP_VERsdsfo"

# ===================== إعدادات إضافية =====================
INVITE_LINKS = [
    # أضف هنا روابط مجموعات/قنوات عامة تريد البحث فيها عن المستخدمين
    # مثال: "https://t.me/joinchat/xxxx", "https://t.me/SomePublicGroup"
    # يمكنك أيضاً تركها فارغة وجلب الروابط تلقائياً فيما بعد.
]

# ===================== قاعدة البيانات (SQLite) =====================
DB_PATH = "osint_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS name_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        changed_at TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        text TEXT,
        date TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS word_freq (
        user_id INTEGER,
        word TEXT,
        frequency INTEGER,
        period TEXT,
        PRIMARY KEY (user_id, word, period)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups_cache (
        group_id INTEGER PRIMARY KEY,
        title TEXT,
        username TEXT,
        invite_link TEXT
    )''')
    conn.commit()
    conn.close()

def set_config(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_config(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_name_record(user_id: int, username: str, first_name: str, last_name: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO name_history (user_id, username, first_name, last_name, changed_at) VALUES (?,?,?,?,?)",
              (user_id, username, first_name, last_name, datetime.utcnow().isoformat(timespec='seconds')))
    conn.commit()
    conn.close()

def get_name_history(user_id: int) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, first_name, last_name, changed_at FROM name_history WHERE user_id=? ORDER BY changed_at ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"username": r[0], "first_name": r[1], "last_name": r[2], "changed_at": r[3]} for r in rows]

def index_messages(user_id: int, messages: List[Dict]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for msg in messages:
        c.execute("INSERT INTO messages_index (user_id, chat_id, message_id, text, date) VALUES (?,?,?,?,?)",
                  (user_id, msg.get('chat_id'), msg.get('message_id'), msg['text'], msg['date'].isoformat() if msg['date'] else None))
        msg_id = c.lastrowid
        words = re.findall(r'\b\w{3,}\b', msg['text'].lower())
        for w in set(words):
            c.execute("INSERT OR IGNORE INTO word_freq (user_id, word, frequency, period) VALUES (?,?,1,'all')", (user_id, w))
            c.execute("UPDATE word_freq SET frequency = frequency + 1 WHERE user_id=? AND word=? AND period='all'", (user_id, w))
    conn.commit()
    conn.close()

def search_word_in_user_messages(user_id: int, keyword: str) -> List[Tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT text, date FROM messages_index WHERE user_id=? AND text LIKE ? ORDER BY date DESC LIMIT 50', (user_id, f'%{keyword}%'))
    rows = c.fetchall()
    conn.close()
    return rows

def global_search(keyword: str) -> List[Tuple[int, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id, text, date FROM messages_index WHERE text LIKE ? ORDER BY date DESC LIMIT 100', (f'%{keyword}%',))
    rows = c.fetchall()
    conn.close()
    return rows

def save_group_cache(group_id, title, username, invite_link):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO groups_cache (group_id, title, username, invite_link) VALUES (?,?,?,?)", (group_id, title, username, invite_link))
    conn.commit()
    conn.close()

def get_all_cached_groups():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT group_id, title, username, invite_link FROM groups_cache")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "username": r[2], "link": r[3]} for r in rows]

# ===================== متغيرات Telethon =====================
telethon_client: Optional[TelegramClient] = None

# ===================== FSM للإعداد =====================
class SetupStates(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()

# ===================== تهيئة البوت =====================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# تخزين مؤقت للرسائل المحللة (لكل مستخدم)
user_messages_cache = {}

# ===================== دوال مساعدة =====================
async def ensure_telethon():
    if telethon_client and telethon_client.is_connected():
        return True
    api_id = get_config("API_ID")
    api_hash = get_config("API_HASH")
    phone = get_config("PHONE")
    if not api_id or not api_hash or not phone:
        return False
    try:
        client = TelegramClient('user_session', int(api_id), api_hash)
        await client.start(phone=phone)
        global telethon_client
        telethon_client = client
        return True
    except:
        return False

async def update_user_name_history(user_id: int, username: str, first_name: str, last_name: str):
    history = get_name_history(user_id)
    if not history:
        save_name_record(user_id, username, first_name, last_name)
        return
    last = history[-1]
    if last["username"] != username or last["first_name"] != first_name or last["last_name"] != last_name:
        save_name_record(user_id, username, first_name, last_name)

async def fetch_user_entity(identifier):
    """محاولة جلب الكيان من اسم مستخدم، رقم، أو ID"""
    try:
        if identifier.isdigit():
            return await telethon_client.get_entity(int(identifier))
        elif identifier.startswith('+'):
            return await telethon_client.get_entity(identifier)
        else:
            return await telethon_client.get_entity(identifier)
    except:
        return None

# ===================== أزرار رئيسية =====================
def main_keyboard():
    buttons = [
        [KeyboardButton(text="🔍 معلومات مستخدم"), KeyboardButton(text="📢 قنوات مشتركة")],
        [KeyboardButton(text="📨 تحليل رسائل"), KeyboardButton(text="📊 كلمات متكررة")],
        [KeyboardButton(text="⏰ نشاط المستخدم"), KeyboardButton(text="📜 تاريخ الأسماء")],
        [KeyboardButton(text="🔎 بحث بكلمة"), KeyboardButton(text="🌍 بحث عالمي")],
        [KeyboardButton(text="🔔 مراقبة تغييرات"), KeyboardButton(text="⚙️ إعدادات")],
        [KeyboardButton(text="🧩 فحص شامل (Full Scan)"), KeyboardButton(text="➕ إضافة رابط مجموعة")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ===================== أوامر الإعداد =====================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    if await ensure_telethon():
        await msg.reply(
            "<b>🔍 بوت OSINT المتكامل</b>\n\n"
            "اختر أحد الأزرار أدناه لبدء الاستعلامات.\n"
            "يمكنك أيضاً استخدام الأوامر النصية المباشرة: /help",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return
    await msg.reply("<b>🔐 مرحباً! لنبدأ الإعداد.</b>\nأرسل <code>API ID</code> (أرقام فقط):", parse_mode="HTML")
    await state.set_state(SetupStates.waiting_api_id)

@dp.message(StateFilter(SetupStates.waiting_api_id))
async def get_api_id(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.reply("❌ API ID يجب أن يكون أرقاماً. حاول مرة أخرى:")
        return
    await state.update_data(api_id=msg.text)
    set_config("API_ID", msg.text)
    await msg.reply("✅ تم حفظ API ID.\n📎 أرسل الآن <code>API Hash</code> (النص الطويل):", parse_mode="HTML")
    await state.set_state(SetupStates.waiting_api_hash)

@dp.message(StateFilter(SetupStates.waiting_api_hash))
async def get_api_hash(msg: types.Message, state: FSMContext):
    api_hash = msg.text.strip()
    if len(api_hash) < 10:
        await msg.reply("❌ API Hash يبدو قصيراً. تأكد من نسخه كاملاً:")
        return
    await state.update_data(api_hash=api_hash)
    set_config("API_HASH", api_hash)
    await msg.reply("✅ تم حفظ API Hash.\n📱 أرسل رقم هاتفك بالتنسيق الدولي (مثال: <code>+201095539788</code>):", parse_mode="HTML")
    await state.set_state(SetupStates.waiting_phone)

@dp.message(StateFilter(SetupStates.waiting_phone))
async def get_phone(msg: types.Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith('+') or not phone[1:].isdigit():
        await msg.reply("❌ رقم غير صحيح. ابدأ بـ + ثم الأرقام. مثال: <code>+201234567890</code>", parse_mode="HTML")
        return
    await state.update_data(phone=phone)
    set_config("PHONE", phone)
    data = await state.get_data()
    api_id = int(data["api_id"])
    api_hash = data["api_hash"]
    global telethon_client
    telethon_client = TelegramClient('user_session', api_id, api_hash)
    await telethon_client.connect()
    if not await telethon_client.is_user_authorized():
        await telethon_client.send_code_request(phone)
        await msg.reply("📨 تم إرسال رمز التحقق. أرسل الرمز (أرقام فقط):")
        await state.set_state(SetupStates.waiting_code)
    else:
        me = await telethon_client.get_me()
        await update_user_name_history(me.id, me.username, me.first_name, me.last_name)
        await msg.reply(f"✅ مرحباً {me.first_name}! البوت جاهز. أرسل /start", parse_mode="HTML")
        await state.clear()

@dp.message(StateFilter(SetupStates.waiting_code))
async def get_code(msg: types.Message, state: FSMContext):
    code = msg.text.strip()
    if not code.isdigit():
        await msg.reply("❌ الرقم يتكون من أرقام فقط. حاول مرة أخرى:")
        return
    data = await state.get_data()
    phone = data["phone"]
    try:
        await telethon_client.sign_in(phone, code)
        me = await telethon_client.get_me()
        await update_user_name_history(me.id, me.username, me.first_name, me.last_name)
        await msg.reply(f"✅ تم تسجيل الدخول بنجاح. البوت جاهز! أرسل /start", parse_mode="HTML")
        await state.clear()
    except Exception as e:
        await msg.reply(f"❌ رمز خاطئ: {str(e)[:100]}. استخدم /start من البداية.")
        await state.clear()

@dp.message(Command("reset"))
async def cmd_reset(msg: types.Message, state: FSMContext):
    global telethon_client
    if telethon_client:
        await telethon_client.disconnect()
    set_config("API_ID", "")
    set_config("API_HASH", "")
    set_config("PHONE", "")
    await state.clear()
    await msg.reply("🔄 تم مسح جميع البيانات. أرسل /start لإعداد البوت من جديد.", reply_markup=main_keyboard())

# ===================== الأمر الأساسي: معلومات المستخدم =====================
@dp.message(Command("info"))
async def cmd_info(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل. أرسل /start.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/info @username</code> أو <code>/info +رقم</code> أو <code>/info ID</code>", parse_mode="HTML")
        return
    target = parts[1].strip()
    await msg.reply(f"🔍 جلب معلومات <b>{target}</b> ...")
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ لم يتم العثور على المستخدم.")
        return
    await update_user_name_history(entity.id, entity.username, entity.first_name, entity.last_name)
    history_count = len(get_name_history(entity.id))
    response = (
        f"<b>👤 {entity.first_name or ''} {entity.last_name or ''}</b>\n"
        f"🆔 <code>{entity.id}</code>\n"
        f"📝 @{entity.username or 'لا يوجد'}\n"
        f"📱 {getattr(entity, 'phone', 'مخفي')}\n"
        f"✅ موثق: {'نعم' if entity.verified else 'لا'}\n"
        f"🤖 بوت: {'نعم' if entity.bot else 'لا'}\n"
        f"🌐 مركز البيانات: {getattr(entity, 'dc_id', 'غير معروف')}\n"
        f"📜 عدد التغييرات المسجلة: {history_count}\n"
        f"🔹 استخدم <code>/history {target}</code> لعرض كل الأسماء السابقة."
    )
    await msg.reply(response, parse_mode="HTML")

# ===================== القنوات المشتركة =====================
@dp.message(Command("common"))
async def cmd_common(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/common @username</code>", parse_mode="HTML")
        return
    target = parts[1].strip()
    await msg.reply(f"🔍 البحث عن قنوات مشتركة مع {target} ...")
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    try:
        common = await telethon_client.get_common_chats(entity)
        if not common:
            await msg.reply("ℹ️ لا توجد قنوات أو مجموعات عامة مشتركة.")
            return
        resp = f"<b>📢 المشترك فيها ({len(common)}):</b>\n"
        for c in common[:30]:
            ctype = "قناة" if getattr(c, 'broadcast', False) else ("سوبر جروب" if getattr(c, 'megagroup', False) else "جروب")
            resp += f"• {c.title} ({ctype})\n"
        await msg.reply(resp, parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"⚠️ خطأ: {str(e)[:200]}")

# ===================== تحليل الرسائل وفهرستها =====================
@dp.message(Command("messages"))
async def cmd_messages(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/messages @username</code> (سيجلب 500 رسالة ويفهرسها)", parse_mode="HTML")
        return
    target = parts[1].strip()
    await msg.reply(f"📨 جلب آخر 500 رسالة من {target} وفهرستها ... قد يستغرق دقيقة.")
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    msgs = []
    try:
        async for m in telethon_client.iter_messages(entity, limit=500):
            if m.text:
                msgs.append({
                    "text": m.text,
                    "date": m.date,
                    "chat_id": m.chat_id,
                    "message_id": m.id
                })
        if not msgs:
            await msg.reply("لا توجد رسائل نصية.")
            return
        index_messages(entity.id, msgs)
        # تحليل الكلمات
        all_text = " ".join([m["text"] for m in msgs])
        words = re.findall(r'\b\w{3,}\b', all_text.lower())
        stopwords = {'و','في','من','الى','على','عن','مع','لا','ما','هذا','ذلك','كان','قال','هو','هي','هم','الي','علي','بعد','قبل','ثم','عند'}
        filtered = [w for w in words if w not in stopwords]
        word_counter = Counter(filtered).most_common(20)
        hours = Counter()
        days = Counter()
        for m in msgs:
            if m["date"]:
                hours[m["date"].hour] += 1
                days[m["date"].strftime('%A')] += 1
        most_hour = max(hours, key=hours.get) if hours else 0
        most_day = max(days, key=days.get) if days else "غير معروف"
        user_messages_cache[msg.from_user.id] = {
            "words": word_counter,
            "activity": {"hour": most_hour, "count": hours[most_hour] if hours else 0, "day": most_day}
        }
        await msg.reply(
            f"✅ تم فهرسة {len(msgs)} رسالة.\n"
            f"🔹 استخدم <code>/words</code> لعرض أكثر الكلمات تكراراً\n"
            f"🔹 استخدم <code>/activity</code> لعرض ساعة الذروة\n"
            f"🔹 استخدم <code>/search_word {target} كلمة</code> للبحث في رسائله"
        )
    except Exception as e:
        await msg.reply(f"⚠️ خطأ: {str(e)[:200]}")

@dp.message(Command("words"))
async def cmd_words(msg: types.Message):
    data = user_messages_cache.get(msg.from_user.id)
    if not data or "words" not in data:
        await msg.reply("لم تقم بتحميل رسائل بعد. استخدم <code>/messages @username</code> أولاً.", parse_mode="HTML")
        return
    resp = "<b>📊 أكثر الكلمات تكراراً:</b>\n"
    for w, c in data["words"][:20]:
        resp += f"• <code>{w}</code> : {c} مرة\n"
    await msg.reply(resp, parse_mode="HTML")

@dp.message(Command("activity"))
async def cmd_activity(msg: types.Message):
    data = user_messages_cache.get(msg.from_user.id)
    if not data or "activity" not in data:
        await msg.reply("لم تقم بتحميل رسائل بعد. استخدم <code>/messages @username</code> أولاً.", parse_mode="HTML")
        return
    act = data["activity"]
    await msg.reply(f"<b>⏰ نشاط المستخدم</b>\n🕒 ساعة الذروة: {act['hour']}:00 ({act['count']} رسالة)\n📅 أكثر يوم نشاط: {act['day']}", parse_mode="HTML")

# ===================== تاريخ الأسماء السابقة =====================
@dp.message(Command("history"))
async def cmd_history(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/history @username</code>", parse_mode="HTML")
        return
    target = parts[1].strip()
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    history = get_name_history(entity.id)
    if not history:
        await msg.reply("ℹ️ لا يوجد سجل سابق لهذا المستخدم. استخدم <code>/info</code> لتسجيل الحالة الحالية.", parse_mode="HTML")
        return
    resp = f"<b>📜 جميع الأسماء السابقة لـ {entity.first_name or entity.username} (مع التوقيت)</b>\n\n"
    for h in history[-30:]:
        resp += f"• <code>{h['username'] or 'بدون'}</code> | {h['first_name'] or ''} {h['last_name'] or ''} — {h['changed_at']}\n"
    await msg.reply(resp, parse_mode="HTML")

# ===================== البحث عن كلمة في رسائل المستخدم =====================
@dp.message(Command("search_word"))
async def cmd_search_word(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply("استخدم: <code>/search_word @username الكلمة</code>", parse_mode="HTML")
        return
    target = parts[1]
    keyword = parts[2]
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    await msg.reply(f"🔎 البحث عن كلمة <b>{keyword}</b> في رسائل {target} ...")
    results = search_word_in_user_messages(entity.id, keyword)
    if not results:
        await msg.reply(f"لم يتم العثور على رسائل تحتوي على <b>{keyword}</b>. تأكد من فهرسة رسائل المستخدم باستخدام <code>/messages {target}</code> أولاً.", parse_mode="HTML")
        return
    resp = f"<b>📄 نتائج البحث عن '{keyword}' في رسائل {target}</b>\n\n"
    for text, date in results[:20]:
        snippet = text[:150].replace('\n', ' ')
        resp += f"• {snippet}...\n  📅 {date}\n\n"
    await msg.reply(resp, parse_mode="HTML")

# ===================== البحث العالمي =====================
@dp.message(Command("global_search"))
async def cmd_global_search(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/global_search كلمة</code>", parse_mode="HTML")
        return
    keyword = parts[1]
    await msg.reply(f"🌍 بحث عالمي عن كلمة <b>{keyword}</b> في جميع الرسائل المفهرسة ...")
    results = global_search(keyword)
    if not results:
        await msg.reply("لم يتم العثور على نتائج. تأكد من فهرسة رسائل بعض المستخدمين باستخدام <code>/messages</code>.", parse_mode="HTML")
        return
    resp = f"<b>📚 نتائج البحث العالمي عن '{keyword}'</b>\n\n"
    for user_id, text, date in results[:30]:
        resp += f"👤 معرف المستخدم: <code>{user_id}</code>\n📝 {text[:150]}...\n📅 {date}\n\n"
    await msg.reply(resp, parse_mode="HTML")

# ===================== مراقبة التغييرات =====================
monitor_tasks = {}

@dp.message(Command("monitor"))
async def cmd_monitor(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/monitor @username</code> (سيتم المراقبة كل ساعة)", parse_mode="HTML")
        return
    target = parts[1].strip()
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    await msg.reply(f"🔔 بدأ مراقبة {target}. سيتم إشعارك عند تغيير الاسم.")
    async def monitor(chat_id, user_id, identifier):
        last_state = None
        while True:
            try:
                entity = await telethon_client.get_entity(identifier)
                current = (entity.username, entity.first_name, entity.last_name)
                if last_state is None:
                    last_state = current
                    await update_user_name_history(entity.id, current[0], current[1], current[2])
                else:
                    if last_state != current:
                        await update_user_name_history(entity.id, current[0], current[1], current[2])
                        changes = []
                        if last_state[0] != current[0]:
                            changes.append(f"يوزرنيم: {last_state[0]} → {current[0]}")
                        if last_state[1] != current[1]:
                            changes.append(f"الاسم الأول: {last_state[1]} → {current[1]}")
                        if last_state[2] != current[2]:
                            changes.append(f"الاسم الأخير: {last_state[2]} → {current[2]}")
                        if changes:
                            await bot.send_message(chat_id, f"🔔 <b>تغيير في {identifier}</b>\n" + "\n".join(changes), parse_mode="HTML")
                        last_state = current
                await asyncio.sleep(3600)
            except Exception as e:
                await bot.send_message(chat_id, f"⚠️ خطأ في المراقبة: {str(e)[:200]}")
                await asyncio.sleep(3600)
    task = asyncio.create_task(monitor(msg.chat.id, entity.id, target))
    monitor_tasks[(msg.chat.id, target)] = task

# ===================== البحث في مجموعة باستخدام المعرف =====================
@dp.message(Command("search_in_chat"))
async def cmd_search_in_chat(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.reply("استخدم: <code>/search_in_chat @username معرف_المجموعة</code>\nالمعرف: رقم المجموعة (مثال: -1001234567890)", parse_mode="HTML")
        return
    target = parts[1]
    chat_id = int(parts[2])
    entity_user = await fetch_user_entity(target)
    if not entity_user:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    await msg.reply(f"🔍 البحث عن {target} في المجموعة {chat_id} ...")
    try:
        participant = await telethon_client(GetParticipantRequest(chat_id, entity_user))
        if participant:
            await msg.reply(f"✅ المستخدم {target} موجود في هذه المجموعة.\n🆔 معرفه: {entity_user.id}")
            # جلب آخر 10 رسائل له في هذه المجموعة
            msgs = []
            async for m in telethon_client.iter_messages(chat_id, from_user=entity_user.id, limit=10):
                if m.text:
                    msgs.append(m.text[:200])
            if msgs:
                resp = "<b>📨 آخر رسائله في المجموعة:</b>\n"
                for txt in msgs:
                    resp += f"• {txt}\n"
                await msg.reply(resp, parse_mode="HTML")
    except Exception as e:
        await msg.reply(f"❌ المستخدم غير موجود في هذه المجموعة أو لا يمكن الوصول إليها.\n{str(e)[:100]}")

# ===================== الفحص الشامل (Full Scan) =====================
async def query_external_bot(bot_username: str, query: str, timeout=30):
    """محاكاة التفاعل مع بوت خارجي وإرجاع أول رد نصي يتلقاه"""
    try:
        async with telethon_client.conversation(bot_username, timeout=timeout) as conv:
            await conv.send_message(query)
            response = await conv.get_response(timeout=timeout)
            return response.text if response and response.text else "لم أتلقَ رداً."
    except Exception as e:
        return f"حدث خطأ: {str(e)}"

@dp.message(Command("full_scan"))
async def cmd_full_scan(msg: types.Message):
    if not await ensure_telethon():
        await msg.reply("❌ البوت غير متصل.")
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/full_scan @username</code>", parse_mode="HTML")
        return
    target = parts[1].strip()
    entity = await fetch_user_entity(target)
    if not entity:
        await msg.reply("❌ المستخدم غير موجود.")
        return
    await msg.reply(f"🧩 بدء الفحص الشامل للمستخدم {target}. قد يستغرق دقيقة أو أكثر...")
    # 1. الاستعلام من البوتات الخارجية
    tgdb_result = await query_external_bot("@tgdb_search_bot", target)
    universal_result = await query_external_bot("@UniversalSearchSmartBot", target)
    # 2. البحث في روابط الدعوة المخزنة (من INVITE_LINKS أو من قاعدة البيانات)
    groups_found = []
    all_links = INVITE_LINKS + [g['link'] for g in get_all_cached_groups() if g.get('link')]
    for link in all_links:
        try:
            group_entity = await telethon_client.get_entity(link)
            async for member in telethon_client.iter_participants(group_entity, limit=2000):
                if member.id == entity.id:
                    groups_found.append({
                        "title": getattr(group_entity, 'title', 'بدون عنوان'),
                        "link": link
                    })
                    break
            await asyncio.sleep(random.uniform(1, 3))
        except:
            continue
    # 3. تحليل الرسائل الأساسية (آخر 100 رسالة إن لم تكن مفهرسة)
    msgs = []
    async for m in telethon_client.iter_messages(entity.id, limit=100):
        if m.text:
            msgs.append(m.text)
    # 4. تجميع التقرير
    output = f"<b>📊 تقرير شامل عن المستخدم {target}</b>\n\n"
    output += "<b>🤖 نتائج البوتات الخارجية:</b>\n"
    output += f"🔹 TgDB: {tgdb_result[:400]}\n"
    output += f"🔹 UniversalSearch: {universal_result[:400]}\n\n"
    output += "<b>📂 مجموعات/قنوات تم العثور عليه فيها عبر الروابط:</b>\n"
    if groups_found:
        for g in groups_found[:20]:
            output += f"✔️ <a href='{g['link']}'>{g['title']}</a>\n"
    else:
        output += "❌ لم يتم العثور على العضوية في أي من الروابط المخزنة.\n"
    output += f"\n<b>💬 آخر {len(msgs)} رسالة (نموذج):</b>\n"
    for txt in msgs[:5]:
        output += f"• {txt[:150]}...\n"
    await msg.reply(output, parse_mode="HTML", disable_web_page_preview=True)

# ===================== إضافة رابط مجموعة يدوياً =====================
@dp.message(Command("add_link"))
async def cmd_add_link(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("استخدم: <code>/add_link https://t.me/joinchat/xxxx</code>", parse_mode="HTML")
        return
    link = parts[1].strip()
    if not link.startswith("https://t.me/"):
        await msg.reply("❌ الرابط غير صالح. يجب أن يبدأ بـ https://t.me/")
        return
    try:
        entity = await telethon_client.get_entity(link)
        group_id = entity.id
        title = getattr(entity, 'title', 'بدون عنوان')
        username = getattr(entity, 'username', None)
        save_group_cache(group_id, title, username, link)
        await msg.reply(f"✅ تم حفظ الرابط: {title}")
    except Exception as e:
        await msg.reply(f"❌ فشل في حفظ الرابط: {str(e)[:200]}")

# ===================== معالجة الأزرار =====================
@dp.message(lambda msg: msg.text == "🔍 معلومات مستخدم")
async def btn_info(msg: types.Message):
    await msg.reply("أرسل معرف المستخدم (@username أو +رقم أو ID):")

@dp.message(lambda msg: msg.text == "📢 قنوات مشتركة")
async def btn_common(msg: types.Message):
    await msg.reply("أرسل @username لمعرفة القنوات المشتركة معه:")

@dp.message(lambda msg: msg.text == "📨 تحليل رسائل")
async def btn_messages(msg: types.Message):
    await msg.reply("أرسل @username لتحليل رسائله:")

@dp.message(lambda msg: msg.text == "📊 كلمات متكررة")
async def btn_words_btn(msg: types.Message):
    await cmd_words(msg)

@dp.message(lambda msg: msg.text == "⏰ نشاط المستخدم")
async def btn_activity_btn(msg: types.Message):
    await cmd_activity(msg)

@dp.message(lambda msg: msg.text == "📜 تاريخ الأسماء")
async def btn_history_btn(msg: types.Message):
    await msg.reply("أرسل @username لعرض تاريخ تغيير أسمائه:")

@dp.message(lambda msg: msg.text == "🔎 بحث بكلمة")
async def btn_search(msg: types.Message):
    await msg.reply("أرسل <code>@username الكلمة</code>", parse_mode="HTML")

@dp.message(lambda msg: msg.text == "🌍 بحث عالمي")
async def btn_global(msg: types.Message):
    await msg.reply("أرسل الكلمة التي تريد البحث عنها في جميع الرسائل المفهرسة:")

@dp.message(lambda msg: msg.text == "🔔 مراقبة تغييرات")
async def btn_monitor(msg: types.Message):
    await msg.reply("أرسل @username لبدء مراقبته بشكل دائم:")

@dp.message(lambda msg: msg.text == "⚙️ إعدادات")
async def btn_settings(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 إعادة تعيين البوت", callback_data="reset_bot")],
        [InlineKeyboardButton(text="📊 إحصائيات البوت", callback_data="bot_stats")],
        [InlineKeyboardButton(text="💾 نسخ احتياطي للبيانات", callback_data="backup_db")]
    ])
    await msg.reply("<b>⚙️ الإعدادات والإدارة</b>", parse_mode="HTML", reply_markup=kb)

@dp.message(lambda msg: msg.text == "🧩 فحص شامل (Full Scan)")
async def btn_full_scan(msg: types.Message):
    await msg.reply("أرسل @username لبدء الفحص الشامل:")

@dp.message(lambda msg: msg.text == "➕ إضافة رابط مجموعة")
async def btn_add_link(msg: types.Message):
    await msg.reply("أرسل رابط الدعوة (مثال: https://t.me/joinchat/xxxx)")

# ===================== معالجة الـ Callback =====================
@dp.callback_query()
async def handle_callback(call: types.CallbackQuery):
    data = call.data
    if data == "reset_bot":
        await cmd_reset(call.message, None)
        await call.answer("تم مسح البيانات.")
    elif data == "bot_stats":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT user_id) FROM name_history")
        users = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM messages_index")
        msgs = c.fetchone()[0] or 0
        conn.close()
        await call.message.reply(f"<b>📊 إحصائيات البوت</b>\n👥 عدد المستخدمين المتتبعين: {users}\n📨 عدد الرسائل المفهرسة: {msgs}", parse_mode="HTML")
    elif data == "backup_db":
        await call.message.reply_document(FSInputFile(DB_PATH), caption="📁 نسخة احتياطية من قاعدة البيانات")
    await call.answer()

# ===================== أمر المساعدة =====================
@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    help_text = (
        "<b>🔍 بوت OSINT - المساعدة التفصيلية</b>\n\n"
        "<b>الأوامر الأساسية:</b>\n"
        "• /info @username - معلومات شاملة عن المستخدم\n"
        "• /common @username - القنوات/المجموعات المشتركة (العامة)\n"
        "• /messages @username - جلب آخر 500 رسالة وفهرستها\n"
        "• /words - عرض أكثر الكلمات تكراراً (بعد تحميل الرسائل)\n"
        "• /activity - عرض ساعات الذروة وأكثر يوم نشاط\n"
        "• /history @username - عرض جميع الأسماء السابقة مع التوقيت\n"
        "• /search_word @username كلمة - البحث عن كلمة في رسائل المستخدم\n"
        "• /global_search كلمة - البحث في رسائل جميع المستخدمين المفهرسين\n"
        "• /search_in_chat @username group_id - التحقق من عضويته في مجموعة معينة\n"
        "• /monitor @username - مراقبة تغييرات الاسم بشكل دوري\n"
        "• /full_scan @username - فحص شامل باستخدام بوتات خارجية وروابط مخزنة\n"
        "• /add_link https://t.me/... - إضافة رابط مجموعة للبحث فيها لاحقاً\n"
        "• /reset - إعادة تعيين بيانات API (لإعادة الإعداد)\n\n"
        "<b>💡 ملاحظات:</b>\n"
        "• يمكنك استخدام الأزرار للتنقل السريع.\n"
        "• للبحث بالرقم أو المعرف، استخدم <code>/info +123456</code> أو <code>/info 123456789</code>.\n"
        "• لاستخدام ميزة البحث عبر البوتات الخارجية، تأكد من أن حسابك ليس محدوداً.\n"
        "• جميع البيانات مخزنة محلياً في قاعدة بيانات SQLite."
    )
    await msg.reply(help_text, parse_mode="HTML")

# ===================== تشغيل البوت =====================
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
