import os
import json
import time
import logging
import sqlite3
from uuid import uuid4
from typing import Dict, Any, Optional, Tuple

import requests
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN", "")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")
if not CRYPTO_PAY_API_TOKEN:
    raise SystemExit("CRYPTO_PAY_API_TOKEN is not set")

CRYPTO_PAY_BASE = "https://pay.crypt.bot/api"

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot, storage=MemoryStorage())

DB_PATH = os.getenv("DB_PATH", "shop.db")

ASSETS = ["USDT", "TON"]

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT NOT NULL,
            package TEXT NOT NULL,
            price_usdt REAL NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            game TEXT NOT NULL,
            package TEXT NOT NULL,
            price_usdt REAL NOT NULL,
            asset TEXT NOT NULL,
            nickname TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            invoice_id INTEGER,
            pay_url TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def ensure_sample_catalog():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM catalog")
    if cur.fetchone()[0] == 0:
        sample = [
            ("Genshin Impact", "60 Genesis Crystals", 1.1),
            ("Genshin Impact", "330 Genesis Crystals", 5.5),
            ("World of Warcraft", "Gold 100k (EU)", 7.0),
        ]
        cur.executemany("INSERT INTO catalog(game, package, price_usdt) VALUES(?,?,?)", sample)
        conn.commit()
    conn.close()

def crypto_pay(method: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    url = f"{CRYPTO_PAY_BASE}/{method}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
        r.raise_for_status()
        data = r.json()
        return bool(data.get("ok")), data
    except Exception as e:
        logging.exception("Crypto Pay API error: %s", e)
        return False, {"error": str(e)}

def create_invoice(asset: str, amount: float, description: str, payload: str) -> Optional[Dict[str, Any]]:
    ok, data = crypto_pay(
        "createInvoice",
        {
            "asset": asset,
            "amount": round(amount, 2),
            "description": description,
            "payload": payload,
        },
    )
    if ok:
        return data.get("result")
    return None

def get_invoice(invoice_id: int) -> Optional[Dict[str, Any]]:
    ok, data = crypto_pay("getInvoices", {"invoice_ids": [invoice_id]})
    if ok:
        arr = data.get("result", {}).get("items", [])
        if arr:
            return arr[0]
    return None

class OrderFSM(StatesGroup):
    choosing_game = State()
    choosing_package = State()
    entering_nickname = State()
    choosing_asset = State()

def games_kb() -> InlineKeyboardMarkup:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT game FROM catalog ORDER BY game")
    kb = InlineKeyboardMarkup(row_width=2)
    for (game,) in cur.fetchall():
        kb.insert(InlineKeyboardButton(text=game, callback_data=f"game:{game}"))
    conn.close()
    return kb

def packages_kb(game: str) -> InlineKeyboardMarkup:
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT package, price_usdt FROM catalog WHERE game=? ORDER BY price_usdt", (game,))
    kb = InlineKeyboardMarkup(row_width=1)
    for pkg, price in cur.fetchall():
        kb.add(InlineKeyboardButton(text=f"{pkg} — {price:.2f} USDT", callback_data=f"pkg:{pkg}"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back:games"))
    conn.close()
    return kb

def assets_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    for a in ASSETS:
        kb.insert(InlineKeyboardButton(text=a, callback_data=f"asset:{a}"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="back:packages"))
    return kb

def pay_kb(pay_url: str, order_id: str, invoice_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 Оплатить (Crypto Pay)", url=pay_url))
    kb.add(InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"check:{order_id}:{invoice_id}"))
    return kb

@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message, state: FSMContext):
    await state.finish()
    text = "👋 Привет! Выбери игру для покупки:"
    await m.answer(text, reply_markup=games_kb())

@dp.callback_query_handler(lambda c: c.data.startswith("game:"), state="*")
async def pick_game(c: types.CallbackQuery, state: FSMContext):
    game = c.data.split(":", 1)[1]
    await state.update_data(game=game)
    await OrderFSM.choosing_package.set()
    await c.message.edit_text(f"Игра: <b>{game}</b>\nВыбери набор:", reply_markup=packages_kb(game))

@dp.callback_query_handler(lambda c: c.data.startswith("pkg:"), state=OrderFSM.choosing_package)
async def pick_package(c: types.CallbackQuery, state: FSMContext):
    pkg = c.data.split(":", 1)[1]
    await state.update_data(package=pkg)
    await OrderFSM.entering_nickname.set()
    await c.message.edit_text("Отправь игровой ник/ID для доставки заказа.")

@dp.message_handler(lambda m: m.text and len(m.text) > 1, state=OrderFSM.entering_nickname)
async def got_nickname(m: types.Message, state: FSMContext):
    nick = m.text.strip()
    await state.update_data(nickname=nick)
    await OrderFSM.choosing_asset.set()
    await m.answer("Выбери крипто-актив для оплаты:", reply_markup=assets_kb())

@dp.callback_query_handler(lambda c: c.data.startswith("asset:"), state=OrderFSM.choosing_asset)
async def choose_asset(c: types.CallbackQuery, state: FSMContext):
    asset = c.data.split(":", 1)[1]
    data = await state.get_data()
    game, pkg, nick = data["game"], data["package"], data["nickname"]
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT price_usdt FROM catalog WHERE game=? AND package=?", (game, pkg))
    row = cur.fetchone(); conn.close()
    if not row:
        await c.answer("Ошибка прайса", show_alert=True)
        await state.finish(); return
    price = float(row[0])
    order_id = uuid4().hex[:12]
    desc = f"{game} - {pkg} (nick: {nick})"
    invoice = create_invoice(asset=asset, amount=price, description=desc, payload=order_id)
    if not invoice:
        await c.answer("Не удалось создать счёт", show_alert=True)
        return
    pay_url = invoice.get("pay_url"); invoice_id = int(invoice.get("invoice_id"))
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, c.from_user.id, c.from_user.username, game, pkg, price, asset, nick, int(time.time()), "pending", invoice_id, pay_url))
    conn.commit(); conn.close()
    text = f"<b>Заказ #{order_id}</b>\nИгра: {game}\nНабор: {pkg}\nНик: {nick}\nК оплате: {price} {asset}"
    await c.message.edit_text(text, reply_markup=pay_kb(pay_url, order_id, invoice_id))
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("check:"))
async def check_payment(c: types.CallbackQuery):
    _, order_id, invoice_id = c.data.split(":")
    inv = get_invoice(int(invoice_id))
    if not inv:
        await c.answer("Ошибка проверки", show_alert=True)
        return
    status = inv.get("status")
    if status == "paid":
        conn = db(); cur = conn.cursor()
        cur.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
        conn.commit(); conn.close()
        await c.message.edit_text(f"✅ Оплата получена! Заказ #{order_id} оплачен.")
    elif status == "active":
        await c.answer("Платёж ещё не поступил.", show_alert=True)
    else:
        await c.message.edit_text("Счёт недействителен.")

if __name__ == "__main__":
    init_db()
    ensure_sample_catalog()
    executor.start_polling(dp, skip_updates=True)
