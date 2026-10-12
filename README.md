# 🤖 UzProCoder Support

Startap va saytlar uchun **murojaatlar (support) boti**. Python + aiogram 3 + **Supabase (PostgreSQL)**. Butun interfeys inline tugmalarda.

## Arxitektura
- **GitHub** — kod saqlanadi (tokenlar emas!)
- **Supabase** — ma'lumotlar bazasi (murojaatlar, startaplar, mas'ullar) — doimiy saqlanadi
- **Hosting** (VPS / Railway / Render ...) — `python bot.py` doimiy ishlab turadi
- Tokenlar va parollar faqat **Environment Variables** da

## Muhit o'zgaruvchilari
| Nom | Tavsif |
|---|---|
| `BOT_TOKEN` | @BotFather tokeni |
| `ADMIN_IDS` | Admin Telegram ID lari (vergul bilan) |
| `DATABASE_URL` | Supabase → Connect → **Session pooler** manzili |

Lokal ishga tushirishda ular `.env` faylida, hostingda esa platformaning **Variables** bo'limida turadi.

## Lokal ishga tushirish
```bash
python -m venv venv
venv\Scripts\activate        # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env       # Linux/Mac: cp .env.example .env
python bot.py
```

## Supabase sozlash
1. supabase.com → **New project** → baza parolini saqlang → yaqin region tanlang
2. Loyiha ochilgach **Connect** tugmasi → **Session pooler** manzilini nusxalang
3. `[YOUR-PASSWORD]` o'rniga parolni qo'ying (parolda `@ : / # ?` bo'lsa, URL-kodlang yoki oddiyroq parol tanlang)
4. Jadvallar bot birinchi ishga tushganda **o'zi yaratiladi**. RLS yoqilgan, ya'ni ochiq API orqali ma'lumotlarga kirib bo'lmaydi

## GitHub ga yuklash (veb orqali)
Repo → **Add file → Upload files**. Faqat shu fayllarni yuklang:
`bot.py`, `db.py`, `requirements.txt`, `Procfile`, `README.md`, `.gitignore`, `.env.example`

**`.env`, `venv/`, `data/` ni yuklamang!** Veb orqali yuklashda `.gitignore` ishlamaydi.

## Hostingga joylash
Hosting reponi GitHub dan oladi, `requirements.txt` ni o'zi o'rnatadi, ishga tushirish buyrug'i: `python bot.py` (turi: Worker / Background service, port kerak emas). Variables ga `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL` ni kiriting. Har safar GitHub'ga yangi fayl yuklasangiz, hosting avtomatik qayta ishga tushadi.

> Bir vaqtda bitta nusxa ishlasin: serverda ishlayotganda kompyuteringizdagi botni o'chiring (aks holda `Conflict: terminated by other getUpdates request` xatosi chiqadi).

## VPS da (systemd)
```ini
[Unit]
Description=UzProCoder Support Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/uzprocoder-support-bot
ExecStart=/opt/uzprocoder-support-bot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
`.env` faylini serverda qo'lda yarating (`nano .env`).
