import os
import re
import csv
import io
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

TOKEN          = os.environ.get("BOT_TOKEN", "")
DB_PATH        = os.environ.get("DB_PATH", "reports.db")
TZ_NAME        = os.environ.get("TIMEZONE", "Europe/Moscow")
REMINDER_HOUR  = int(os.environ.get("REMINDER_HOUR", "20"))
REMINDER_MIN   = int(os.environ.get("REMINDER_MINUTE", "0"))

# ── Шаги диалога ─────────────────────────────────────────────────────────────
DATE, ADDRESS, WORK, COLLECTED, SHARE, CONFIRM = range(6)
# Дополнение записи
EDIT_NUM, EDIT_ADDR, EDIT_WORK, EDIT_COLLECTED, EDIT_SHARE, EDIT_CONFIRM = range(6, 12)

# ── БД ───────────────────────────────────────────────────────────────────────

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    c = get_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            date      TEXT NOT NULL,
            addresses TEXT,
            work_done TEXT,
            time_from TEXT,
            time_to   TEXT,
            collected TEXT,
            my_share  TEXT,
            UNIQUE(user_id, date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 1
        )
    """)
    c.commit()
    c.close()

def upsert_report(user_id, data: dict):
    c = get_conn()
    c.execute("""
        INSERT INTO reports (user_id, date, addresses, work_done, time_from, time_to, collected, my_share)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET
            addresses = excluded.addresses,
            work_done = excluded.work_done,
            time_from = excluded.time_from,
            time_to   = excluded.time_to,
            collected = excluded.collected,
            my_share  = excluded.my_share
    """, (
        user_id, data["date"],
        data.get("addresses",""), data.get("work_done",""),
        data.get("time_from",""), data.get("time_to",""),
        data.get("collected",""), data.get("my_share",""),
    ))
    c.commit()
    c.close()

def append_to_report(user_id, date, extra: dict):
    """Дополняет существующую запись — добавляет новые значения через запятую."""
    row = get_report_by_date(user_id, date)
    if not row:
        return False

    _, old_addr, old_work, old_tf, old_tt, old_col, old_share = row

    def merge(old, new, is_money=False):
        """Склеивает старое и новое через запятую, или суммирует деньги."""
        old = (old or "").strip()
        new = (new or "").strip()
        if not new or new in ("Пропустить", "Не брал", "Не менялось"):
            return old
        if is_money:
            # Суммируем суммы
            try:
                return str(int(old or 0) + int(re.sub(r"[^\d]", "", new)))
            except:
                return old
        if not old:
            return new
        return old + ", " + new

    def merge_time(old_tf, old_tt, new_work):
        """Если в новом описании есть время — обновляем, иначе оставляем."""
        tf, tt = parse_time(new_work)
        if tf or tt:
            return tf, tt
        return old_tf, old_tt

    new_addr  = merge(old_addr, extra.get("addresses",""))
    new_work  = merge(old_work, extra.get("work_done",""))
    new_col   = merge(old_col,  extra.get("collected",""), is_money=True)
    new_share = merge(old_share,extra.get("my_share",""),  is_money=True)
    new_tf, new_tt = merge_time(old_tf, old_tt, extra.get("work_done",""))

    c = get_conn()
    c.execute("""
        UPDATE reports SET addresses=?, work_done=?, time_from=?, time_to=?, collected=?, my_share=?
        WHERE user_id=? AND date=?
    """, (new_addr, new_work, new_tf, new_tt, new_col, new_share, user_id, date))
    c.commit()
    c.close()
    return True

def get_reports(user_id):
    c = get_conn()
    rows = c.execute(
        "SELECT date, addresses, work_done, time_from, time_to, collected, my_share "
        "FROM reports WHERE user_id=? ORDER BY date DESC, rowid DESC",
        (user_id,)
    ).fetchall()
    c.close()
    return [(i+1,) + r for i, r in enumerate(rows)]

def get_report_by_num(user_id, num):
    rows = get_reports(user_id)
    if not rows or num < 1 or num > len(rows):
        return None
    return rows[num-1]  # (num, date, addr, work, tf, tt, col, share)

def get_report_by_date(user_id, date):
    c = get_conn()
    row = c.execute(
        "SELECT date, addresses, work_done, time_from, time_to, collected, my_share "
        "FROM reports WHERE user_id=? AND date=?", (user_id, date)
    ).fetchone()
    c.close()
    return row

def delete_by_num(user_id, num):
    rows = get_reports(user_id)
    if not rows or num < 1 or num > len(rows):
        return False
    target_date = rows[num-1][1]
    c = get_conn()
    c.execute("DELETE FROM reports WHERE user_id=? AND date=?", (user_id, target_date))
    c.commit()
    c.close()
    return True

def set_reminder(user_id, enabled: bool):
    c = get_conn()
    c.execute("INSERT OR REPLACE INTO reminders (user_id, enabled) VALUES (?, ?)",
              (user_id, 1 if enabled else 0))
    c.commit()
    c.close()

def get_reminder_users():
    c = get_conn()
    rows = c.execute("SELECT user_id FROM reminders WHERE enabled=1").fetchall()
    c.close()
    return [r[0] for r in rows]

# ── Форматирование ────────────────────────────────────────────────────────────

def fmt_row(row):
    num, date, addr, work, tf, tt, col, share = row
    lines = [f"*#{num}  {date}*"]
    if addr:     lines.append(f"📍 {addr}")
    if work:     lines.append(f"🔧 {work}")
    if tf or tt: lines.append(f"🕐 {tf or '?'} — {tt or '?'}")
    if col:      lines.append(f"💰 Взято: *{col} ₽*")
    if share:    lines.append(f"🟢 Моя доля: *{share} ₽*")
    return "\n".join(lines)

def make_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["№","Дата","Адреса","Что сделано","Начало","Конец","Взято (₽)","Моя доля (₽)"])
    for row in rows:
        num, date, addr, work, tf, tt, col, share = row
        w.writerow([num, date, addr, work, tf, tt, col, share])
    out.seek(0)
    return out.getvalue().encode("utf-8-sig")

# ── Утилиты ───────────────────────────────────────────────────────────────────

def today_str():
    return datetime.now(ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d")

MONTHS = {
    "янв":"01","фев":"02","мар":"03","апр":"04","май":"05","мая":"05",
    "июн":"06","июл":"07","авг":"08","сен":"09","окт":"10","ноя":"11","дек":"12"
}

def parse_date(text):
    text = text.strip()
    if re.search(r"сегодня", text, re.I): return today_str()
    if re.search(r"вчера",   text, re.I):
        return (datetime.now(ZoneInfo(TZ_NAME)) - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if m:
        y = m.group(3) or str(datetime.now().year)
        if len(y) == 2: y = "20" + y
        return f"{y}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.search(r"(\d{1,2})\s+(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)", text, re.I)
    if m:
        key = next((k for k in MONTHS if m.group(2).lower().startswith(k)), None)
        return f"{datetime.now().year}-{MONTHS.get(key,'01')}-{m.group(1).zfill(2)}"
    return today_str()

def parse_time(text):
    m = re.search(r"[сc]\s*(\d{1,2})(?::(\d{2}))?\s*(?:до|по)\s*(\d{1,2})(?::(\d{2}))?", text, re.I)
    if m:
        return f"{m.group(1).zfill(2)}:{m.group(2) or '00'}", f"{m.group(3).zfill(2)}:{m.group(4) or '00'}"
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", text)
    if m:
        return f"{m.group(1).zfill(2)}:{m.group(2)}", f"{m.group(3).zfill(2)}:{m.group(4)}"
    return "", ""

# ── Клавиатуры ───────────────────────────────────────────────────────────────

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("➕ Новая запись"),     KeyboardButton("✏️ Дополнить запись")],
     [KeyboardButton("📋 Мои записи"),       KeyboardButton("📥 Скачать Excel")],
     [KeyboardButton("⚙️ Напоминание")]],
    resize_keyboard=True
)

def skip_kb(label):
    return ReplyKeyboardMarkup([[KeyboardButton(label)]], resize_keyboard=True)

# ── Диалог: новая запись ─────────────────────────────────────────────────────

async def new_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "📅 *Шаг 1 из 5 — Дата*\n\nВведите дату:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Сегодня"), KeyboardButton("Вчера")]],
            resize_keyboard=True
        )
    )
    return DATE

async def step_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    date = parse_date(update.message.text)
    ctx.user_data["date"] = date
    existing = get_report_by_date(update.effective_user.id, date)
    if existing:
        ctx.user_data["overwrite"] = True
        _, addr, work, _, _, col, share = existing
        await update.message.reply_text(
            f"⚠️ На *{date}* уже есть запись:\n"
            f"📍 {addr or '—'}  🔧 {work or '—'}\n"
            f"💰 {col or '—'} ₽  🟢 {share or '—'} ₽\n\n"
            f"Продолжите — она будет *перезаписана*.",
            parse_mode="Markdown"
        )
    else:
        ctx.user_data["overwrite"] = False
    await update.message.reply_text(
        "📍 *Шаг 2 из 5 — Адрес*\n\nВведите адрес:",
        parse_mode="Markdown",
        reply_markup=skip_kb("Пропустить")
    )
    return ADDRESS

async def step_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["addresses"] = "" if text == "Пропустить" else text
    await update.message.reply_text(
        "🔧 *Шаг 3 из 5 — Что сделано*\n\nОпишите работу:\n_Можно указать время: «с 9 до 18»_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return WORK

async def step_work(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tf, tt = parse_time(text)
    ctx.user_data["work_done"] = text
    ctx.user_data["time_from"] = tf
    ctx.user_data["time_to"]   = tt
    await update.message.reply_text(
        "💰 *Шаг 4 из 5 — Взято с клиента*\n\nВведите сумму:",
        parse_mode="Markdown",
        reply_markup=skip_kb("Не брал")
    )
    return COLLECTED

async def step_collected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["collected"] = "" if text == "Не брал" else re.sub(r"[^\d]","",text)
    await update.message.reply_text(
        "🟢 *Шаг 5 из 5 — Моя доля*\n\nСколько из этого ваших?",
        parse_mode="Markdown",
        reply_markup=skip_kb("Пропустить")
    )
    return SHARE

async def step_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["my_share"] = "" if text == "Пропустить" else re.sub(r"[^\d]","",text)
    d = ctx.user_data
    lines = [
        "📋 *Проверьте запись:*\n",
        f"📅 Дата: *{d.get('date','')}*",
        f"📍 Адрес: {d.get('addresses','') or '—'}",
        f"🔧 Работа: {d.get('work_done','') or '—'}",
    ]
    tf, tt = d.get("time_from",""), d.get("time_to","")
    if tf or tt: lines.append(f"🕐 Время: {tf or '?'} — {tt or '?'}")
    lines += [f"💰 Взято: {d.get('collected','') or '—'} ₽",
              f"🟢 Моя доля: {d.get('my_share','') or '—'} ₽"]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("✅ Сохранить"), KeyboardButton("❌ Отмена")]],
            resize_keyboard=True
        )
    )
    return CONFIRM

async def step_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == "✅ Сохранить":
        upsert_report(update.effective_user.id, ctx.user_data)
        word = "перезаписана" if ctx.user_data.get("overwrite") else "сохранена"
        await update.message.reply_text(f"✅ Запись {word}!", reply_markup=MAIN_KB)
    else:
        await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KB)
    ctx.user_data.clear()
    return ConversationHandler.END

# ── Диалог: дополнить запись ─────────────────────────────────────────────────

async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    rows = get_reports(uid)
    if not rows:
        await update.message.reply_text("📭 Записей нет — нечего дополнять.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["mode"] = "append"

    # Показываем список последних записей с кнопками
    text = "✏️ *Выберите запись для дополнения:*\n\n"
    buttons = []
    for row in rows[:10]:  # максимум 10
        num, date, addr, work, tf, tt, col, share = row
        short = (work or addr or "—")[:30]
        text += f"*#{num}* {date} — {short}\n"
        buttons.append([KeyboardButton(f"#{num}")])
    buttons.append([KeyboardButton("❌ Отмена")])

    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )
    return EDIT_NUM

async def edit_num(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Отмена":
        await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KB)
        ctx.user_data.clear()
        return ConversationHandler.END

    m = re.search(r"(\d+)", text)
    if not m:
        await update.message.reply_text("Введите номер записи, например #1")
        return EDIT_NUM

    num = int(m.group(1))
    row = get_report_by_num(update.effective_user.id, num)
    if not row:
        await update.message.reply_text(f"❌ Записи #{num} нет. Попробуйте ещё раз.")
        return EDIT_NUM

    _, date, addr, work, tf, tt, col, share = row
    ctx.user_data["edit_date"] = date
    ctx.user_data["edit_num"]  = num

    await update.message.reply_text(
        f"✏️ *Дополняем запись #{num} от {date}*\n\n"
        f"📍 {addr or '—'}\n🔧 {work or '—'}\n"
        f"💰 {col or '—'} ₽  🟢 {share or '—'} ₽\n\n"
        f"Новые данные будут добавлены через запятую к существующим.\n"
        f"Деньги — суммируются.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "📍 *Доп. адрес*\n\nДобавить адрес?",
        parse_mode="Markdown",
        reply_markup=skip_kb("Не менялось")
    )
    return EDIT_ADDR

async def edit_addr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["addresses"] = "" if text == "Не менялось" else text
    await update.message.reply_text(
        "🔧 *Доп. работа*\n\nЧто ещё сделали?\n_Можно указать время: «с 14 до 18»_",
        parse_mode="Markdown",
        reply_markup=skip_kb("Не менялось")
    )
    return EDIT_WORK

async def edit_work(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["work_done"] = "" if text == "Не менялось" else text
    await update.message.reply_text(
        "💰 *Доп. сумма*\n\nЕщё взяли с клиента? (будет прибавлена к существующей)",
        parse_mode="Markdown",
        reply_markup=skip_kb("Не менялось")
    )
    return EDIT_COLLECTED

async def edit_collected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["collected"] = "" if text == "Не менялось" else re.sub(r"[^\d]","",text)
    await update.message.reply_text(
        "🟢 *Доп. доля*\n\nЕщё добавить к вашей доле?",
        parse_mode="Markdown",
        reply_markup=skip_kb("Не менялось")
    )
    return EDIT_SHARE

async def edit_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["my_share"] = "" if text == "Не менялось" else re.sub(r"[^\d]","",text)

    uid  = update.effective_user.id
    date = ctx.user_data["edit_date"]
    num  = ctx.user_data["edit_num"]

    # Показываем что получится после слияния
    old_row = get_report_by_date(uid, date)
    if old_row:
        _, old_addr, old_work, old_tf, old_tt, old_col, old_share = old_row

        def preview_merge(old, new, is_money=False):
            old = (old or "").strip()
            new = (new or "").strip()
            if not new or new == "Не менялось": return old or "—"
            if is_money:
                try: return str(int(old or 0) + int(re.sub(r"[^\d]","",new)))
                except: return old or "—"
            return (old + ", " + new) if old else new

        p_addr  = preview_merge(old_addr,  ctx.user_data.get("addresses",""))
        p_work  = preview_merge(old_work,  ctx.user_data.get("work_done",""))
        p_col   = preview_merge(old_col,   ctx.user_data.get("collected",""), is_money=True)
        p_share = preview_merge(old_share, ctx.user_data.get("my_share",""),  is_money=True)
        tf, tt  = parse_time(ctx.user_data.get("work_done",""))
        p_tf    = tf or old_tf or ""
        p_tt    = tt or old_tt or ""

        lines = [
            f"📋 *Запись #{num} после дополнения:*\n",
            f"📅 {date}",
            f"📍 {p_addr}",
            f"🔧 {p_work}",
        ]
        if p_tf or p_tt: lines.append(f"🕐 {p_tf or '?'} — {p_tt or '?'}")
        lines += [f"💰 Взято: *{p_col} ₽*", f"🟢 Моя доля: *{p_share} ₽*"]

        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Сохранить"), KeyboardButton("❌ Отмена")]],
                resize_keyboard=True
            )
        )
    return EDIT_CONFIRM

async def edit_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == "✅ Сохранить":
        uid  = update.effective_user.id
        date = ctx.user_data["edit_date"]
        num  = ctx.user_data["edit_num"]
        ok   = append_to_report(uid, date, ctx.user_data)
        if ok:
            await update.message.reply_text(f"✅ Запись #{num} дополнена!", reply_markup=MAIN_KB)
        else:
            await update.message.reply_text("❌ Ошибка при сохранении.", reply_markup=MAIN_KB)
    else:
        await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KB)
    ctx.user_data.clear()
    return ConversationHandler.END

# ── cancel ────────────────────────────────────────────────────────────────────

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END

# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_reminder(update.effective_user.id, True)
    await update.message.reply_text(
        "👋 *Привет! Я ваш рабочий журнал.*\n\n"
        "Нажмите *➕ Новая запись* — заполним по шагам.\n"
        "Нажмите *✏️ Дополнить запись* — добавим к уже существующей.\n\n"
        "*/delete 1* — удалить запись №1\n"
        "*/reminder on|off* — напоминание",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_reports(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📭 Записей пока нет.", reply_markup=MAIN_KB)
        return
    total_col   = sum(int(r[6]) for r in rows if str(r[6]).isdigit())
    total_share = sum(int(r[7]) for r in rows if str(r[7]).isdigit())
    text = "📋 *Все записи:*\n\n"
    for row in rows:
        block = fmt_row(row) + "\n\n"
        if len(text) + len(block) > 3800:
            await update.message.reply_text(text, parse_mode="Markdown")
            text = ""
        text += block
    text += f"💰 Итого взято: *{total_col:,} ₽*\n🟢 Итого моё: *{total_share:,} ₽*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Укажите номер: /delete 1\nНомера видны в /list (значок #).",
            reply_markup=MAIN_KB
        )
        return
    num = int(args[0])
    if delete_by_num(update.effective_user.id, num):
        await update.message.reply_text(f"✅ Запись #{num} удалена.", reply_markup=MAIN_KB)
    else:
        await update.message.reply_text(f"❌ Записи #{num} нет.", reply_markup=MAIN_KB)

async def cmd_excel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_reports(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📭 Нет записей.", reply_markup=MAIN_KB)
        return
    data  = make_csv(rows)
    fname = f"отчёт_{today_str()}.csv"
    await update.message.reply_document(
        document=io.BytesIO(data), filename=fname,
        caption=f"📥 {len(rows)} записей. Открывайте в Excel или Google Таблицах."
    )

async def cmd_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or args[0].lower() not in ("on","off"):
        await update.message.reply_text(
            f"⚙️ *Напоминание*\n\n"
            f"Каждый день в {REMINDER_HOUR:02d}:{REMINDER_MIN:02d} ({TZ_NAME})\n\n"
            f"/reminder on — включить 🔔\n"
            f"/reminder off — выключить 🔕",
            parse_mode="Markdown", reply_markup=MAIN_KB
        )
        return
    enabled = args[0].lower() == "on"
    set_reminder(update.effective_user.id, enabled)
    await update.message.reply_text(
        f"✅ Напоминание {'включено 🔔' if enabled else 'выключено 🔕'}",
        reply_markup=MAIN_KB
    )

async def handle_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Мои записи":
        await cmd_list(update, ctx)
    elif text == "📥 Скачать Excel":
        await cmd_excel(update, ctx)
    elif text == "⚙️ Напоминание":
        ctx.args = []
        await cmd_reminder(update, ctx)

# ── Cron ─────────────────────────────────────────────────────────────────────

async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    for uid in get_reminder_users():
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text="🔔 *Не забудьте добавить запись за сегодня!*",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    # Диалог: новая запись
    conv_new = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_entry),
            MessageHandler(filters.Regex("^➕ Новая запись$"), new_entry),
        ],
        states={
            DATE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_date)],
            ADDRESS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_address)],
            WORK:      [MessageHandler(filters.TEXT & ~filters.COMMAND, step_work)],
            COLLECTED: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_collected)],
            SHARE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_share)],
            CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалог: дополнить запись
    conv_edit = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_entry),
            MessageHandler(filters.Regex("^✏️ Дополнить запись$"), edit_entry),
        ],
        states={
            EDIT_NUM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_num)],
            EDIT_ADDR:      [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_addr)],
            EDIT_WORK:      [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_work)],
            EDIT_COLLECTED: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_collected)],
            EDIT_SHARE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_share)],
            EDIT_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_new)
    app.add_handler(conv_edit)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("delete",   cmd_delete))
    app.add_handler(CommandHandler("excel",    cmd_excel))
    app.add_handler(CommandHandler("reminder", cmd_reminder))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND &
        filters.Regex("^(📋 Мои записи|📥 Скачать Excel|⚙️ Напоминание)$"),
        handle_menu
    ))

    tz = ZoneInfo(TZ_NAME)
    reminder_time = datetime.now(tz).replace(
        hour=REMINDER_HOUR, minute=REMINDER_MIN, second=0, microsecond=0
    ).timetz()
    app.job_queue.run_daily(send_reminders, time=reminder_time, days=tuple(range(7)))

    print("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
