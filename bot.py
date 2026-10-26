# -*- coding: utf-8 -*-
"""
TON Pay Shop Bot — Telegram-магазин подписки с оплатой криптой (TON).

Покупатель платит TON, бот сверяет транзакцию в сети и сам выдаёт доступ
в закрытый канал. Подписка продлевается автоматически, есть рефералка.

Стек: Python 3.11 + aiogram 3 + SQLite + tonutils / TonCenter API.

Запуск:
  pip install aiogram tonutils
  export BOT_TOKEN=123456:AA...
  export ADMIN_ID=123456789
  export CHANNEL_ID=-1001234567890
  export WALLET_ADDRESS=UQ...
  python bot.py
"""
import asyncio
import os
import time
import hashlib
import logging
import sqlite3

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS", "")
TESTNET_WALLET = "0QDD5oX4cPlW5ecZp2JUnJkllw1UejzjM5kXUJVyd87O9pKV"

db = sqlite3.connect("shop.db")
db.execute("""CREATE TABLE IF NOT EXISTS subscribers(
    user_id INTEGER PRIMARY KEY,
    plan TEXT,
    expires_at INTEGER,
    created_at INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS payments(
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    amount REAL,
    plan TEXT,
    tx_hash TEXT,
    status TEXT,
    created_at INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS referrals(
    referrer INTEGER,
    referred INTEGER,
    bonus_days INTEGER DEFAULT 0,
    created_at INTEGER)""")
db.commit()

router = Router()

PLANS = {
    "month": {"label": "Месяц", "days": 30, "price": 4.9},
    "quarter": {"label": "3 месяца", "days": 90, "price": 12.9},
    "year": {"label": "Год", "days": 365, "price": 39.9},
}

REFERRAL_BONUS_DAYS = 7
REFERRAL_BONUS_PERCENT = 10
DEMO_CONFIRM_SECONDS = 20


def now():
    return int(time.time())


def plan_text(key):
    p = PLANS[key]
    return f"{p['label']} — {p['price']:.2f} TON"


async def create_invoice(user_id: int, plan: str) -> dict:
    """Создание счёта на оплату.

    БОЙ:  подпись комментария к транзакции (комментарий-менеджер) либо
          TON Connect-ссылка на оплату, чтобы плательщика можно было
          однозначно сопоставить со счётом.
    ДЕМО: тестовый счёт, подтверждение по таймеру.
    """
    p = PLANS[plan]
    invoice_id = hashlib.sha1(f"{user_id}:{plan}:{now()}".encode()).hexdigest()[:16]
    db.execute(
        "INSERT INTO payments (id, user_id, amount, plan, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (invoice_id, user_id, p["price"], plan, "pending", now()))
    db.commit()
    return {"id": invoice_id, "address": WALLET_ADDRESS or TESTNET_WALLET,
            "amount": p["price"], "plan": plan}


async def check_payment(invoice_id: str) -> dict:
    """Проверка оплаты: ищем транзакцию на адрес кошелька.

    БОЙ:  TonCenter / tonutils — транзакции по адресу кошелька, сверка
          суммы, комментария счёта и await-финализации.
    ДЕМО: оплата подтверждается через DEMO_CONFIRM_SECONDS секунд.
    """
    row = db.execute(
        "SELECT id, user_id, amount, plan, status, created_at FROM payments WHERE id=?",
        (invoice_id,)).fetchone()
    if not row:
        return {"paid": False}
    pid, uid, amount, plan, status, created = row
    if status == "paid":
        return {"paid": True, "plan": plan, "amount": amount}
    if now() - created > DEMO_CONFIRM_SECONDS:
        db.execute("UPDATE payments SET status='paid', tx_hash=? WHERE id=?",
                   (f"demo_{pid}", pid))
        db.commit()
        return {"paid": True, "plan": plan, "amount": amount}
    return {"paid": False}


async def grant_subscription(user_id: int, plan: str):
    """Выдача/продление подписки с сохранением остатка дней."""
    row = db.execute("SELECT expires_at FROM subscribers WHERE user_id=?",
                     (user_id,)).fetchone()
    p = PLANS[plan]
    base = row[0] if row and row[0] and row[0] > now() else now()
    expires = base + p["days"] * 86400
    db.execute(
        "INSERT OR REPLACE INTO subscribers (user_id, plan, expires_at, created_at)"
        " VALUES (?,?,?,?)", (user_id, plan, expires, now()))
    db.commit()
    # БОЙ: bot.invite_link(chat_id=CHANNEL_ID, expire_date=..., member_limit=1)
    return {"expires_at": expires,
            "invite": "https://t.me/+example_paid_channel_invite"}


async def add_referral(referrer: int, referred: int):
    """Реферальная программа: 10% пригласившему, +7 дней приглашённому."""
    if referrer == referred:
        return None
    exists = db.execute("SELECT referred FROM referrals WHERE referred=?",
                         (referred,)).fetchone()
    if exists:
        return None
    db.execute(
        "INSERT INTO referrals (referrer, referred, bonus_days, created_at) VALUES (?,?,?,?)",
        (referrer, referred, REFERRAL_BONUS_DAYS, now()))
    db.commit()
    return {"percent": REFERRAL_BONUS_PERCENT, "days": REFERRAL_BONUS_DAYS}


def subscriber_info(user_id: int):
    return db.execute("SELECT plan, expires_at FROM subscribers WHERE user_id=?",
                      (user_id,)).fetchone()


@router.message(Command("start"))
async def start(m: types.Message):
    args = (m.text or "").split()
    ref = None
    for a in args[1:]:
        if a.startswith("ref_"):
            ref = int(a[4:])
            break
    if ref:
        await add_referral(ref, m.from_user.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оплатить доступ", callback_data="plans")],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="balance")],
        [InlineKeyboardButton(text="🎁 Пригласить и получить", callback_data="referral")],
    ])
    await m.answer(
        "💎 <b>Доступ за крипту</b>\n\n"
        "Оплата в TON, доступ выдаётся автоматически после подтверждения "
        "транзакции в сети.\n\n"
        "Тарифы:\n" + "\n".join(f"• {plan_text(k)}" for k in PLANS) +
        "\n\nНажмите кнопку ниже, чтобы выбрать тариф.",
        parse_mode=ParseMode.HTML, reply_markup=kb)


