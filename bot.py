"""
KODLI KINO BOT
--------------
Replit uchun bitta faylli Telegram bot.

O'rnatish:
    pip install aiogram

Ishga tushirish:
    python bot.py

Muhim:
1. TOKEN va ADMIN_ID qiymatlarini kiriting.
2. REQUIRED_CHANNELS ichiga kanal username'larini yozing:
   ["@kanal_username"].
3. Bot majburiy obuna kanallarida administrator bo'lishi kerak.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date
from html import escape
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)


# ============================================================
# ASOSIY SOZLAMALAR — shu yerga o'zingizning qiymatlaringizni yozing
# ============================================================
TOKEN = os.getenv("BOT_TOKEN", "8482208866:AAGK1r11SeZ8Vs4xNHCZv22ACkatvoBB5Pc")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5469349844"))

# Kanal username'i @ bilan yoziladi. Masalan: ["@mening_kanalim"]
# Admin panel orqali keyinchalik o'zgartirish mumkin.
REQUIRED_CHANNELS = ["@kanal_username"]

DB_NAME = "kino_bot.db"
SPAM_LIMIT = 8          # 30 soniyada ruxsat etilgan so'rovlar soni
SPAM_WINDOW = 30
REFERRAL_BONUS = 2      # taklif qilingan har bir do'st uchun bonus ball
DAILY_BONUS = 1


# ============================================================
# LOG YOZISH
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kodli_kino_bot")


# ============================================================
# SQLITE BAZA
# ============================================================
class Database:
    """SQLite bilan ishlash uchun sodda yordamchi klass."""

    def __init__(self, path: str):
        self.path = path
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.init()

    def init(self) -> None:
        """Barcha kerakli jadvallarni yaratadi."""
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                status TEXT DEFAULT 'user',
                balance INTEGER DEFAULT 0,
                referral_count INTEGER DEFAULT 0,
                referred_by INTEGER,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_bonus TEXT
            );

            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT 'Boshqa',
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                access_level TEXT DEFAULT 'public',
                views INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                movie_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                UNIQUE(user_id, movie_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        channels = self.get_setting("required_channels")
        if channels is None:
            self.set_setting("required_channels", json.dumps(REQUIRED_CHANNELS))
        self.connection.commit()

    def get_setting(self, key: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    def get_channels(self) -> list[str]:
        raw = self.get_setting("required_channels")
        if not raw:
            return []
        try:
            channels = json.loads(raw)
            return [str(item).strip() for item in channels if str(item).strip()]
        except json.JSONDecodeError:
            return []

    def register_user(self, user_id: int, username: str, full_name: str) -> bool:
        """Yangi foydalanuvchini ro'yxatdan o'tkazadi. True — yangi user."""
        existing = self.connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing:
            self.connection.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                (username or "", full_name or "", user_id),
            )
            self.connection.commit()
            return False
        self.connection.execute(
            """
            INSERT INTO users(user_id, username, full_name)
            VALUES(?, ?, ?)
            """,
            (user_id, username or "", full_name or ""),
        )
        self.connection.commit()
        return True

    def add_referral(self, new_user_id: int, referrer_id: int) -> bool:
        """Referralni bir marta bog'laydi va taklif qiluvchiga bonus beradi."""
        if new_user_id == referrer_id:
            return False
        row = self.connection.execute(
            "SELECT referred_by FROM users WHERE user_id = ?", (new_user_id,)
        ).fetchone()
        referrer = self.connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)
        ).fetchone()
        if not row or row["referred_by"] or not referrer:
            return False
        self.connection.execute(
            """
            UPDATE users
            SET referred_by = ?, balance = balance + ?, referral_count = referral_count + 1
            WHERE user_id = ?
            """,
            (referrer_id, REFERRAL_BONUS, referrer_id),
        )
        self.connection.commit()
        return True

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

    def set_status(self, user_id: int, status: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def claim_daily_bonus(self, user_id: int) -> bool:
        today = date.today().isoformat()
        row = self.get_user(user_id)
        if not row or row["last_bonus"] == today:
            return False
        self.connection.execute(
            "UPDATE users SET balance = balance + ?, last_bonus = ? WHERE user_id = ?",
            (DAILY_BONUS, today, user_id),
        )
        self.connection.commit()
        return True

    def add_movie(
        self,
        code: str,
        name: str,
        description: str,
        category: str,
        media_type: str,
        file_id: str,
        access_level: str,
    ) -> bool:
        try:
            self.connection.execute(
                """
                INSERT INTO movies(
                    code, name, description, category, media_type, file_id, access_level
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    description,
                    category,
                    media_type,
                    file_id,
                    access_level,
                ),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_movie(self, code: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM movies WHERE code = ?", (code,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def get_movie(self, code: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM movies WHERE code = ?", (code,)
        ).fetchone()

    def increase_views(self, movie_id: int) -> None:
        self.connection.execute(
            "UPDATE movies SET views = views + 1 WHERE id = ?", (movie_id,)
        )
        self.connection.commit()

    def search_movies(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        pattern = f"%{query}%"
        return self.connection.execute(
            """
            SELECT * FROM movies
            WHERE name LIKE ? OR code LIKE ? OR category LIKE ?
            ORDER BY views DESC, created_at DESC LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        ).fetchall()

    def latest_movies(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM movies ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def popular_movies(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM movies ORDER BY views DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def categories(self) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT category, COUNT(*) AS total FROM movies
            GROUP BY category ORDER BY total DESC
            """
        ).fetchall()
        return [f"{row['category']} ({row['total']})" for row in rows]

    def movies_by_category(self, category: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM movies WHERE category = ?
            ORDER BY views DESC, created_at DESC LIMIT ?
            """,
            (category, limit),
        ).fetchall()

    def rate_movie(self, user_id: int, movie_id: int, rating: int) -> None:
        self.connection.execute(
            """
            INSERT INTO ratings(user_id, movie_id, rating) VALUES(?, ?, ?)
            ON CONFLICT(user_id, movie_id) DO UPDATE SET rating = excluded.rating
            """,
            (user_id, movie_id, rating),
        )
        self.connection.commit()

    def movie_rating(self, movie_id: int) -> tuple[float, int]:
        row = self.connection.execute(
            "SELECT AVG(rating) AS average, COUNT(*) AS total FROM ratings WHERE movie_id = ?",
            (movie_id,),
        ).fetchone()
        return (round(row["average"] or 0, 1), row["total"])

    def stats(self) -> dict[str, Any]:
        users = self.connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        movies = self.connection.execute("SELECT COUNT(*) AS n FROM movies").fetchone()["n"]
        views = self.connection.execute("SELECT COALESCE(SUM(views), 0) AS n FROM movies").fetchone()["n"]
        vip = self.connection.execute(
            "SELECT COUNT(*) AS n FROM users WHERE status = 'vip'"
        ).fetchone()["n"]
        premium = self.connection.execute(
            "SELECT COUNT(*) AS n FROM users WHERE status = 'premium'"
        ).fetchone()["n"]
        return {
            "users": users,
            "movies": movies,
            "views": views,
            "vip": vip,
            "premium": premium,
        }

    def all_user_ids(self) -> list[int]:
        rows = self.connection.execute("SELECT user_id FROM users").fetchall()
        return [row["user_id"] for row in rows]

    def close(self) -> None:
        self.connection.close()


db = Database(DB_NAME)
router = Router()
dp = Dispatcher()
dp.include_router(router)


# ============================================================
# FSM HOLATLARI
# ============================================================
class AddMovieStates(StatesGroup):
    code = State()
    name = State()
    description = State()
    category = State()
    access = State()
    media = State()


class DeleteMovieStates(StatesGroup):
    code = State()


class BroadcastStates(StatesGroup):
    message = State()


class StatusStates(StatesGroup):
    user_id = State()
    status = State()


class ChannelStates(StatesGroup):
    channels = State()


class SearchStates(StatesGroup):
    query = State()


# ============================================================
# TUGMALAR
# ============================================================
def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Foydalanuvchining asosiy reply keyboard menyusi."""
    rows = [
        [KeyboardButton(text="🎬 Kino olish"), KeyboardButton(text="🔎 Qidirish")],
        [KeyboardButton(text="🔥 Mashhur kinolar"), KeyboardButton(text="🆕 So'nggi kinolar")],
        [KeyboardButton(text="📂 Kategoriyalar"), KeyboardButton(text="💎 Statusim")],
        [KeyboardButton(text="🎁 Kunlik bonus"), KeyboardButton(text="👥 Referal")],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="👑 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def subscription_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    """Majburiy obuna kanallari uchun inline tugmalar."""
    rows = []
    for channel in channels:
        clean = channel.strip()
        if clean.startswith("@"):
            url = f"https://t.me/{clean[1:]}"
            label = f"📢 {clean}"
        elif clean.startswith("https://t.me/"):
            url = clean
            label = "📢 Kanalga o'tish"
        else:
            # Kanal ID bo'lsa invite link avtomatik aniqlanmaydi.
            url = "https://t.me/"
            label = f"📢 {clean}"
        rows.append([InlineKeyboardButton(text=label, url=url)])
    rows.append(
        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subs")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_inline_keyboard() -> InlineKeyboardMarkup:
    """Start xabari uchun zamonaviy inline menyu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Kino olish", callback_data="get_movie"),
                InlineKeyboardButton(text="🔎 Qidirish", callback_data="search"),
            ],
            [
                InlineKeyboardButton(text="🔥 Mashhur", callback_data="popular"),
                InlineKeyboardButton(text="🆕 Yangi", callback_data="latest"),
            ],
            [
                InlineKeyboardButton(text="📂 Kategoriyalar", callback_data="categories"),
                InlineKeyboardButton(text="💎 Statusim", callback_data="my_status"),
            ],
        ]
    )


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Admin panel reply keyboard menyusi."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Kino qo'shish"), KeyboardButton(text="🗑 Kino o'chirish")],
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="📣 Broadcast")],
            [KeyboardButton(text="👤 Status berish"), KeyboardButton(text="🧹 Status olish")],
            [KeyboardButton(text="📢 Kanallarni sozlash"), KeyboardButton(text="📋 Kino ro'yxati")],
            [KeyboardButton(text="⬅️ Asosiy menyu")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )


