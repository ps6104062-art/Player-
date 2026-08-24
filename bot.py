"""
Playerok Parser Bot (curl-cffi + GraphQL)
==========================================
Установка:
    pip install aiogram>=3.10.0 curl-cffi

Запуск:
    python bot.py
"""

import asyncio
import logging
import re
from collections import Counter
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
BOT_TOKEN = "8657083122:AAFfn-iGiiVKMYsBVkrWHBqZk0hNchHNmrY"

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxZjE5ZjJlNS0wNTY2LTZmYjAtZDhlOS01MWFhODFlNGJkOTkiLCJpZGVudGl0eSI6IjFmMTlmMmU1LTA1ODQtNjQ3MC02Mjk1LWVmOWNiZjc4MTg1ZCIsInJvbGUiOiJVU0VSIiwidiI6MSwicmV2IjoxLCJpYXQiOjE3ODc1MTU4NTksImV4cCI6MTgxOTA1MTg1OX0.wY5PnQq5V_TFhxRKaYqSyA5oUa8t9B8M8FqIEPtbQlU"
DDG9 = "185.126.67.212"

GRAPHQL_URL = "https://playerok.com/graphql"
EXECUTOR = ThreadPoolExecutor(max_workers=4)

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

# ─── GRAPHQL ЗАПРОС ──────────────────────────────────────────────────────────
LOTS_QUERY = """
query catalog($first: Int, $after: String, $gameSlug: String, $categorySlug: String) {
  lots(first: $first, after: $after, filter: {
    gameSlug: $gameSlug,
    categorySlug: $categorySlug
  }) {
    edges {
      node {
        id
        name
        price
        slug
        seller {
          username
          rating
          reviewsCount
        }
        category {
          name
          slug
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

# Альтернативный запрос если первый не работает
ITEMS_QUERY = """
query items($first: Int, $after: String, $filter: ItemFilter) {
  items(first: $first, after: $after, filter: $filter) {
    edges {
      node {
        id
        name
        price
        slug
        user {
          username
          rating
          reviewsCount
        }
        category {
          name
          slug
        }
        game {
          slug
          name
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

def fetch_lots_sync(game_slug: str, category_slug: str = None, after: str = None) -> Optional[dict]:
    """Синхронный запрос через curl-cffi для обхода Cloudflare."""
    try:
        from curl_cffi import requests as cffi_requests

        cookies = {
            "__ddg9_": DDG9,
            "token": TOKEN,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "https://playerok.com",
            "Referer": f"https://playerok.com/{game_slug}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        }

        # Пробуем первый запрос
        variables = {
            "first": 50,
            "gameSlug": game_slug,
        }
        if category_slug:
            variables["categorySlug"] = category_slug
        if after:
            variables["after"] = after

        resp = cffi_requests.post(
            GRAPHQL_URL,
            json={"query": LOTS_QUERY, "variables": variables},
            headers=headers,
            cookies=cookies,
            impersonate="chrome120",
            timeout=20
        )

        if resp.status_code == 200:
            data = resp.json()
            # Пробуем разные ключи ответа
            result = (
                data.get("data", {}).get("lots") or
                data.get("data", {}).get("items") or
                data.get("data", {}).get("offers")
            )
            if result:
                return result

        # Если первый не сработал — пробуем второй запрос
        variables2 = {
            "first": 50,
            "filter": {"gameSlug": game_slug}
        }
        if after:
            variables2["after"] = after

        resp2 = cffi_requests.post(
            GRAPHQL_URL,
            json={"query": ITEMS_QUERY, "variables": variables2},
            headers=headers,
            cookies=cookies,
            impersonate="chrome120",
            timeout=20
        )

        if resp2.status_code == 200:
            data2 = resp2.json()
            log.info(f"Ответ API: {str(data2)[:500]}")
            return (
                data2.get("data", {}).get("items") or
                data2.get("data", {}).get("lots") or
                data2.get("data", {}).get("offers")
            )

        log.error(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return None

    except Exception as e:
        log.error(f"fetch_lots_sync ошибка: {e}")
        return None


def parse_nodes(result: dict) -> list[dict]:
    lots = []
    edges = result.get("edges", [])
    for edge in edges:
        node = edge.get("node", {})
        if not node:
            continue
        seller = node.get("seller") or node.get("user") or {}
        category = node.get("category") or {}
        lots.append({
            "id": str(node.get("id", "")),
            "name": node.get("name") or node.get("title") or "",
            "price": int(node.get("price") or 0),
            "url": f"https://playerok.com/products/{node.get('slug', '')}",
            "seller_username": seller.get("username", ""),
            "seller_rating": float(seller.get("rating") or 0),
            "seller_reviews": int(seller.get("reviewsCount") or 0),
            "category": category.get("name", ""),
        })
    return lots


async def get_all_lots(game_slug: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    all_lots = []
    after = None

    for page in range(5):
        result = await loop.run_in_executor(
            EXECUTOR, fetch_lots_sync, game_slug, None, after
        )
        if not result:
            break

        nodes = parse_nodes(result)
        all_lots.extend(nodes)
        log.info(f"Страница {page+1}: получено {len(nodes)} лотов")

        page_info = result.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        await asyncio.sleep(0.5)

    return all_lots


def analyze_competition(lots: list[dict]) -> list[dict]:
    def normalize(name: str) -> str:
        n = re.sub(r"\d+\s*(руб|₽|rub|\$|usd|coins?|к|k)", "", name, flags=re.IGNORECASE)
        n = re.sub(r"\d+", "", n)
        n = re.sub(r"\s+", " ", n)
        return n.lower().strip()

    normalized = [normalize(l["name"]) for l in lots]
    counts = Counter(normalized)
    result, seen = [], set()

    for lot, norm in zip(lots, normalized):
        if norm in seen:
            continue
        seen.add(norm)
        lot_copy = dict(lot)
        lot_copy["competitors"] = counts[norm] - 1
        result.append(lot_copy)

    return result


def parse_game_slug(text: str) -> str:
    text = text.strip().rstrip("/")
    if "playerok.com/" in text:
        parts = text.split("playerok.com/")[-1].split("/")
        return parts[0]
    return text


async def parse_and_notify(bot: Bot, chat_id: int, cfg: dict):
    game_slug = cfg["game_slug"]
    cooldown = cfg["cooldown"]
    max_competitors = cfg["max_competitors"]
    known_ids: set[str] = set()

    log.info(f"[{chat_id}] Старт: {game_slug}")

    while True:
        try:
            lots = await get_all_lots(game_slug)
            log.info(f"[{chat_id}] Итого лотов: {len(lots)}")

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
                if l["competitors"] <= max_competitors
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
                        f"👤 {lot['seller_username']} ⭐{lot['seller_rating']}\n"
                        f"👥 Конкурентов: {lot['competitors']}\n"
                        f"🔗 <a href=\"{lot['url']}\">Открыть лот</a>"
                    )
                    await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
                    await asyncio.sleep(0.5)
            else:
                log.info(f"[{chat_id}] Новых подходящих нет")

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
        "🎮 <b>Шаг 1/3</b>\n\nОтправь ссылку или slug игры:\n\n"
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
        f"⏱ <b>Шаг 2/3</b>\nКаждые сколько минут?\nПример: <code>5</code>",
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
    await msg.answer("👥 <b>Шаг 3/3</b>\nМакс. конкурентов?\nПример: <code>5</code>", parse_mode="HTML")


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
        f"✅ <b>Готово!</b>\n\n🎮 {all_data['game_slug']}\n"
        f"⏱ {all_data['cooldown']} мин.\n👥 Макс. конкурентов: {max_c}\n\n/start_parse",
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
    
