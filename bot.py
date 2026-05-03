import os
import re
import csv
import io
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)

TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "reports.db")

# ─── База данных ──────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            date      TEXT,
            addresses TEXT,
            work_done TEXT,
            time_from TEXT,
            time_to   TEXT,
            collected TEXT,
            my_share  TEXT,
            raw_text  TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

def save_report(user_id, data: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO reports (user_id, date, addresses, work_done, time_from, time_to, collected, my_share, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        data.get("date", ""),
        data.get("addresses", ""),
        data.get("work_done", ""),
        data.get("time_from", ""),
        data.get("time_to", ""),
        data.get("collected", ""),
        data.get("my_share", ""),
        data.get("raw_text", ""),
    ))
    conn.commit()
    conn.close()

def get_reports(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, date, addresses, work_done, time_from, time_to, collected, my_share FROM reports WHERE user_id=? ORDER BY date DESC, id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows

def delete_last(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM reports WHERE id=?", (row[0],))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ─── Парсер текста ────────────────────────────────────────────────────────────

MONTHS = {
    "янв": "01", "фев": "02", "мар": "03", "апр": "04",
    "май": "05", "мая": "05", "июн": "06", "июл": "07",
    "авг": "08", "сен": "09", "окт": "10", "ноя": "11", "дек": "12",
}

def parse_date(text):
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if m:
        y = m.group(3) or str(datetime.now().year)
        if len(y) == 2: y = "20" + y
        return f"{y}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.search(r"(\d{1,2})\s+(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)", text, re.I)
    if m:
        day = m.group(1).zfill(2)
        key = next((k for k in MONTHS if m.group(2).lower().startswith(k)), None)
        month = MONTHS.get(key, "01")
        return f"{datetime.now().year}-{month}-{day}"
    if re.search(r"вчера", text, re.I):
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")

def parse_time(text):
    m = re.search(r"[сc]\s*(\d{1,2})(?::(\d{2}))?\s*(?:до|по)\s*(\d{1,2})(?::(\d{2}))?", text, re.I)
    if m:
        return f"{m.group(1).zfill(2)}:{m.group(2) or '00'}", f"{m.group(3).zfill(2)}:{m.group(4) or '00'}"
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", text)
    if m:
        return f"{m.group(1).zfill(2)}:{m.group(2)}", f"{m.group(3).zfill(2)}:{m.group(4)}"
    return "", ""

def parse_money(text):
    collected, my_share = "", ""
    m = re.search(r"(?:взял|забрал|получил|взято)\s+([\d\s]+)(?:\s*(?:руб|р\b|₽|тыс))?", text, re.I)
    if m:
        n = int(m.group(1).replace(" ", ""))
        collected = str(n * 1000 if re.search(r"тыс", text[m.start():m.start()+40], re.I) else n)
    m = re.search(r"(?:из них\s+(?:мои?|мне)\s+|мои?\s+|моя\s+доля\s+)([\d\s]+)", text, re.I) \
        or re.search(r"из них\s+([\d\s]+)\s+мои?", text, re.I)
    if m:
        try: my_share = str(int(m.group(1).replace(" ", "")))
        except: pass
    return collected, my_share

def parse_address(text):
    found = []
    for pat in [
        r"(?:на|по адресу|адрес)\s+([А-ЯЁа-яё\w\s.,\-]+?\d+(?:\s*[а-яА-Я])?)",
        r"(?:ул\.|улица|пр\.|проспект|пер\.)\s*([А-ЯЁа-яё\w\s]+?\d+)",
    ]:
        for m in re.finditer(pat, text, re.I):
            a = m.group(1).strip().rstrip(",")
            if len(a) > 2 and a not in found:
                found.append(a)
    return ", ".join(found)

def parse_work(text):
    cleaned = text
    for pat in [
        r"\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?",
        r"\d{1,2}\s+(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)\w*",
        r"(?:сегодня|вчера)",
        r"[сc]\s*\d{1,2}(?::\d{2})?\s*(?:до|по)\s*\d{1,2}(?::\d{2})?",
        r"\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}",
        r"(?:взял|забрал|получил|взято)\s+[\d\s]+(?:\s*(?:руб|р\b|₽|тыс))?",
        r"(?:из них\s+(?:мои?|мне)\s+|мои?\s+|моя\s+доля\s+)[\d\s]+",
        r"из них\s+[\d\s]+\s+мои?",
        r"(?:на|по адресу)\s+[А-ЯЁа-яё\w\s.,\-]+?\d+(?:\s*[а-яА-Я])?",
        r"работал\w*",
    ]:
        cleaned = re.sub(pat, " ", cleaned, flags=re.I)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")

def parse_text(text):
    tf, tt = parse_time(text)
    col, share = parse_money(text)
    return {
        "date":      parse_date(text),
        "addresses": parse_address(text),
        "work_done": parse_work(text),
        "time_from": tf,
        "time_to":   tt,
        "collected": col,
        "my_share":  share,
        "raw_text":  text,
    }

# ─── Форматирование записи ────────────────────────────────────────────────────

def fmt_report(row):
    rid, date, addr, work, tf, tt, col, share = row
    lines = [f"📅 *{date}*"]
    if addr:  lines.append(f"📍 {addr}")
    if work:  lines.append(f"🔧 {work}")
    if tf or tt: lines.append(f"🕐 {tf or '?'} — {tt or '?'}")
    if col:   lines.append(f"💰 Взято: *{col} ₽*")
    if share: lines.append(f"🟢 Моя доля: *{share} ₽*")
    lines.append(f"_(ID: {rid})_")
    return "\n".join(lines)

def make_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Дата","Адреса","Что сделано","Начало","Конец","Взято (₽)","Моя доля (₽)"])
    for row in rows:
        writer.writerow(row)
    output.seek(0)
    return output.getvalue().encode("utf-8-sig")  # utf-8 with BOM for Excel

# ─── Хэндлеры бота ───────────────────────────────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Мои записи"), KeyboardButton("📥 Скачать Excel")]],
    resize_keyboard=True
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Привет! Я ваш рабочий журнал.*\n\n"
        "Просто напишите или надиктуйте что вы сделали, например:\n\n"
        "➡️ _27 апреля работал на Звёздном 44, подключил абонента, с 11 до 18, взял 10000, из них мои 3000_\n\n"
        "Я разберу текст и сохраню запись автоматически.\n\n"
        "*Команды:*\n"
        "/list — последние 10 записей\n"
        "/excel — скачать все записи\n"
        "/delete — удалить последнюю запись\n"
        "/help — помощь",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = get_reports(uid)[:10]
    if not rows:
        await update.message.reply_text("📭 Записей пока нет. Просто напишите что вы сделали!")
        return
    total_col   = sum(int(r[6]) for r in rows if r[6] and r[6].isdigit())
    total_share = sum(int(r[7]) for r in rows if r[7] and r[7].isdigit())
    text = f"📋 *Последние {len(rows)} записей:*\n\n"
    text += "\n\n".join(fmt_report(r) for r in rows)
    text += f"\n\n💰 Итого взято: *{total_col:,} ₽*\n🟢 Итого моё: *{total_share:,} ₽*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_excel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = get_reports(uid)
    if not rows:
        await update.message.reply_text("📭 Записей нет — нечего выгружать.")
        return
    csv_bytes = make_csv(rows)
    fname = f"отчёт_{datetime.now().strftime('%Y-%m-%d')}.csv"
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=fname,
        caption=f"📥 Выгружено {len(rows)} записей. Откройте в Excel или Google Таблицах."
    )

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if delete_last(uid):
        await update.message.reply_text("✅ Последняя запись удалена.")
    else:
        await update.message.reply_text("❌ Записей нет.")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Как пользоваться:*\n\n"
        "Просто напишите или голосом надиктуйте что сделали.\n"
        "Бот понимает:\n"
        "• Дату: _27 апреля_, _вчера_, _27.04_\n"
        "• Время: _с 9 до 18_, _с 10:00 до 17:30_\n"
        "• Адрес: _на ул. Ленина 5_, _на Звёздном 44_\n"
        "• Деньги: _взял 5000_, _из них мои 1000_\n\n"
        "*Команды:*\n"
        "/list — последние записи\n"
        "/excel — скачать всё в Excel\n"
        "/delete — удалить последнюю запись\n",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    uid  = update.effective_user.id

    if text == "📋 Мои записи":
        await cmd_list(update, ctx); return
    if text == "📥 Скачать Excel":
        await cmd_excel(update, ctx); return

    data = parse_text(text)

    # Если похоже на рабочую запись
    if data["work_done"] or data["addresses"] or data["collected"]:
        save_report(uid, data)
        lines = ["✅ *Запись сохранена!*\n"]
        lines.append(f"📅 Дата: {data['date']}")
        if data["addresses"]:  lines.append(f"📍 Адрес: {data['addresses']}")
        if data["work_done"]:  lines.append(f"🔧 Работа: {data['work_done']}")
        if data["time_from"] or data["time_to"]:
            lines.append(f"🕐 Время: {data['time_from'] or '?'} — {data['time_to'] or '?'}")
        if data["collected"]:  lines.append(f"💰 Взято: {data['collected']} ₽")
        if data["my_share"]:   lines.append(f"🟢 Моя доля: {data['my_share']} ₽")
        lines.append("\n_Если что-то не так — напишите /delete и добавьте заново._")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "🤔 Не понял запись. Попробуйте написать подробнее, например:\n"
            "_27 апреля работал на Звёздном 44, подключил абонента, с 11 до 18, взял 10000, из них мои 3000_",
            parse_mode="Markdown"
        )

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎤 Голосовое сообщение получено!\n\n"
        "К сожалению, расшифровка голоса требует платный API. "
        "Используйте встроенную диктовку Telegram — нажмите на микрофон в поле ввода текста, "
        "надиктуйте, и Telegram сам переведёт в текст. Затем отправьте как обычное сообщение.",
        parse_mode="Markdown"
    )

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list",  cmd_list))
    app.add_handler(CommandHandler("excel", cmd_excel))
    app.add_handler(CommandHandler("delete",cmd_delete))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