def movie_rating_keyboard(movie_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ 1", callback_data=f"rate:{movie_id}:1"),
                InlineKeyboardButton(text="⭐ 2", callback_data=f"rate:{movie_id}:2"),
                InlineKeyboardButton(text="⭐ 3", callback_data=f"rate:{movie_id}:3"),
                InlineKeyboardButton(text="⭐ 4", callback_data=f"rate:{movie_id}:4"),
                InlineKeyboardButton(text="⭐ 5", callback_data=f"rate:{movie_id}:5"),
            ]
        ]
    )


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def status_label(status: str) -> str:
    return {
        "user": "👤 Oddiy foydalanuvchi",
        "vip": "💎 VIP foydalanuvchi",
        "premium": "👑 PREMIUM foydalanuvchi",
    }.get(status, "👤 Oddiy foydalanuvchi")


def access_label(access: str) -> str:
    return {
        "public": "🌐 Hamma uchun",
        "vip": "💎 VIP va PREMIUM",
        "premium": "👑 Faqat PREMIUM",
    }.get(access, access)


def can_access(user_status: str, access_level: str) -> bool:
    if access_level == "public":
        return True
    if access_level == "vip":
        return user_status in {"vip", "premium"}
    if access_level == "premium":
        return user_status == "premium"
    return False


