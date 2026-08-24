"""
Playerok Parser Bot (playerok-requests-api)
============================================
Установка:
    pip install aiogram>=3.10.0 playerok-requests-api

Запуск:
    python bot.py
"""

import asyncio
import logging
import re
import json
from collections import Counter
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
BOT_TOKEN = "8657083122:AAFfn-iGiiVKMYsBVkrWHBqZk0hNchHNmrY"

COOKIES = {
    "__ddg9_": "185.126.67.212",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxZjE5ZjJlNS0wNTY2LTZmYjAtZDhlOS01MWFhODFlNGJkOTkiLCJpZGVudGl0eSI6IjFmMTlmMmU1LTA1ODQtNjQ3MC02Mjk1LWVmOWNiZjc4MTg1ZCIsInJvbGUiOiJVU0VSIiwidiI6MSwicmV2IjoxLCJpYXQiOjE3ODc1MTU4NTksImV4cCI6MTgxOTA1MTg1OX0.wY5PnQq5V_TFhxRKaYqSyA5oUa8t9B8M8FqIEPtbQlU"
}

# ─── ЛОГИРОВАНИЕ ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── FSM ─────────────────────────────────────────────────────────────────────
class SetupStates(StatesGroup):
    waiting_game = State()
    waiting_cooldown = State()
    waiting_max_competitors = State()

tasks: dict[int, asyncio.Task] = {}
configs: dict[int, dict] = {}

# ─── ПАРСЕР ──────────────────────────────────────────────────────────────────
def get_lots_sync(game_slug: str) -> list[dict]:
    """Получает лоты через playerok-requests-api (синхронно)."""
    try:
        from playerok_requests_api.items import PlayerokItemsApi

        # Сохраняем куки во временный файл
        cookies_path = "/tmp/playerok_cookies.json"
        with open(cookies_path, "w") as f:
            json.dump(COOKIES, f)

        api = PlayerokItemsApi(cookies_file=cookies_path, logger=False)
        raw_lots = api.all_exhibited_lots()

        if not raw_lots:
            return []

        result = []
        for lot in raw_lots:
            node = lot.get("node", {})
            if not node:
                continue

            # Фильтруем по игре если указана
            game = node.get("game", {}) or {}
            category = node.get("category", {}) or {}
            game_name = (game.get("slug") or game.get("name") or "").lower()
            category_name = (category.get("name") or "").lower()

            if game_slug and game_slug.lower() not in game_name and game_slug.lower() not in category_name:
                continue

            seller = node.get("user", {}) or node.get("seller", {}) or {}

            result.append({
                "id": node.get("id", ""),
                "name": node.get("name") or node.get("title") or "",
                "price": int(node.get("price") or 0),
                "url": f"https://playerok.com/products/{node.get('slug', '')}",
                "seller_username": seller.get("username") or seller.get("login") or "",
                "seller_rating": float(seller.get("rating") or 0),
                "seller_reviews": int(seller.get("reviewsCount") or seller.get("reviews_count") or 0),
                "category": category_name,
            })

        return result
    except Exception as e:
        log.error(f"Ошибка get_lots_sync: {e}")
        return []


