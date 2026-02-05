import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "https://click-uper.com"
).rstrip("/")

MINIAPP_URL = os.getenv(
    "MINIAPP_URL",
    "https://click-uper.com/?v=12"
).rstrip("/")


def parse_ref(start_arg: str):
    # ожидаем ref_123
    if not start_arg:
        return None
    start_arg = str(start_arg).strip()
    if start_arg.startswith("ref_"):
        num = start_arg.replace("ref_", "").strip()
        if num.isdigit():
            return int(num)
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.message is None:
        return

    invited_tg_id = user.id
    referrer_tg_id = None

    # /start ref_123
    if context.args and len(context.args) > 0:
        referrer_tg_id = parse_ref(context.args[0])

    # 1) upsert invited в БД (реферал привяжется как referred_by)
    try:
        requests.post(
            f"{BACKEND_URL}/api/user/upsert",
            json={
                "tg_id": invited_tg_id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "referred_by": referrer_tg_id
            },
            timeout=10
        )
    except Exception:
        pass

    # 2) Если есть referrer — начислить ему 0.1 СРАЗУ (один раз)
    if referrer_tg_id and referrer_tg_id != invited_tg_id:
        try:
            requests.post(
                f"{BACKEND_URL}/api/referral/claim_start",
                json={
                    "referrer_tg_id": referrer_tg_id,
                    "invited_tg_id": invited_tg_id
                },
                timeout=10
            )
        except Exception:
            pass

    # 3) Кнопка открытия мини-апки
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open Mini App", web_app=WebAppInfo(url=MINIAPP_URL))]
    ])

    await update.message.reply_text(
        "Welcome! Tap the button to open the Mini App 👇",
        reply_markup=kb
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