def format_movie_list(movies: list[sqlite3.Row], title: str) -> str:
    if not movies:
        return f"{title}\n\n😔 Hozircha bu bo'limda kino yo'q."
    lines = [title, ""]
    for movie in movies:
        average, total = db.movie_rating(movie["id"])
        lines.append(
            f"🎬 <b>{escape(movie['name'])}</b>\n"
            f"🔢 Kod: <code>{escape(movie['code'])}</code> | "
            f"👁 {movie['views']} | ⭐ {average} ({total})\n"
            f"📂 {escape(movie['category'])}"
        )
        lines.append("")
    lines.append("📌 Kino olish uchun uning kodini yuboring.")
    return "\n".join(lines)


def is_rate_limited(user_id: int) -> bool:
    """Oddiy xotiradagi spam himoya: bitta userga vaqt oynasida limit."""
    now = time.monotonic()
    history = spam_history.setdefault(user_id, [])
    spam_history[user_id] = [stamp for stamp in history if now - stamp < SPAM_WINDOW]
    if len(spam_history[user_id]) >= SPAM_LIMIT:
        return True
    spam_history[user_id].append(now)
    return False


async def check_subscriptions(bot: Bot, user_id: int) -> bool:
    """User barcha majburiy kanallarga a'zo ekanini tekshiradi."""
    channels = db.get_channels()
    if not channels:
        return True
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            }:
                return False
        except Exception as error:
            logger.warning("Obuna tekshirishda xato (%s): %s", channel, error)
            return False
    return True


async def require_subscription(message: Message, bot: Bot) -> bool:
    """Obuna talabini tekshiradi va kerak bo'lsa tugma chiqaradi."""
    if await check_subscriptions(bot, message.from_user.id):
        return True
    channels = db.get_channels()
    await message.answer(
        "🔒 <b>Davom etish uchun kanal(lar)ga obuna bo'ling.</b>\n\n"
        "Obuna bo'lgach, «✅ Tekshirish» tugmasini bosing.",
        reply_markup=subscription_keyboard(channels),
    )
    return False