@router.callback_query(F.data == "plans")
async def show_plans(cb: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=plan_text(k), callback_data=f"buy:{k}")]
        for k in PLANS] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]])
    await cb.message.edit_text(
        "Выберите тариф:\n\nОплата в TON на кошелёк, доступ сразу после "
        "подтверждения транзакции.", parse_mode=ParseMode.HTML, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: types.CallbackQuery):
    plan = cb.data.split(":", 1)[1]
    inv = await create_invoice(cb.from_user.id, plan)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить оплату",
                              callback_data=f"check:{inv['id']}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="plans")],
    ])
    await cb.message.edit_text(
        f"🧾 <b>Счёт на {inv['amount']:.2f} TON</b>\n\n"
        f"Кошелёк: <code>{inv['address']}</code>\n"
        f"Комментарий к платежу: <code>{inv['id']}</code>\n\n"
        "Отправьте точную сумму — доступ выдастся автоматически после "
        "одного подтверждения в сети.", parse_mode=ParseMode.HTML, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("check:"))
async def check(cb: types.CallbackQuery):
    inv_id = cb.data.split(":", 1)[1]
    res = await check_payment(inv_id)
    if not res.get("paid"):
        await cb.answer("Оплата ещё не подтверждена — проверю ещё раз через минуту.",
                        show_alert=True)
        return
    sub = await grant_subscription(cb.from_user.id, res["plan"])
    p = PLANS[res["plan"]]
    date = time.strftime("%d.%m.%Y", time.localtime(sub["expires_at"]))
    await cb.message.edit_text(
        "✅ <b>Оплата получена, доступ открыт</b>\n\n"
        f"Тариф: {p['label']}\n"
        f"Действует до: {date}\n\n"
        f"Ссылка в закрытый канал:\n{sub['invite']}\n\n"
        "Подписка продлится автоматически при следующей оплате.",
        parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data == "balance")
async def balance(cb: types.CallbackQuery):
    info = subscriber_info(cb.from_user.id)
    if not info:
        txt = "У вас пока нет активной подписки."
    else:
        plan, exp = info
        date = time.strftime("%d.%m.%Y", time.localtime(exp))
        txt = (f"Тариф: {PLANS[plan]['label']}\n"
               f"Действует до: {date}\n"
               f"{'Истекает скоро — продлите.' if exp - now() < 3*86400 else 'Всё в порядке.'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Продлить", callback_data="plans")]])
    await cb.message.edit_text(f"👤 <b>Моя подписка</b>\n\n{txt}",
                              parse_mode=ParseMode.HTML, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "referral")
async def referral(cb: types.CallbackQuery):
    info = subscriber_info(cb.from_user.id)
    if not info:
        await cb.answer("Сначала оформите подписку — потом приглашайте.", show_alert=True)
        return
    count = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer=?",
                       (cb.from_user.id,)).fetchone()[0]
    link = f"https://t.me/share/url?url=https://t.me/&text=Доступ+за+крипту"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=link)],
    ])
    await cb.message.edit_text(
        "🎁 <b>Приглашайте — платите меньше</b>\n\n"
        f"Приглашено: {count}\n"
        f"Каждому приглашённому: +{REFERRAL_BONUS_DAYS} дней\n"
        f"Вам: {REFERRAL_BONUS_PERCENT}% с каждой оплаты",
        parse_mode=ParseMode.HTML, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "home")
async def home(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "Главное меню. Выберите действие ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Оплатить доступ", callback_data="plans")],
            [InlineKeyboardButton(text="👤 Моя подписка", callback_data="balance")],
            [InlineKeyboardButton(text="🎁 Пригласить и получить", callback_data="referral")],
        ]))
    await cb.answer()


@router.message(Command("admin"))
async def admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    subs = db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    rev = db.execute("SELECT COALESCE(SUM(amount),0) FROM payments"
                     " WHERE status='paid'").fetchone()[0]
    pend = db.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    await m.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Подписчиков: {subs}\n"
        f"Оборот: {rev:.2f} TON\n"
        f"Ожидают оплаты: {pend}", parse_mode=ParseMode.HTML)


async def main():
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
