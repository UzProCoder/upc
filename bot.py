"""
UzProCoder Support — startaplar uchun murojaatlar (support) boti.
To'liq INLINE tugmalarda ishlaydi. aiogram 3 + SQLite.
"""
import asyncio
import html
import logging
import os
from datetime import datetime
from math import ceil

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402  (DB_PATH .env dan o'qilgandan keyin)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

BRAND = "UzProCoder Support"
PAGE_SIZE = 8
MAX_TICKET_LEN = 2500
MAX_REPLY_LEN = 1500
MAX_ACTIVE_TICKETS = 5

log = logging.getLogger("support-bot")

STATUS = {
    "new": "🆕 Yangi",
    "accepted": "🔄 Ko'rib chiqilmoqda",
    "resolved": "✅ Nazoratdan yechildi",
    "rejected": "❌ Rad etildi",
}
STATUS_EMOJI = {"new": "🆕", "accepted": "🔄", "resolved": "✅", "rejected": "❌"}
PROGRESS = {
    "new": "▰▱▱ 1/3 — Yuborildi, ko'rib chiqilishi kutilmoqda",
    "accepted": "▰▰▱ 2/3 — Mutaxassis ko'rib chiqmoqda",
    "resolved": "▰▰▰ 3/3 — Murojaat hal qilindi",
    "rejected": "▰▰✖ Murojaat rad etildi",
}
LOG_LABEL = {
    "created": "📨 Yuborildi",
    "accepted": "🔄 Qabul qilindi",
    "resolved": "✅ Nazoratdan yechildi",
    "rejected": "❌ Rad etildi",
    "reopened": "♻️ Qayta ochildi",
    "reply_staff": "💬 Xodim javobi",
    "reply_user": "🗨 Murojaatchi xabari",
}


# ============================================================ yordamchilar
def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def fmt_dt(s) -> str:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return s or "—"


def short(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pager(prefix: str, page: int, pages: int):
    row = []
    if page > 0:
        row.append(btn("⬅️", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("➡️", f"{prefix}:{page + 1}"))
    return row


async def show(target, text: str, markup=None, toast: str | None = None):
    """CallbackQuery bo'lsa xabarni tahrirlaydi, Message bo'lsa yangi xabar yuboradi."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                await target.message.answer(text, reply_markup=markup)
        await target.answer(toast)
    else:
        await target.answer(text, reply_markup=markup)


async def safe_send(bot: Bot, chat_id: int, text: str, markup=None):
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramAPIError as e:
        log.warning("Xabar yuborilmadi (%s): %s", chat_id, e)
        return None


async def staff_scope(uid: int):
    """(xodimmi, project_ids yoki None=hammasi)."""
    if uid in ADMIN_IDS:
        return True, None
    ids = await db.responsible_project_ids(uid)
    return bool(ids), ids


async def ticket_for_staff(uid: int, tid: int):
    t = await db.get_ticket(tid)
    if t and (uid in ADMIN_IDS or await db.is_responsible(t["project_id"], uid)):
        return t
    return None


def can_manage(t, uid: int) -> bool:
    return uid in ADMIN_IDS or t["handler_id"] == uid


# ============================================================ matnlar
def ticket_card(t, events, for_staff: bool) -> str:
    lines = [
        f"🎫 <b>Murojaat #{t['id']}</b>",
        f"🏢 Startup: <b>{esc(t['project_name'])}</b>",
        f"📌 Holat: <b>{STATUS[t['status']]}</b>",
        PROGRESS[t["status"]],
        "",
    ]
    if for_staff:
        uname = f" (@{esc(t['username'])})" if t["username"] else ""
        lines.append(f"👤 Murojaatchi: <b>{esc(t['user_name'])}</b>{uname}")
        lines.append(f"🆔 Telegram ID: <code>{t['user_id']}</code>")
    lines.append(f"🕒 Yuborilgan: {fmt_dt(t['created_at'])}")
    lines.append(f"🔁 Yangilangan: {fmt_dt(t['updated_at'])}")
    if for_staff and t["handler_name"]:
        lines.append(f"👨‍💼 Mas'ul xodim: <b>{esc(t['handler_name'])}</b>")
    if t["note"] and t["status"] in ("resolved", "rejected"):
        lines.append(f"📝 Izoh: {esc(t['note'])}")
    lines += ["", "💬 <b>Murojaat mazmuni:</b>", esc(t["text"])]
    if events:
        lines += ["", "🕘 <b>So'nggi harakatlar:</b>"]
        for e in events:
            label = LOG_LABEL.get(e["kind"], e["kind"])
            who = f" — {esc(e['actor_name'])}" if for_staff and e["actor_name"] else ""
            snippet = f"\n   <i>{esc(short(e['text']))}</i>" if e["text"] else ""
            lines.append(f"• {fmt_dt(e['created_at'])} · {label}{who}{snippet}")
    return "\n".join(lines)


HELP_TEXT = (
    f"ℹ️ <b>{BRAND} — yordam</b>\n\n"
    "Bu bot orqali bizning startap va saytlarimiz bo'yicha murojaat yuborishingiz mumkin.\n\n"
    "<b>Qanday ishlaydi?</b>\n"
    "1️⃣ «📝 Yangi murojaat» tugmasini bosing\n"
    "2️⃣ Qaysi startap bo'yicha yozayotganingizni tanlang\n"
    "3️⃣ Muammo yoki savolingizni batafsil yozing\n"
    "4️⃣ Murojaatingiz mas'ul xodimga yetkaziladi va sizga <b>ID raqam</b> beriladi\n\n"
    "Murojaat holati o'zgarganda sizga avtomatik xabar keladi. "
    "Holatni «🔎 Holatni tekshirish» orqali ID raqam bilan ham bilib olishingiz mumkin."
)


# ============================================================ klaviaturalar
async def main_menu_kb(uid: int):
    rows = [
        [btn("📝 Yangi murojaat", "u:new")],
        [btn("📂 Murojaatlarim", "u:my:0"), btn("🔎 Holatni tekshirish", "u:check")],
        [btn("ℹ️ Yordam", "u:help")],
    ]
    is_staff, _ = await staff_scope(uid)
    extra = []
    if is_staff:
        extra.append(btn("🛠 Xodim paneli", "s:home"))
    if uid in ADMIN_IDS:
        extra.append(btn("👑 Admin panel", "a:home"))
    if extra:
        rows.append(extra)
    return kb(rows)


def staff_ticket_kb(t):
    tid, st = t["id"], t["status"]
    rows = []
    if st == "new":
        rows.append([btn("✅ Qabul qilish", f"s:accept:{tid}"), btn("❌ Rad etish", f"s:reject:{tid}")])
    elif st == "accepted":
        rows.append([btn("💬 Javob yozish", f"s:reply:{tid}")])
        rows.append([btn("✅ Nazoratdan yechish", f"s:resolve:{tid}"), btn("❌ Rad etish", f"s:reject:{tid}")])
    else:
        rows.append([btn("♻️ Qayta ochish", f"s:reopen:{tid}")])
    rows.append([btn("🔄 Yangilash", f"s:view:{tid}")])
    rows.append([btn("📋 Ro'yxat", "s:list:all:0"), btn("🏠 Panel", "s:home")])
    return kb(rows)


def user_ticket_kb(t):
    tid = t["id"]
    rows = [[btn("🔄 Yangilash", f"u:view:{tid}")]]
    if t["status"] in ("new", "accepted"):
        rows.append([btn("💬 Xabar qo'shish", f"u:reply:{tid}")])
    rows.append([btn("📂 Murojaatlarim", "u:my:0"), btn("🏠 Menyu", "u:menu")])
    return kb(rows)


# ============================================================ bildirishnomalar
async def notify_staff_new(bot: Bot, tid: int):
    t = await db.get_ticket(tid)
    events = await db.get_log(tid, 5)
    text = "🔔 <b>Yangi murojaat keldi!</b>\n\n" + ticket_card(t, events, True)
    for chat_id in await db.staff_ids(t["project_id"], ADMIN_IDS):
        msg = await safe_send(bot, chat_id, text, staff_ticket_kb(t))
        if msg:
            await db.save_notification(tid, chat_id, msg.message_id)


async def refresh_notifications(bot: Bot, tid: int):
    """Barcha xodimlardagi murojaat kartasini yangi holatga keltiradi."""
    t = await db.get_ticket(tid)
    events = await db.get_log(tid, 5)
    text = ticket_card(t, events, True)
    for n in await db.get_notifications(tid):
        try:
            await bot.edit_message_text(
                text, chat_id=n["chat_id"], message_id=n["message_id"], reply_markup=staff_ticket_kb(t)
            )
        except TelegramAPIError:
            pass


async def notify_user_status(bot: Bot, t, kind: str):
    tid = t["id"]
    note = f"\n\n📝 <b>Izoh:</b> {esc(t['note'])}" if t["note"] else ""
    heads = {
        "accepted": "🔄 <b>Murojaatingiz qabul qilindi</b>\nMutaxassis uni ko'rib chiqmoqda.",
        "resolved": "✅ <b>Murojaatingiz nazoratdan yechildi</b>\nMuammo hal qilindi deb belgilandi.",
        "rejected": "❌ <b>Murojaatingiz rad etildi</b>",
        "reopened": "♻️ <b>Murojaatingiz qayta ochildi</b>\nMutaxassis uni qayta ko'rib chiqmoqda.",
    }
    text = (
        f"{heads[kind]}{note if kind in ('resolved', 'rejected') else ''}\n\n"
        f"🎫 Murojaat: <b>#{tid}</b> · {esc(t['project_name'])}\n"
        f"{PROGRESS[t['status']]}"
    )
    await safe_send(bot, t["user_id"], text, kb([[btn("🎫 Murojaatni ko'rish", f"u:view:{tid}")]]))


# ============================================================ FSM holatlari
class NewTicket(StatesGroup):
    text = State()
    confirm = State()


class CheckTicket(StatesGroup):
    ticket_id = State()


class UserReply(StatesGroup):
    text = State()


class StaffNote(StatesGroup):
    text = State()


class StaffReply(StatesGroup):
    text = State()


class ProjectForm(StatesGroup):
    name = State()
    description = State()
    rename = State()
    redesc = State()
    add_resp = State()


router = Router()
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
fallback_router = Router()


# ============================================================ /start va menyu
async def send_menu(target, user):
    text = (
        f"👋 <b>Assalomu alaykum, {esc(user.full_name)}!</b>\n\n"
        f"<b>{BRAND}</b> botiga xush kelibsiz.\n"
        "Bu yerda bizning startap va saytlarimiz bo'yicha murojaat qoldirishingiz, "
        "uning holatini kuzatib borishingiz mumkin.\n\n"
        "Kerakli bo'limni tanlang 👇"
    )
    await show(target, text, await main_menu_kb(user.id))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.full_name, u.username)
    await send_menu(message, u)


@router.callback_query(F.data == "u:menu")
async def u_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_menu(cb, cb.from_user)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "u:help")
async def u_help(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, HELP_TEXT, kb([[btn("🏠 Menyu", "u:menu")]]))


# ============================================================ FOYDALANUVCHI: yangi murojaat
@router.callback_query(F.data == "u:new")
async def u_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    projects = await db.list_projects(only_active=True)
    if not projects:
        await show(
            cb,
            "😔 Hozircha murojaat qabul qilinadigan startaplar mavjud emas.\nKeyinroq urinib ko'ring.",
            kb([[btn("🏠 Menyu", "u:menu")]]),
        )
        return
    rows = [[btn(f"🏢 {p['name']}", f"u:proj:{p['id']}")] for p in projects]
    rows.append([btn("⬅️ Orqaga", "u:menu")])
    await show(cb, "🏢 <b>Qaysi startap/sayt bo'yicha murojaat qilmoqchisiz?</b>\n\nRo'yxatdan tanlang 👇", kb(rows))


@router.callback_query(F.data.startswith("u:proj:"))
async def u_project(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    p = await db.get_project(pid)
    if not p or not p["active"]:
        await cb.answer("Bu startap hozir murojaat qabul qilmaydi", show_alert=True)
        return
    await state.set_state(NewTicket.text)
    await state.update_data(project_id=pid)
    desc = f"\n<i>{esc(p['description'])}</i>\n" if p["description"] else ""
    await show(
        cb,
        f"🏢 <b>{esc(p['name'])}</b>{desc}\n"
        "✍️ Muammo yoki savolingizni <b>batafsil</b> yozib yuboring.\n"
        f"(Eng ko'pi bilan {MAX_TICKET_LEN} belgi)",
        kb([[btn("⬅️ Startap tanlash", "u:new"), btn("❌ Bekor qilish", "u:menu")]]),
    )


@router.message(NewTicket.text)
async def u_ticket_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Iltimos, murojaatni <b>matn</b> ko'rinishida yozing.")
        return
    text = message.text.strip()
    if len(text) < 5:
        await message.answer("⚠️ Murojaat juda qisqa. Iltimos, batafsilroq yozing.")
        return
    if len(text) > MAX_TICKET_LEN:
        await message.answer(f"⚠️ Matn juda uzun ({len(text)} belgi). {MAX_TICKET_LEN} belgidan oshmasin.")
        return
    data = await state.get_data()
    p = await db.get_project(data["project_id"])
    await state.update_data(text=text)
    await state.set_state(NewTicket.confirm)
    await message.answer(
        f"📋 <b>Murojaatingizni tekshiring</b>\n\n🏢 Startup: <b>{esc(p['name'])}</b>\n\n💬 {esc(text)}",
        reply_markup=kb(
            [
                [btn("✅ Yuborish", "u:send")],
                [btn("✏️ Qayta yozish", f"u:proj:{p['id']}"), btn("❌ Bekor qilish", "u:menu")],
            ]
        ),
    )


@router.callback_query(NewTicket.confirm, F.data == "u:send")
async def u_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    p = await db.get_project(data.get("project_id", 0))
    if not p or not p["active"] or not data.get("text"):
        await state.clear()
        await cb.answer("Sessiya eskirgan, qaytadan boshlang", show_alert=True)
        return
    if await db.active_user_tickets(cb.from_user.id) >= MAX_ACTIVE_TICKETS:
        await state.clear()
        await show(
            cb,
            f"⚠️ Sizda {MAX_ACTIVE_TICKETS} ta ko'rib chiqilayotgan murojaat bor.\n"
            "Iltimos, avvalgilari yakunlanishini kuting.",
            kb([[btn("📂 Murojaatlarim", "u:my:0")], [btn("🏠 Menyu", "u:menu")]]),
        )
        return
    u = cb.from_user
    tid = await db.create_ticket(p["id"], u.id, u.full_name, u.username, data["text"])
    await db.add_log(tid, u.id, u.full_name, "created")
    await state.clear()
    await show(
        cb,
        "✅ <b>Murojaatingiz qabul qilindi!</b>\n\n"
        f"🎫 Murojaat ID: <b>#{tid}</b>\n🏢 Startup: <b>{esc(p['name'])}</b>\n\n"
        "Holat o'zgarganda sizga xabar beramiz. ID raqamni saqlab qo'ying 📌",
        kb([[btn("🎫 Murojaatni ko'rish", f"u:view:{tid}")], [btn("🏠 Menyu", "u:menu")]]),
    )
    await notify_staff_new(bot, tid)


# ============================================================ FOYDALANUVCHI: ro'yxat, ko'rish, tekshirish
@router.callback_query(F.data.startswith("u:my:"))
async def u_my(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(cb.data.split(":")[2])
    total = await db.count_user_tickets(cb.from_user.id)
    if total == 0:
        await show(cb, "📂 Sizda hali murojaatlar yo'q.", kb([[btn("📝 Yangi murojaat", "u:new")], [btn("🏠 Menyu", "u:menu")]]))
        return
    pages = ceil(total / PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    items = await db.user_tickets(cb.from_user.id, PAGE_SIZE, page * PAGE_SIZE)
    rows = [
        [btn(f"#{t['id']} {STATUS_EMOJI[t['status']]} {short(t['project_name'], 22)} · {fmt_dt(t['created_at'])[:5]}", f"u:view:{t['id']}")]
        for t in items
    ]
    if pages > 1:
        rows.append(pager("u:my", page, pages))
    rows.append([btn("🏠 Menyu", "u:menu")])
    await show(cb, f"📂 <b>Murojaatlaringiz</b> — jami {total} ta\n\nKo'rish uchun tanlang 👇", kb(rows))


async def show_user_ticket(target, t, toast=None):
    events = await db.get_log(t["id"], 5)
    await show(target, ticket_card(t, events, False), user_ticket_kb(t), toast)


@router.callback_query(F.data.startswith("u:view:"))
async def u_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    t = await db.get_ticket(int(cb.data.split(":")[2]))
    if not t or t["user_id"] != cb.from_user.id:
        await cb.answer("Murojaat topilmadi", show_alert=True)
        return
    await show_user_ticket(cb, t, "Yangilandi")


@router.callback_query(F.data == "u:check")
async def u_check(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(CheckTicket.ticket_id)
    await show(
        cb,
        "🔎 <b>Murojaat holatini tekshirish</b>\n\nMurojaat <b>ID raqamini</b> yuboring (masalan: <code>12</code>).",
        kb([[btn("❌ Bekor qilish", "u:menu")]]),
    )


@router.message(CheckTicket.ticket_id)
async def u_check_id(message: Message, state: FSMContext):
    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("⚠️ Faqat raqam yuboring, masalan: <code>12</code>")
        return
    t = await db.get_ticket(int(raw))
    if not t or t["user_id"] != message.from_user.id:
        await message.answer(
            f"😕 <b>#{esc(raw)}</b> raqamli murojaat sizning hisobingizda topilmadi.\nID ni tekshirib, qayta yuboring.",
            reply_markup=kb([[btn("❌ Bekor qilish", "u:menu")]]),
        )
        return
    await state.clear()
    await show_user_ticket(message, t)


# ============================================================ FOYDALANUVCHI: qo'shimcha xabar
@router.callback_query(F.data.startswith("u:reply:"))
async def u_reply(cb: CallbackQuery, state: FSMContext):
    t = await db.get_ticket(int(cb.data.split(":")[2]))
    if not t or t["user_id"] != cb.from_user.id:
        await cb.answer("Murojaat topilmadi", show_alert=True)
        return
    if t["status"] not in ("new", "accepted"):
        await cb.answer("Bu murojaat yopilgan. Yangi murojaat yuboring.", show_alert=True)
        return
    await state.set_state(UserReply.text)
    await state.update_data(tid=t["id"])
    await show(
        cb,
        f"💬 <b>Murojaat #{t['id']}</b> uchun qo'shimcha xabaringizni yozing:",
        kb([[btn("❌ Bekor qilish", f"u:view:{t['id']}")]]),
    )


@router.message(UserReply.text)
async def u_reply_text(message: Message, state: FSMContext, bot: Bot):
    if not message.text or len(message.text) > MAX_REPLY_LEN:
        await message.answer(f"⚠️ Matn yuboring (eng ko'pi bilan {MAX_REPLY_LEN} belgi).")
        return
    data = await state.get_data()
    t = await db.get_ticket(data["tid"])
    await state.clear()
    if not t or t["status"] not in ("new", "accepted"):
        await message.answer("⚠️ Murojaat yopilgan.", reply_markup=kb([[btn("🏠 Menyu", "u:menu")]]))
        return
    u = message.from_user
    await db.add_log(t["id"], u.id, u.full_name, "reply_user", message.text)
    recipients = {t["handler_id"]} if t["status"] == "accepted" and t["handler_id"] else await db.staff_ids(t["project_id"], ADMIN_IDS)
    text = (
        f"🗨 <b>Murojaatchidan yangi xabar</b>\n🎫 #{t['id']} · {esc(t['project_name'])}\n"
        f"👤 {esc(t['user_name'])}\n\n{esc(message.text)}"
    )
    markup = kb([[btn("💬 Javob yozish", f"s:reply:{t['id']}")], [btn("🎫 Murojaatni ko'rish", f"s:view:{t['id']}")]])
    for chat_id in recipients:
        await safe_send(bot, chat_id, text, markup)
    await refresh_notifications(bot, t["id"])
    await message.answer("✅ Xabaringiz yuborildi.", reply_markup=kb([[btn("🎫 Murojaatga qaytish", f"u:view:{t['id']}")]]))


# ============================================================ XODIM PANELI
@router.callback_query(F.data == "s:home")
async def s_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ok, scope = await staff_scope(cb.from_user.id)
    if not ok:
        await cb.answer("⛔ Sizda xodim huquqi yo'q", show_alert=True)
        return
    c = await db.status_counts(scope)
    total = sum(c.values())
    text = (
        "🛠 <b>Xodim paneli</b>\n\n"
        f"🆕 Yangi: <b>{c.get('new', 0)}</b>\n"
        f"🔄 Ko'rib chiqilmoqda: <b>{c.get('accepted', 0)}</b>\n"
        f"✅ Nazoratdan yechilgan: <b>{c.get('resolved', 0)}</b>\n"
        f"❌ Rad etilgan: <b>{c.get('rejected', 0)}</b>\n"
        f"📊 Jami: <b>{total}</b>"
    )
    rows = [
        [btn(f"🆕 Yangi ({c.get('new', 0)})", "s:list:new:0"), btn(f"🔄 Jarayonda ({c.get('accepted', 0)})", "s:list:accepted:0")],
        [btn(f"✅ Yechilgan ({c.get('resolved', 0)})", "s:list:resolved:0"), btn(f"❌ Rad ({c.get('rejected', 0)})", "s:list:rejected:0")],
        [btn(f"📋 Barchasi ({total})", "s:list:all:0")],
        [btn("🏠 Menyu", "u:menu")],
    ]
    await show(cb, text, kb(rows))


@router.callback_query(F.data.startswith("s:list:"))
async def s_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ok, scope = await staff_scope(cb.from_user.id)
    if not ok:
        await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
        return
    _, _, status, page = cb.data.split(":")
    page = int(page)
    total = await db.count_tickets(scope, status)
    title = "📋 Barcha murojaatlar" if status == "all" else f"{STATUS[status]} murojaatlar"
    if total == 0:
        await show(cb, f"<b>{title}</b>\n\nBu bo'limda murojaat yo'q.", kb([[btn("🛠 Panel", "s:home")]]))
        return
    pages = ceil(total / PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    items = await db.list_tickets(scope, status, PAGE_SIZE, page * PAGE_SIZE)
    rows = [
        [btn(f"#{t['id']} {STATUS_EMOJI[t['status']]} {short(t['project_name'], 20)} · {fmt_dt(t['created_at'])[:5]}", f"s:view:{t['id']}")]
        for t in items
    ]
    if pages > 1:
        rows.append(pager(f"s:list:{status}", page, pages))
    rows.append([btn("🛠 Panel", "s:home")])
    await show(cb, f"<b>{title}</b> — {total} ta", kb(rows))


async def show_staff_ticket(target, tid: int, toast=None):
    t = await db.get_ticket(tid)
    events = await db.get_log(tid, 5)
    await show(target, ticket_card(t, events, True), staff_ticket_kb(t), toast)


@router.callback_query(F.data.startswith("s:view:"))
async def s_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    tid = int(cb.data.split(":")[2])
    if not await ticket_for_staff(cb.from_user.id, tid):
        await cb.answer("⛔ Murojaat topilmadi yoki ruxsat yo'q", show_alert=True)
        return
    await show_staff_ticket(cb, tid, "Yangilandi")


@router.callback_query(F.data.startswith("s:accept:"))
async def s_accept(cb: CallbackQuery, bot: Bot):
    tid = int(cb.data.split(":")[2])
    u = cb.from_user
    t = await ticket_for_staff(u.id, tid)
    if not t:
        await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
        return
    if not await db.set_status(tid, "accepted", ["new"], handler_id=u.id, handler_name=u.full_name):
        await cb.answer("⚠️ Bu murojaat allaqachon qabul qilingan yoki yopilgan", show_alert=True)
        await show_staff_ticket(cb, tid)
        return
    await db.add_log(tid, u.id, u.full_name, "accepted")
    t = await db.get_ticket(tid)
    await notify_user_status(bot, t, "accepted")
    await refresh_notifications(bot, tid)
    await show_staff_ticket(cb, tid, "✅ Qabul qilindi")


@router.callback_query(F.data.startswith("s:reopen:"))
async def s_reopen(cb: CallbackQuery, bot: Bot):
    tid = int(cb.data.split(":")[2])
    u = cb.from_user
    if not await ticket_for_staff(u.id, tid):
        await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
        return
    if not await db.set_status(tid, "accepted", ["resolved", "rejected"], handler_id=u.id, handler_name=u.full_name):
        await cb.answer("⚠️ Murojaat holati o'zgargan", show_alert=True)
        await show_staff_ticket(cb, tid)
        return
    await db.add_log(tid, u.id, u.full_name, "reopened")
    t = await db.get_ticket(tid)
    await notify_user_status(bot, t, "reopened")
    await refresh_notifications(bot, tid)
    await show_staff_ticket(cb, tid, "♻️ Qayta ochildi")


# ---- yechish / rad etish (izoh bilan)
@router.callback_query(F.data.regexp(r"^s:(resolve|reject):\d+$"))
async def s_close_ask(cb: CallbackQuery, state: FSMContext):
    _, action, tid = cb.data.split(":")
    tid = int(tid)
    t = await ticket_for_staff(cb.from_user.id, tid)
    if not t:
        await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
        return
    if action == "resolve" and t["status"] != "accepted":
        await cb.answer("⚠️ Avval murojaatni qabul qiling", show_alert=True)
        return
    if t["status"] not in ("new", "accepted"):
        await cb.answer("⚠️ Murojaat allaqachon yopilgan", show_alert=True)
        return
    if t["status"] == "accepted" and not can_manage(t, cb.from_user.id):
        await cb.answer("⚠️ Buni faqat qabul qilgan xodim yoki admin qila oladi", show_alert=True)
        return
    await state.set_state(StaffNote.text)
    await state.update_data(tid=tid, action=action)
    title = "✅ Nazoratdan yechish" if action == "resolve" else "❌ Rad etish"
    await show(
        cb,
        f"<b>{title}</b> — murojaat #{tid}\n\nMurojaatchiga yuboriladigan <b>izoh/sababni</b> yozing "
        "yoki izohsiz davom eting.",
        kb([[btn("⏭ Izohsiz davom etish", "s:noteskip")], [btn("❌ Bekor qilish", f"s:view:{tid}")]]),
    )


async def finalize_close(bot: Bot, target, actor, tid: int, action: str, note):
    t = await ticket_for_staff(actor.id, tid)
    if not t:
        await show(target, "⛔ Ruxsat yo'q", kb([[btn("🛠 Panel", "s:home")]]))
        return
    if action == "resolve":
        ok = await db.set_status(tid, "resolved", ["accepted"], note=note)
        kind = "resolved"
    else:
        ok = await db.set_status(tid, "rejected", ["new", "accepted"], handler_id=actor.id, handler_name=actor.full_name, note=note)
        kind = "rejected"
    if not ok:
        await show(target, "⚠️ Murojaat holati o'zgargan.", kb([[btn("🎫 Murojaat", f"s:view:{tid}")]]))
        return
    await db.add_log(tid, actor.id, actor.full_name, kind, note)
    t = await db.get_ticket(tid)
    await notify_user_status(bot, t, kind)
    await refresh_notifications(bot, tid)
    await show_staff_ticket(target, tid, "Bajarildi")


@router.callback_query(StaffNote.text, F.data == "s:noteskip")
async def s_note_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await finalize_close(bot, cb, cb.from_user, data["tid"], data["action"], None)


@router.message(StaffNote.text)
async def s_note_text(message: Message, state: FSMContext, bot: Bot):
    if not message.text or len(message.text) > MAX_REPLY_LEN:
        await message.answer(f"⚠️ Matn yuboring (eng ko'pi bilan {MAX_REPLY_LEN} belgi).")
        return
    data = await state.get_data()
    await state.clear()
    await finalize_close(bot, message, message.from_user, data["tid"], data["action"], message.text.strip())


# ---- xodim javobi
@router.callback_query(F.data.startswith("s:reply:"))
async def s_reply(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    t = await ticket_for_staff(cb.from_user.id, tid)
    if not t:
        await cb.answer("⛔ Ruxsat yo'q", show_alert=True)
        return
    if t["status"] != "accepted":
        await cb.answer("⚠️ Javob yozish uchun avval murojaatni qabul qiling", show_alert=True)
        return
    if not can_manage(t, cb.from_user.id):
        await cb.answer("⚠️ Bu murojaatni boshqa xodim qabul qilgan", show_alert=True)
        return
    await state.set_state(StaffReply.text)
    await state.update_data(tid=tid)
    await show(
        cb,
        f"💬 <b>Murojaat #{tid}</b> — murojaatchiga javobingizni yozing:",
        kb([[btn("❌ Bekor qilish", f"s:view:{tid}")]]),
    )


@router.message(StaffReply.text)
async def s_reply_text(message: Message, state: FSMContext, bot: Bot):
    if not message.text or len(message.text) > MAX_REPLY_LEN:
        await message.answer(f"⚠️ Matn yuboring (eng ko'pi bilan {MAX_REPLY_LEN} belgi).")
        return
    data = await state.get_data()
    await state.clear()
    t = await ticket_for_staff(message.from_user.id, data["tid"])
    if not t or t["status"] != "accepted":
        await message.answer("⚠️ Murojaat holati o'zgargan.", reply_markup=kb([[btn("🛠 Panel", "s:home")]]))
        return
    sent = await safe_send(
        bot,
        t["user_id"],
        f"💬 <b>Murojaatingiz bo'yicha javob</b>\n🎫 #{t['id']} · {esc(t['project_name'])}\n\n{esc(message.text)}",
        kb([[btn("💬 Javob yozish", f"u:reply:{t['id']}")], [btn("🎫 Murojaatni ko'rish", f"u:view:{t['id']}")]]),
    )
    await db.add_log(t["id"], message.from_user.id, message.from_user.full_name, "reply_staff", message.text)
    warn = "" if sent else "\n\n⚠️ Foydalanuvchiga yetkazib bo'lmadi (u botni bloklagan bo'lishi mumkin)."
    await message.answer(
        f"✅ Javob yuborildi.{warn}",
        reply_markup=kb([[btn("🎫 Murojaatga qaytish", f"s:view:{t['id']}")], [btn("🛠 Panel", "s:home")]]),
    )
    await refresh_notifications(bot, t["id"])


# ============================================================ ADMIN PANEL
@admin_router.callback_query(F.data == "a:home")
async def a_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pr = await db.list_projects()
    c = await db.status_counts(None)
    text = (
        "👑 <b>Admin panel</b>\n\n"
        f"🏢 Startaplar: <b>{len(pr)}</b>\n"
        f"🆕 Yangi murojaatlar: <b>{c.get('new', 0)}</b>\n"
        f"📊 Jami murojaatlar: <b>{sum(c.values())}</b>"
    )
    rows = [
        [btn("🏢 Startaplar", "a:projects")],
        [btn("📊 Statistika", "a:stats"), btn("📋 Murojaatlar", "s:list:all:0")],
        [btn("🏠 Menyu", "u:menu")],
    ]
    await show(cb, text, kb(rows))


@admin_router.callback_query(F.data == "a:stats")
async def a_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    c = await db.status_counts(None)
    stats = await db.project_stats()
    lines = [
        "📊 <b>Statistika</b>\n",
        f"👥 Foydalanuvchilar: <b>{await db.users_count()}</b>",
        f"🆕 Yangi: <b>{c.get('new', 0)}</b> · 🔄 Jarayonda: <b>{c.get('accepted', 0)}</b>",
        f"✅ Yechilgan: <b>{c.get('resolved', 0)}</b> · ❌ Rad: <b>{c.get('rejected', 0)}</b>",
        "",
        "🏢 <b>Startaplar bo'yicha:</b>",
    ]
    for s in stats:
        lines.append(f"• {esc(s['name'])}: <b>{s['total']}</b> (yangi: {s['new_count']})")
    if not stats:
        lines.append("— hali startap qo'shilmagan")
    await show(cb, "\n".join(lines), kb([[btn("⬅️ Admin panel", "a:home")]]))


@admin_router.callback_query(F.data == "a:projects")
async def a_projects(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    projects = await db.list_projects()
    rows = [[btn(f"{'🟢' if p['active'] else '🔴'} {p['name']}", f"a:p:{p['id']}")] for p in projects]
    rows.append([btn("➕ Startap qo'shish", "a:padd")])
    rows.append([btn("⬅️ Admin panel", "a:home")])
    text = "🏢 <b>Startaplar</b>\n\n🟢 faol · 🔴 nofaol\nBoshqarish uchun tanlang 👇"
    if not projects:
        text = "🏢 <b>Startaplar</b>\n\nHali startap qo'shilmagan. «➕ Startap qo'shish» ni bosing."
    await show(cb, text, kb(rows))


async def show_project(target, pid: int):
    p = await db.get_project(pid)
    if not p:
        await show(target, "Startap topilmadi.", kb([[btn("⬅️ Startaplar", "a:projects")]]))
        return
    resp = await db.list_responsibles(pid)
    count = await db.project_ticket_count(pid)
    lines = [
        f"🏢 <b>{esc(p['name'])}</b>",
        f"📌 Holat: <b>{'🟢 Faol' if p['active'] else '🔴 Nofaol'}</b>",
        f"📝 Tavsif: {esc(p['description']) or '—'}",
        f"🎫 Murojaatlar: <b>{count}</b>",
        "",
        "👨‍💼 <b>Qo'shimcha mas'ullar:</b>",
    ]
    if resp:
        lines += [f"• {esc(r['name'])} — <code>{r['tg_id']}</code>" for r in resp]
    else:
        lines.append("— yo'q (murojaatlar faqat adminga keladi)")
    rows = [
        [btn("👤 Mas'ul qo'shish", f"a:radd:{pid}"), btn("🗑 Mas'ul o'chirish", f"a:rlist:{pid}")],
        [btn("✏️ Nomi", f"a:pren:{pid}"), btn("📝 Tavsif", f"a:pdesc:{pid}")],
        [btn("🔴 Nofaol qilish" if p["active"] else "🟢 Faollashtirish", f"a:ptoggle:{pid}")],
        [btn("🗑 Startapni o'chirish", f"a:pdel:{pid}")],
        [btn("⬅️ Startaplar", "a:projects")],
    ]
    await show(target, "\n".join(lines), kb(rows))


@admin_router.callback_query(F.data.startswith("a:p:"))
async def a_project(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_project(cb, int(cb.data.split(":")[2]))


@admin_router.callback_query(F.data.startswith("a:ptoggle:"))
async def a_toggle(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    p = await db.get_project(pid)
    if p:
        await db.update_project(pid, active=0 if p["active"] else 1)
    await show_project(cb, pid)


# ---- startap qo'shish
@admin_router.callback_query(F.data == "a:padd")
async def a_padd(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ProjectForm.name)
    await show(
        cb,
        "➕ <b>Yangi startap</b>\n\nStartap/sayt <b>nomini</b> yuboring (masalan: <i>O'zMU sayti</i>).",
        kb([[btn("❌ Bekor qilish", "a:projects")]]),
    )


@admin_router.message(ProjectForm.name)
async def a_padd_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 60:
        await message.answer("⚠️ Nom 2 dan 60 belgigacha bo'lsin. Qayta yuboring.")
        return
    await state.update_data(name=name)
    await state.set_state(ProjectForm.description)
    await message.answer(
        f"✅ Nom: <b>{esc(name)}</b>\n\nEndi qisqa <b>tavsif</b> yuboring (foydalanuvchiga ko'rinadi) yoki o'tkazib yuboring.",
        reply_markup=kb([[btn("⏭ Tavsifsiz", "a:pskip")], [btn("❌ Bekor qilish", "a:projects")]]),
    )


async def create_project(target, state: FSMContext, description: str):
    data = await state.get_data()
    await state.clear()
    try:
        pid = await db.add_project(data["name"], description)
    except db.IntegrityError:
        await show(target, "⚠️ Bunday nomli startap allaqachon mavjud.", kb([[btn("⬅️ Startaplar", "a:projects")]]))
        return
    await show_project(target, pid)


@admin_router.callback_query(ProjectForm.description, F.data == "a:pskip")
async def a_padd_skip(cb: CallbackQuery, state: FSMContext):
    await create_project(cb, state, "")


@admin_router.message(ProjectForm.description)
async def a_padd_desc(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) > 300:
        await message.answer("⚠️ Tavsif 300 belgidan oshmasin.")
        return
    await create_project(message, state, desc)


# ---- nom / tavsif tahrirlash
@admin_router.callback_query(F.data.startswith("a:pren:"))
async def a_rename(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ProjectForm.rename)
    await state.update_data(pid=pid)
    await show(cb, "✏️ Yangi <b>nomni</b> yuboring:", kb([[btn("❌ Bekor qilish", f"a:p:{pid}")]]))


@admin_router.message(ProjectForm.rename)
async def a_rename_text(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 60:
        await message.answer("⚠️ Nom 2 dan 60 belgigacha bo'lsin.")
        return
    pid = (await state.get_data())["pid"]
    try:
        await db.update_project(pid, name=name)
    except db.IntegrityError:
        await message.answer("⚠️ Bunday nom band. Boshqa nom yuboring.")
        return
    await state.clear()
    await show_project(message, pid)


@admin_router.callback_query(F.data.startswith("a:pdesc:"))
async def a_redesc(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ProjectForm.redesc)
    await state.update_data(pid=pid)
    await show(cb, "📝 Yangi <b>tavsifni</b> yuboring:", kb([[btn("❌ Bekor qilish", f"a:p:{pid}")]]))


@admin_router.message(ProjectForm.redesc)
async def a_redesc_text(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) > 300:
        await message.answer("⚠️ Tavsif 300 belgidan oshmasin.")
        return
    pid = (await state.get_data())["pid"]
    await db.update_project(pid, description=desc)
    await state.clear()
    await show_project(message, pid)


# ---- mas'ullar
@admin_router.callback_query(F.data.startswith("a:radd:"))
async def a_radd(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ProjectForm.add_resp)
    await state.update_data(pid=pid)
    await show(
        cb,
        "👤 <b>Mas'ul qo'shish</b>\n\nMas'ulning <b>Telegram ID</b> raqamini yuboring.\n"
        "Xohlasangiz ID dan keyin ism ham yozing:\n<code>123456789 Alisher</code>\n\n"
        "ℹ️ ID ni <b>@userinfobot</b> orqali bilish mumkin. Mas'ul botga bir marta /start bosgan bo'lishi kerak.",
        kb([[btn("❌ Bekor qilish", f"a:p:{pid}")]]),
    )


@admin_router.message(ProjectForm.add_resp)
async def a_radd_text(message: Message, state: FSMContext, bot: Bot):
    parts = (message.text or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer("⚠️ Birinchi qiymat Telegram ID (raqam) bo'lishi kerak. Masalan: <code>123456789 Alisher</code>")
        return
    tg_id = int(parts[0])
    name = parts[1].strip()[:50] if len(parts) > 1 else f"ID {tg_id}"
    pid = (await state.get_data())["pid"]
    p = await db.get_project(pid)
    await state.clear()
    await db.add_responsible(pid, tg_id, name)
    sent = await safe_send(
        bot,
        tg_id,
        f"👨‍💼 Siz <b>{esc(p['name'])}</b> startapi bo'yicha <b>mas'ul</b> etib tayinlandingiz.\n"
        "Endi shu startapdan kelgan murojaatlar sizga ham keladi.",
        kb([[btn("🛠 Xodim paneli", "s:home")]]),
    )
    if not sent:
        await message.answer(
            "⚠️ Mas'ul qo'shildi, lekin unga xabar yuborib bo'lmadi.\n"
            "U botga kirib /start bosishi kerak — shundan keyin murojaatlar keladi."
        )
    await show_project(message, pid)


@admin_router.callback_query(F.data.startswith("a:rlist:"))
async def a_rlist(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    resp = await db.list_responsibles(pid)
    if not resp:
        await cb.answer("Bu startapda mas'ullar yo'q", show_alert=True)
        return
    rows = [[btn(f"🗑 {r['name']} ({r['tg_id']})", f"a:rdel:{pid}:{r['tg_id']}")] for r in resp]
    rows.append([btn("⬅️ Orqaga", f"a:p:{pid}")])
    await show(cb, "🗑 <b>Qaysi mas'ulni o'chiramiz?</b>", kb(rows))


@admin_router.callback_query(F.data.startswith("a:rdel:"))
async def a_rdel(cb: CallbackQuery):
    _, _, pid, tg_id = cb.data.split(":")
    await db.remove_responsible(int(pid), int(tg_id))
    await show_project(cb, int(pid))


# ---- startapni o'chirish
@admin_router.callback_query(F.data.startswith("a:pdel:"))
async def a_pdel(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    count = await db.project_ticket_count(pid)
    if count:
        await show(
            cb,
            f"⚠️ Bu startapda <b>{count} ta murojaat</b> bor, shuning uchun o'chirib bo'lmaydi.\n"
            "O'rniga uni «🔴 Nofaol» qiling — yangi murojaat qabul qilinmaydi.",
            kb([[btn("🔴 Nofaol qilish", f"a:ptoggle:{pid}")], [btn("⬅️ Orqaga", f"a:p:{pid}")]]),
        )
        return
    await show(
        cb,
        "🗑 Startapni o'chirishni tasdiqlaysizmi? Bu amalni qaytarib bo'lmaydi.",
        kb([[btn("✅ Ha, o'chirish", f"a:pdelok:{pid}")], [btn("❌ Yo'q", f"a:p:{pid}")]]),
    )


@admin_router.callback_query(F.data.startswith("a:pdelok:"))
async def a_pdelok(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    if await db.project_ticket_count(pid) == 0:
        await db.delete_project(pid)
    projects = await db.list_projects()
    rows = [[btn(f"{'🟢' if p['active'] else '🔴'} {p['name']}", f"a:p:{p['id']}")] for p in projects]
    rows += [[btn("➕ Startap qo'shish", "a:padd")], [btn("⬅️ Admin panel", "a:home")]]
    await show(cb, "✅ Startap o'chirildi.\n\n🏢 <b>Startaplar</b>", kb(rows))


# ============================================================ zaxira (fallback)
@fallback_router.callback_query()
async def stale_callback(cb: CallbackQuery):
    await cb.answer("⚠️ Tugma eskirgan yoki ruxsat yo'q. /start bosing.", show_alert=True)


@fallback_router.message()
async def stray_message(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🤖 Iltimos, quyidagi tugmalardan foydalaning 👇",
        reply_markup=await main_menu_kb(message.from_user.id),
    )


# ============================================================ ishga tushirish
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN topilmadi. .env faylini tekshiring.")
    if not ADMIN_IDS:
        raise SystemExit("❌ ADMIN_IDS topilmadi. .env faylida admin Telegram ID sini yozing.")
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("❌ DATABASE_URL topilmadi (Supabase ulanish manzili).")
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(router)
    dp.include_router(fallback_router)
    await bot.set_my_commands([BotCommand(command="start", description="Botni ishga tushirish / menyu")])
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    log.info("Bot ishga tushdi: @%s", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