async def send_movie(message: Message, movie: sqlite3.Row) -> None:
    """Kino media faylini nomi, tavsifi va ratingi bilan yuboradi."""
    user = db.get_user(message.from_user.id)
    user_status = user["status"] if user else "user"
    if not can_access(user_status, movie["access_level"]) and not is_admin(message.from_user.id):
        await message.answer(
            "🔐 Bu kino maxsus status uchun.\n\n"
            f"Talab qilinadigan status: <b>{access_label(movie['access_level'])}</b>\n"
            "Admin bilan bog'laning."
        )
        return

    average, total = db.movie_rating(movie["id"])
    caption = (
        f"🎬 <b>{escape(movie['name'])}</b>\n\n"
        f"📝 {escape(movie['description'] or 'Tavsif kiritilmagan.')}\n\n"
        f"📂 Kategoriya: <b>{escape(movie['category'])}</b>\n"
        f"🔢 Kod: <code>{escape(movie['code'])}</code>\n"
        f"⭐ Reyting: <b>{average}/5</b> ({total} ta ovoz)\n"
        f"👁 Ko'rishlar: {movie['views'] + 1}"
    )
    db.increase_views(movie["id"])
    if movie["media_type"] == "video":
        await message.answer_video(
            video=movie["file_id"],
            caption=caption,
            supports_streaming=True,
            reply_markup=movie_rating_keyboard(movie["id"]),
        )
    else:
        await message.answer_document(
            document=movie["file_id"],
            caption=caption,
            reply_markup=movie_rating_keyboard(movie["id"]),
        )


async def show_movies(message: Message, movies: list[sqlite3.Row], title: str) -> None:
    await message.answer(
        format_movie_list(movies, title),
        reply_markup=main_keyboard(message.from_user.id),
    )


async def cancel_if_requested(message: Message, state: FSMContext) -> bool:
    if message.text and message.text.strip() == "❌ Bekor qilish":
        await state.clear()
        await message.answer(
            "✅ Amal bekor qilindi.",
            reply_markup=main_keyboard(message.from_user.id),
        )
        return True
    return False


spam_history: dict[int, list[float]] = {}


# ============================================================
# FOYDALANUVCHI HANDLERLARI
# ============================================================
@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user = message.from_user
    is_new = db.register_user(user.id, user.username or "", user.full_name)
    referral_added = False
    if is_new and message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                referral_added = db.add_referral(user.id, int(parts[1][4:]))
            except ValueError:
                pass

    if referral_added:
        await message.answer("🎁 Referral orqali ro'yxatdan o'tdingiz. Do'stingizga bonus berildi!")

    if not await require_subscription(message, bot):
        return

    await message.answer(
        "🎬 <b>KODLI KINO BOT</b> ga xush kelibsiz!\n\n"
        "🍿 Kino kodini yuboring — sevimli filmingizni tezda oling.\n"
        "🔎 Qidirish, kategoriyalar va mashhur kinolar ham mavjud.\n\n"
        "📌 Masalan: <code>101</code> yoki <code>202</code>",
        reply_markup=main_keyboard(user.id),
    )
    await message.answer("✨ Asosiy menyu:", reply_markup=user_inline_keyboard())


@router.callback_query(F.data == "check_subs")
async def check_subs_callback(callback: CallbackQuery, bot: Bot) -> None:
    if await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            "✅ Obuna tasdiqlandi! Endi kino kodini yuborishingiz mumkin.",
            reply_markup=main_keyboard(callback.from_user.id),
        )
        await callback.answer("Obuna tasdiqlandi ✅")
    else:
        await callback.answer("Hali barcha kanallarga obuna bo'lmagansiz.", show_alert=True)


@router.callback_query(F.data == "get_movie")
async def get_movie_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            "🔒 Avval majburiy kanallarga obuna bo'ling.",
            reply_markup=subscription_keyboard(db.get_channels()),
        )
        await callback.answer()
        return
    await state.set_state(SearchStates.query)
    await callback.message.answer(
        "🎬 Kino kodini yuboring:\n\nMasalan: <code>101</code>",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "search")