async def get_lots(game_slug: str) -> list[dict]:
    """Асинхронная обёртка для синхронного API."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_lots_sync, game_slug)


def analyze_competition(lots: list[dict]) -> list[dict]:
    def normalize(name: str) -> str:
        n = re.sub(r"\d+\s*(руб|₽|rub|\$|usd|coins?|к|k)", "", name, flags=re.IGNORECASE)
        n = re.sub(r"\d+", "", n)
        n = re.sub(r"\s+", " ", n)
        return n.lower().strip()

    normalized = [normalize(lot["name"]) for lot in lots]
    counts = Counter(normalized)

    result = []
    seen = set()
    for lot, norm in zip(lots, normalized):
        if norm in seen:
            continue
        seen.add(norm)
        lot_copy = dict(lot)
        lot_copy["competitors"] = counts[norm] - 1
        result.append(lot_copy)

    return result


def parse_game_slug(url_or_slug: str) -> str:
    url_or_slug = url_or_slug.strip().rstrip("/")
    if "playerok.com/" in url_or_slug:
        parts = url_or_slug.split("playerok.com/")[-1].split("/")
        return parts[0]
    return url_or_slug


async def parse_and_notify(bot: Bot, chat_id: int, cfg: dict):
    game_slug = cfg["game_slug"]
    cooldown = cfg["cooldown"]
    max_competitors = cfg["max_competitors"]
    known_ids: set[str] = set()

    log.info(f"[{chat_id}] Старт: {game_slug}")

    while True:
        try:
            lots = await get_lots(game_slug)
            log.info(f"[{chat_id}] Получено лотов: {len(lots)}")

            if not lots:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Лоты не найдены для <code>{game_slug}</code>.\n"
                    f"Следующая проверка через {cooldown} мин.",
                    parse_mode="HTML"
                )
                await asyncio.sleep(cooldown * 60)
                continue

            analyzed = analyze_competition(lots)
            good_lots = [
                l for l in analyzed
                if l.get("competitors", 999) <= max_competitors
                and l["id"] not in known_ids
                and l["name"]
            ]

            if good_lots:
                for lot in good_lots[:10]:
                    known_ids.add(lot["id"])
                    text = (
                        f"🎯 <b>Интересный лот!</b>\n\n"
                        f"📦 <b>{lot['name']}</b>\n"
                        f"💰 Цена: {lot['price']:,} ₽\n"
                        f"👤 Продавец: {lot['seller_username']}\n"
                        f"⭐ Рейтинг: {lot['seller_rating']}\n"
                        f"👥 Конкурентов: {lot['competitors']}\n"
                        f"🔗 <a href=\"{lot['url']}\">Открыть лот</a>"
                    )
                    await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
                    await asyncio.sleep(0.5)
            else:
                log.info(f"[{chat_id}] Новых подходящих лотов нет")

            await asyncio.sleep(cooldown * 60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[{chat_id}] Ошибка: {e}")
            await asyncio.sleep(30)


# ─── HANDLERS ────────────────────────────────────────────────────────────────
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>Playerok Parser Bot</b>\n\n"
        "🔍 /setup — настроить\n"
        "▶️ /start_parse — запустить\n"
        "⏹ /stop — остановить\n"
        "📊 /status — статус",
        parse_mode="HTML"
    )


@dp.message(Command("setup"))
async def cmd_setup(msg: Message, state: FSMContext):
    await state.set_state(SetupStates.waiting_game)
    await msg.answer(
        "🎮 <b>Шаг 1/3</b>\n\n"
        "Отправь ссылку или slug игры:\n\n"
        "<code>https://playerok.com/rust/other</code>\n"
        "<code>rust</code>",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_game)
async def process_game(msg: Message, state: FSMContext):
    slug = parse_game_slug(msg.text.strip())
    await state.update_data(game_slug=slug)
    await state.set_state(SetupStates.waiting_cooldown)
    await msg.answer(
        f"✅ Игра: <code>{slug}</code>\n\n"
        f"⏱ <b>Шаг 2/3</b>\n\nКаждые сколько минут?\nПример: <code>5</code>",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_cooldown)
async def process_cooldown(msg: Message, state: FSMContext):
    try:
        cd = int(msg.text.strip())
        if cd < 1 or cd > 1440:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Число от 1 до 1440")
        return
    await state.update_data(cooldown=cd)
    await state.set_state(SetupStates.waiting_max_competitors)
    await msg.answer(
        "👥 <b>Шаг 3/3</b>\n\nМакс. конкурентов?\nПример: <code>5</code>",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_max_competitors)
async def process_max_competitors(msg: Message, state: FSMContext):
    try:
        max_c = int(msg.text.strip())
        if max_c < 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Число больше 0")
        return
    data = await state.get_data()
    all_data = {**data, "max_competitors": max_c}
    configs[msg.chat.id] = all_data
    await state.clear()
    await msg.answer(
        f"✅ <b>Готово!</b>\n\n"
        f"🎮 {all_data['game_slug']}\n"
        f"⏱ {all_data['cooldown']} мин.\n"
        f"👥 Макс. конкурентов: {max_c}\n\n"
        f"/start_parse",
        parse_mode="HTML"
    )


@dp.message(Command("start_parse"))
async def cmd_start_parse(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Сначала /setup")
        return
    if chat_id in tasks and not tasks[chat_id].done():
        await msg.answer("▶️ Уже запущен!")
        return
    task = asyncio.create_task(parse_and_notify(msg.bot, chat_id, configs[chat_id]))
    tasks[chat_id] = task
    await msg.answer(f"🚀 <b>Запущен!</b> Каждые {configs[chat_id]['cooldown']} мин. 👀", parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    chat_id = msg.chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        await msg.answer("⏹ Остановлен.")
    else:
        await msg.answer("ℹ️ Не запущен.")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Нет настроек. /setup")
        return
    cfg = configs[chat_id]
    status = "▶️ Работает" if (chat_id in tasks and not tasks[chat_id].done()) else "⏹ Остановлен"
    await msg.answer(
        f"📊 {status}\n🎮 {cfg['game_slug']}\n⏱ {cfg['cooldown']} мин.\n👥 {cfg['max_competitors']}",
        parse_mode="HTML"
    )


async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Бот запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
