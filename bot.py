"""
Telegram command bot: ask the watcher for state, change settings, mute alerts.

Runs as its own process (long polling), independent from the scheduled checks.
Commands:
  /status    — portfolio, protocols, balances right now
  /positions — protocols only
  /chart     — force a portfolio chart
  /check     — run a check immediately
  /history N — portfolio value for the last N days
  /settings  — show thresholds
  /set key value — change a threshold (e.g. /set dust_usd 5)
  /mute 6h   — silence alerts for 6 hours
  /unmute    — resume alerts
  /wallets   — monitored addresses
  /help      — this message
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

import watcher as w

dp = Dispatcher()
CHAT_ID = str(w.CHAT_ID)


def allowed(msg: Message) -> bool:
    return str(msg.chat.id) == CHAT_ID


def fmt_chain(chain, data):
    if not data or data.get("balance") is None:
        return f"{chain}: нет данных"
    return f"{chain}: {data['balance']:.6f} {data['symbol']}"


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not allowed(msg):
        return
    await msg.answer(
        "🏦 <b>Команды</b>\n\n"
        "/status — портфель и балансы\n"
        "/positions — только протоколы\n"
        "/chart — график портфеля\n"
        "/check — проверить сейчас\n"
        "/history N — портфель за N дней\n"
        "/settings — текущие пороги\n"
        "/set ключ значение — изменить порог\n"
        "/mute 6h — тишина 6 часов\n"
        "/unmute — снять тишину\n"
        "/wallets — список кошельков",
        parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not allowed(msg):
        return
    con = w.db_init()
    await msg.answer("⏳ собираю данные…")
    parts = []
    for wal in w.load_wallets():
        addr = wal["address"]
        prices = w.fetch_prices()
        snap = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "manual", "wallet": addr, "label": wal.get("label"),
                "balances": w.snapshot_balances(addr), "prices": prices}
        total, positions, err = w.fetch_positions(addr)
        snap["portfolio_total"] = total
        snap["positions"] = positions
        if err:
            snap["debank_error"] = err
        parts.append(w.render(snap, []))
    con.close()
    # Telegram caps a message at 4096 chars - send in chunks
    text = "\n\n➖➖➖\n\n".join(parts)
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i + 4000], parse_mode="HTML")


@dp.message(Command("positions"))
async def cmd_positions(msg: Message):
    if not allowed(msg):
        return
    await msg.answer("⏳ читаю DeBank…")
    lines = []
    for wal in w.load_wallets():
        total, positions, err = w.fetch_positions(wal["address"])
        lines.append(f"💼 <b>{wal.get('label') or wal['address'][:10]}</b> — "
                     f"${total:,.2f}" if total else "💼 нет данных")
        if err:
            lines.append(f"⚠️ {err[:80]}")
            continue
        for p in sorted(positions, key=lambda x: -x["usd"]):
            if p["usd"] < 0.01:
                continue
            lines.append(f"  <b>{p['protocol']}</b> ({p.get('chain')}) — ${p['usd']:,.2f}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("chart"))
async def cmd_chart(msg: Message):
    if not allowed(msg):
        return
    con = w.db_init()
    sent = False
    for wal in w.load_wallets():
        png = w.render_chart(con, wal["address"], w.ROOT / f"chart_{wal['address'][:8]}.png")
        if png:
            await msg.answer_photo(FSInputFile(str(png)), caption="📈 Динамика портфеля")
            sent = True
    con.close()
    if not sent:
        await msg.answer("📉 Недостаточно данных: нужно ≥2 точек и разброс ≥ "
                         f"${w.cfg('chart_min_spread_usd')}")


@dp.message(Command("check"))
async def cmd_check(msg: Message):
    if not allowed(msg):
        return
    await msg.answer("🔄 запускаю полную проверку…")
    await asyncio.to_thread(w.run, "full", True)
    await msg.answer("✅ проверка выполнена")


@dp.message(Command("history"))
async def cmd_history(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    days = 7
    if command.args and command.args.strip().isdigit():
        days = min(int(command.args.strip()), 90)
    con = w.db_init()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    lines = [f"📊 <b>Портфель за {days} дн.</b>"]
    for wal in w.load_wallets():
        rows = list(con.execute(
            "SELECT ts, portfolio_total FROM runs WHERE wallet=? AND ts > ? "
            "AND portfolio_total IS NOT NULL ORDER BY ts", (wal["address"], since)))
        if not rows:
            lines.append(f"  <code>{wal['address'][:10]}…</code> — нет данных")
            continue
        vals = [r[1] for r in rows]
        first, last = vals[0], vals[-1]
        delta = last - first
        pct = (delta / first * 100) if first else 0
        lines.append(f"  <code>{wal['address'][:10]}…</code>\n"
                     f"     {len(rows)} замеров · ${first:,.2f} → ${last:,.2f} "
                     f"({'📈' if delta >= 0 else '📉'} {delta:+,.2f}, {pct:+.1f}%)")
    con.close()
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not allowed(msg):
        return
    w.load_config()
    rows = "\n".join(f"  <code>{k}</code> = {v}" for k, v in w.CONFIG.items())
    await msg.answer(f"⚙️ <b>Настройки</b>\n{rows}\n\n"
                     f"<i>Изменить: /set dust_usd 5</i>", parse_mode="HTML")


@dp.message(Command("set"))
async def cmd_set(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    args = (command.args or "").split(maxsplit=1)
    if len(args) != 2:
        await msg.answer("Формат: <code>/set dust_usd 5</code>", parse_mode="HTML")
        return
    key, raw = args[0].strip(), args[1].strip()
    w.load_config()
    if key not in w.CONFIG_DEFAULTS:
        await msg.answer(f"❌ Неизвестный ключ. Доступны:\n<code>"
                         + "</code>\n<code>".join(w.CONFIG_DEFAULTS) + "</code>",
                         parse_mode="HTML")
        return
    default = w.CONFIG_DEFAULTS[key]
    try:
        if isinstance(default, bool):
            value = raw.lower() in ("1", "true", "yes", "on", "да")
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, list):
            value = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            value = raw
    except ValueError:
        await msg.answer("❌ Не удалось разобрать значение")
        return
    w.CONFIG[key] = value
    w.save_config()
    await msg.answer(f"✅ <code>{key}</code> = <b>{value}</b>", parse_mode="HTML")


@dp.message(Command("mute"))
async def cmd_mute(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    raw = (command.args or "6h").strip().lower()
    hours = 6
    if raw.endswith("h") and raw[:-1].isdigit():
        hours = int(raw[:-1])
    elif raw.isdigit():
        hours = int(raw)
    w.load_config()
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    w.CONFIG["muted_until"] = until
    w.save_config()
    await msg.answer(f"🔇 Алерты приглушены до {until} UTC")


@dp.message(Command("unmute"))
async def cmd_unmute(msg: Message):
    if not allowed(msg):
        return
    w.load_config()
    w.CONFIG["muted_until"] = None
    w.save_config()
    await msg.answer("🔔 Алерты включены")


@dp.message(Command("wallets"))
async def cmd_wallets(msg: Message):
    if not allowed(msg):
        return
    lines = ["👛 <b>Кошельки</b>"]
    for wal in w.load_wallets():
        label = wal.get("label") or "без названия"
        lines.append(f"  <b>{label}</b>\n  <code>{wal['address']}</code>")
    lines.append(f"\n<i>Добавить: отредактируй wallets.json</i>")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text)
async def fallback(msg: Message):
    if not allowed(msg):
        return
    await msg.answer("Не понял. /help — список команд")


async def main():
    w.load_config()
    token = w.load_token()
    if not token:
        sys.exit("BOT_TOKEN not found")
    bot = Bot(token=token)
    me = await bot.get_me()
    w.log(f"command bot started: @{me.username}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())