async def search_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            "🔒 Avval majburiy kanallarga obuna bo'ling.",
            reply_markup=subscription_keyboard(db.get_channels()),
        )
        await callback.answer()
        return
    await state.set_state(SearchStates.query)
    await callback.message.answer(
        "🔎 Kino nomi, kodi yoki kategoriyasini yozing:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "🎬 Kino olish")
async def get_movie_button(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await require_subscription(message, bot):
        return
    await state.set_state(SearchStates.query)
    await message.answer("🎬 Kino kodini yuboring:", reply_markup=cancel_keyboard())


@router.message(F.text == "🔎 Qidirish")
async def search_button(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await require_subscription(message, bot):
        return
    await state.set_state(SearchStates.query)
    await message.answer(
        "🔎 Kino nomi, kodi yoki kategoriyasini yozing:",
        reply_markup=cancel_keyboard(),
    )


@router.message(SearchStates.query)
async def search_query_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if await cancel_if_requested(message, state):
        return
    if not await require_subscription(message, bot):
        return
    if not message.text:
        await message.answer("⚠️ Iltimos, matn yoki kino kodini yuboring.")
        return
    if is_rate_limited(message.from_user.id):
        await message.answer("🛑 Juda ko'p so'rov yuborildi. 30 soniyadan keyin qayta urinib ko'ring.")
        return

    query = message.text.strip()
    exact_movie = db.get_movie(query)
    if exact_movie:
        await state.clear()
        await send_movie(message, exact_movie)
        return
    movies = db.search_movies(query)
    await state.clear()
    if movies:
        await show_movies(message, movies, f"🔎 <b>Qidiruv natijalari:</b> {escape(query)}")
    else:
        await message.answer(
            "😔 Afsuski, bu kod yoki nom bo'yicha kino topilmadi.\n"
            "🔢 Kodni tekshirib, qayta yuboring.",
            reply_markup=main_keyboard(message.from_user.id),
        )


@router.message(F.text == "🔥 Mashhur kinolar")
async def popular_button(message: Message, bot: Bot) -> None:
    if await require_subscription(message, bot):
        await show_movies(message, db.popular_movies(), "🔥 <b>Eng mashhur kinolar</b>")


@router.callback_query(F.data == "popular")
async def popular_callback(callback: CallbackQuery, bot: Bot) -> None:
    if await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            format_movie_list(db.popular_movies(), "🔥 <b>Eng mashhur kinolar</b>"),
            reply_markup=main_keyboard(callback.from_user.id),
        )
    else:
        await callback.message.answer(
            "🔒 Obuna talab qilinadi.",
            reply_markup=subscription_keyboard(db.get_channels()),
        )
    await callback.answer()


@router.message(F.text == "🆕 So'nggi kinolar")
async def latest_button(message: Message, bot: Bot) -> None:
    if await require_subscription(message, bot):
        await show_movies(message, db.latest_movies(), "🆕 <b>So'nggi qo'shilgan kinolar</b>")


@router.callback_query(F.data == "latest")
async def latest_callback(callback: CallbackQuery, bot: Bot) -> None:
    if await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            format_movie_list(db.latest_movies(), "🆕 <b>So'nggi qo'shilgan kinolar</b>"),
            reply_markup=main_keyboard(callback.from_user.id),
        )
    else:
        await callback.message.answer(
            "🔒 Obuna talab qilinadi.",
            reply_markup=subscription_keyboard(db.get_channels()),
        )
    await callback.answer()


def categories_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for category in db.categories():
        name = category.rsplit(" (", 1)[0]
        rows.append(
            [InlineKeyboardButton(text=f"📂 {category}", callback_data=f"cat:{name}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="📭 Kategoriyalar bo'sh", callback_data="noop")
    ]])


@router.message(F.text == "📂 Kategoriyalar")
async def categories_button(message: Message, bot: Bot) -> None:
    if await require_subscription(message, bot):
        await message.answer("📂 <b>Kategoriya tanlang:</b>", reply_markup=categories_keyboard())


@router.callback_query(F.data == "categories")
async def categories_callback(callback: CallbackQuery, bot: Bot) -> None:
    if await check_subscriptions(bot, callback.from_user.id):
        await callback.message.answer(
            "📂 <b>Kategoriya tanlang:</b>", reply_markup=categories_keyboard()
        )
    else:
        await callback.message.answer(
            "🔒 Obuna talab qilinadi.",
            reply_markup=subscription_keyboard(db.get_channels()),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def category_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not await check_subscriptions(bot, callback.from_user.id):
        await callback.answer("Avval obuna bo'ling.", show_alert=True)
        return
    category = callback.data[4:]
    movies = db.movies_by_category(category)
    await callback.message.answer(
        format_movie_list(movies, f"📂 <b>{escape(category)}</b>"),
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:"))
async def rating_callback(callback: CallbackQuery) -> None:
    try:
        _, movie_id_text, rating_text = callback.data.split(":")
        movie_id, rating = int(movie_id_text), int(rating_text)
        if rating not in range(1, 6):
            raise ValueError
        db.rate_movie(callback.from_user.id, movie_id, rating)
        await callback.answer(f"Bahoyingiz qabul qilindi: {rating}/5 ⭐")
    except (ValueError, sqlite3.Error):
        await callback.answer("Baholashda xatolik yuz berdi.", show_alert=True)


@router.callback_query(F.data == "my_status")
async def status_callback(callback: CallbackQuery) -> None:
    row = db.get_user(callback.from_user.id)
    status = row["status"] if row else "user"
    balance = row["balance"] if row else 0
    await callback.message.answer(
        f"💎 <b>Sizning statusingiz:</b> {status_label(status)}\n"
        f"🎁 Bonus ballaringiz: <b>{balance}</b>",
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.message(F.text == "💎 Statusim")
async def status_button(message: Message, bot: Bot) -> None:
    if not await require_subscription(message, bot):
        return
    row = db.get_user(message.from_user.id)
    status = row["status"] if row else "user"
    balance = row["balance"] if row else 0
    await message.answer(
        f"💎 <b>Sizning statusingiz:</b> {status_label(status)}\n"
        f"🎁 Bonus ballaringiz: <b>{balance}</b>",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(F.text == "🎁 Kunlik bonus")
async def daily_bonus_handler(message: Message, bot: Bot) -> None:
    if not await require_subscription(message, bot):
        return
    if db.claim_daily_bonus(message.from_user.id):
        await message.answer(f"🎁 Tabriklaymiz! Sizga {DAILY_BONUS} bonus ball berildi.")
    else:
        await message.answer("⏳ Bugungi bonusni allaqachon olgansiz. Ertaga qayting!")


@router.message(F.text == "👥 Referal")
async def referral_handler(message: Message, bot: Bot) -> None:
    if not await require_subscription(message, bot):
        return
    row = db.get_user(message.from_user.id)
    count = row["referral_count"] if row else 0
    balance = row["balance"] if row else 0
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "👥 <b>Referal tizimi</b>\n\n"
        f"🔗 Sizning havolangiz:\n<code>{link}</code>\n\n"
        f"👤 Taklif qilinganlar: <b>{count}</b>\n"
        f"🎁 Bonus balans: <b>{balance}</b>\n\n"
        f"Har bir yangi foydalanuvchi uchun {REFERRAL_BONUS} bonus ball olasiz."
    )


# ============================================================
# ADMIN PANEL
# ============================================================
@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Sizda admin huquqi yo'q.")
        return
    await message.answer("👑 <b>Admin panel</b>\nKerakli amalni tanlang:", reply_markup=admin_keyboard())


@router.message(F.text == "👑 Admin panel")
async def admin_button(message: Message) -> None:
    if is_admin(message.from_user.id):
        await message.answer("👑 <b>Admin panel</b>\nKerakli amalni tanlang:", reply_markup=admin_keyboard())


@router.message(F.text == "➕ Kino qo'shish")
async def add_movie_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AddMovieStates.code)
    await message.answer("1/6 🔢 Kino kodini yuboring (masalan: <code>101</code>):", reply_markup=cancel_keyboard())


@router.message(AddMovieStates.code)
async def add_movie_code(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    code = (message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", code):
        await message.answer("⚠️ Kod faqat harf, raqam, _ yoki - dan iborat bo'lsin.")
        return
    if db.get_movie(code):
        await message.answer("⚠️ Bu kod allaqachon band. Boshqa kod kiriting.")
        return
    await state.update_data(code=code)
    await state.set_state(AddMovieStates.name)
    await message.answer("2/6 🎬 Kino nomini yuboring:")


@router.message(AddMovieStates.name)
async def add_movie_name(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Kino nomi bo'sh bo'lmasin.")
        return
    await state.update_data(name=name)
    await state.set_state(AddMovieStates.description)
    await message.answer("3/6 📝 Kino tavsifini yuboring (bo'lmasa «-» yozing):")


@router.message(AddMovieStates.description)
async def add_movie_description(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    description = (message.text or "").strip()
    await state.update_data(description="" if description == "-" else description)
    await state.set_state(AddMovieStates.category)
    await message.answer("4/6 📂 Kategoriya nomini yuboring (masalan: Komediya):")


@router.message(AddMovieStates.category)
async def add_movie_category(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    category = (message.text or "").strip() or "Boshqa"
    await state.update_data(category=category)
    await state.set_state(AddMovieStates.access)
    await message.answer(
        "5/6 🔐 Kino darajasini tanlang:\n\n"
        "🌐 <code>public</code> — hamma uchun\n"
        "💎 <code>vip</code> — VIP va PREMIUM\n"
        "👑 <code>premium</code> — faqat PREMIUM"
    )


@router.message(AddMovieStates.access)
async def add_movie_access(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    access = (message.text or "").strip().lower()
    if access not in {"public", "vip", "premium"}:
        await message.answer("⚠️ Faqat public, vip yoki premium yozing.")
        return
    await state.update_data(access_level=access)
    await state.set_state(AddMovieStates.media)
    await message.answer("6/6 🎥 Kino videosini yoki faylini yuboring:")


@router.message(AddMovieStates.media)
async def add_movie_media(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    if message.video:
        media_type, file_id = "video", message.video.file_id
    elif message.document:
        media_type, file_id = "document", message.document.file_id
    else:
        await message.answer("⚠️ Iltimos, video yoki hujjat ko'rinishida kino yuboring.")
        return
    data = await state.get_data()
    saved = db.add_movie(
        code=data["code"],
        name=data["name"],
        description=data.get("description", ""),
        category=data.get("category", "Boshqa"),
        media_type=media_type,
        file_id=file_id,
        access_level=data.get("access_level", "public"),
    )
    await state.clear()
    if saved:
        await message.answer(
            f"✅ Kino muvaffaqiyatli qo'shildi!\n\n"
            f"🎬 {escape(data['name'])}\n"
            f"🔢 Kod: <code>{escape(data['code'])}</code>\n"
            f"🔐 {access_label(data.get('access_level', 'public'))}",
            reply_markup=admin_keyboard(),
        )
    else:
        await message.answer("❌ Kino qo'shilmadi. Kodni tekshiring.", reply_markup=admin_keyboard())


@router.message(F.text == "🗑 Kino o'chirish")
async def delete_movie_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(DeleteMovieStates.code)
    await message.answer("🗑 O'chiriladigan kino kodini yuboring:", reply_markup=cancel_keyboard())


@router.message(DeleteMovieStates.code)
async def delete_movie_handler(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    code = (message.text or "").strip()
    movie = db.get_movie(code)
    if not movie:
        await message.answer("❌ Bu kodli kino topilmadi.")
        return
    db.delete_movie(code)
    await state.clear()
    await message.answer(
        f"✅ <b>{escape(movie['name'])}</b> o'chirildi.",
        reply_markup=admin_keyboard(),
    )


@router.message(F.text == "📋 Kino ro'yxati")
async def movie_list_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    movies = db.latest_movies(30)
    await message.answer(format_movie_list(movies, "📋 <b>Kino ro'yxati</b>"), reply_markup=admin_keyboard())


@router.message(F.text == "📊 Statistika")
async def statistics_handler(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    stats = db.stats()
    await message.answer(
        "📊 <b>BOT STATISTIKASI</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{stats['users']}</b>\n"
        f"🎬 Kinolar: <b>{stats['movies']}</b>\n"
        f"👁 Jami ko'rishlar: <b>{stats['views']}</b>\n"
        f"💎 VIP: <b>{stats['vip']}</b>\n"
        f"👑 PREMIUM: <b>{stats['premium']}</b>\n"
        f"📢 Majburiy kanallar: <b>{len(db.get_channels())}</b>",
        reply_markup=admin_keyboard(),
    )


@router.message(F.text == "📣 Broadcast")
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastStates.message)
    await message.answer(
        "📣 Hamma foydalanuvchiga yuboriladigan xabarni yozing.\n"
        "⚠️ Bekor qilish uchun tugmani bosing.",
        reply_markup=cancel_keyboard(),
    )


@router.message(BroadcastStates.message)
async def broadcast_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if await cancel_if_requested(message, state):
        return
    if not message.text:
        await message.answer("⚠️ Broadcast hozircha faqat matn ko'rinishida yuboriladi.")
        return
    user_ids = db.all_user_ids()
    success, failed = 0, 0
    await message.answer(f"📣 Yuborish boshlandi. Jami: {len(user_ids)} ta foydalanuvchi.")
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, message.text)
            success += 1
            await asyncio.sleep(0.04)
        except Exception as error:
            failed += 1
            logger.warning("Broadcast xatosi %s: %s", user_id, error)
    await state.clear()
    await message.answer(
        f"✅ Broadcast yakunlandi.\n\n📨 Yetib bordi: {success}\n❌ Xato: {failed}",
        reply_markup=admin_keyboard(),
    )


@router.message(F.text.in_({"👤 Status berish", "🧹 Status olish"}))
async def status_change_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    action = "remove" if message.text == "🧹 Status olish" else "give"
    await state.update_data(action=action)
    await state.set_state(StatusStates.user_id)
    await message.answer("👤 Foydalanuvchi Telegram ID raqamini yuboring:", reply_markup=cancel_keyboard())


@router.message(StatusStates.user_id)
async def status_user_id(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ ID faqat raqamlardan iborat bo'ladi.")
        return
    if not db.get_user(user_id):
        await message.answer("❌ Bu foydalanuvchi hali botga /start bosmagan.")
        return
    await state.update_data(user_id=user_id)
    await state.set_state(StatusStates.status)
    await message.answer(
        "Statusni yozing: <code>vip</code> yoki <code>premium</code>.\n"
        "Olish uchun: <code>user</code>"
    )


@router.message(StatusStates.status)
async def status_set_handler(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    status = (message.text or "").strip().lower()
    if status not in {"user", "vip", "premium"}:
        await message.answer("⚠️ Faqat user, vip yoki premium yozing.")
        return
    data = await state.get_data()
    db.set_status(data["user_id"], status)
    await state.clear()
    await message.answer(
        f"✅ Status o'zgartirildi: {status_label(status)}",
        reply_markup=admin_keyboard(),
    )


@router.message(F.text == "📢 Kanallarni sozlash")
async def channel_setting_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    current = db.get_channels()
    current_label = ", ".join(current) if current else "o'chirilgan"
    await state.set_state(ChannelStates.channels)
    await message.answer(
        "📢 Majburiy kanallarni @username ko'rinishida, bo'sh joy bilan ajratib yuboring.\n"
        "Bir nechta kanal: <code>@kanal1 @kanal2</code>\n"
        "Majburiy obunani o'chirish uchun: <code>off</code>\n\n"
        f"Joriy: {current_label}",
        reply_markup=cancel_keyboard(),
    )


@router.message(ChannelStates.channels)
async def channel_setting_handler(message: Message, state: FSMContext) -> None:
    if await cancel_if_requested(message, state):
        return
    text = (message.text or "").strip()
    channels = [] if text.lower() == "off" else [
        item for item in text.split() if item.startswith("@") or item.startswith("https://t.me/")
    ]
    if text.lower() != "off" and not channels:
        await message.answer("⚠️ Kanalni @username ko'rinishida kiriting yoki off yozing.")
        return
    db.set_setting("required_channels", json.dumps(channels))
    await state.clear()
    await message.answer(
        "✅ Kanallar sozlandi.\n"
        + ("🔓 Majburiy obuna o'chirildi." if not channels else f"📢 {len(channels)} ta kanal qo'shildi."),
        reply_markup=admin_keyboard(),
    )


@router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_main(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🏠 Asosiy menyu:", reply_markup=main_keyboard(message.from_user.id))


# ============================================================
# UMUMIY XATOLAR VA ISHGA TUSHIRISH
# ============================================================
@router.message()
async def fallback_handler(message: Message, bot: Bot) -> None:
    """Noma'lum xabarlar uchun qulay yo'naltiruvchi javob."""
    if not await require_subscription(message, bot):
        return
    if message.text:
        # Foydalanuvchi /start dan keyin darhol kod yuborsa ham kino beriladi.
        movie = db.get_movie(message.text.strip())
        if movie:
            await send_movie(message, movie)
            return
        await message.answer(
            "🤔 Buyruq tushunilmadi.\n"
            "🎬 Kino kodini yuboring yoki menyudagi tugmalardan foydalaning.",
            reply_markup=main_keyboard(message.from_user.id),
        )
    else:
        await message.answer(
            "⚠️ Bu xabar turi qo'llab-quvvatlanmaydi.",
            reply_markup=main_keyboard(message.from_user.id),
        )


async def main() -> None:
    """Botni polling rejimida ishga tushiradi."""
    if not TOKEN or TOKEN == "TOKENNI_SHU_YERGA_YOZING":
        raise RuntimeError(
            "BOT_TOKEN topilmadi. bot.py boshidagi TOKEN qiymatini yoki "
            "BOT_TOKEN environment variable'ini sozlang."
        )
    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        logger.info("KODLI KINO BOT ishga tushmoqda...")
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Bot ishlashida kutilmagan xatolik")
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")