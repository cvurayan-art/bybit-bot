import os
import time
import hmac
import hashlib
import json
import asyncio
import uuid
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG ---
SESSIONS_FILE = "sessions.json"
POLL_INTERVAL = 20 
ASK_API_KEY, ASK_SECRET_KEY = range(2)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
# Value should be: socks5://14a2f8ef26861:39867a4abb@109.121.41.136:12324
PROXY_URL      = os.environ.get("PROXY_URL", "") 

user_sessions = {}
monitoring_active = {}
seen_orders = {}

# --- CORE ASYNC BYBIT REQUEST ---
async def bybit_request(api_key, api_secret, method, endpoint, params=None, body=None):
    """Executes Bybit calls via IPRoyal SOCKS5 with remote DNS resolution."""
    ts = str(int(time.time() * 1000))
    recv_window = "10000"
    
    if method == "GET":
        qs = "&".join([f"{k}={v}" for k, v in sorted((params or {}).items())])
        payload = ts + api_key + recv_window + qs
    else:
        payload = ts + api_key + recv_window + json.dumps(body or {}, separators=(',', ':'))
    
    signature = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json"
    }

    # CRITICAL FIX: Use socks5h:// for remote DNS resolution
    # This prevents the timeout errors you saw.
    if PROXY_URL and PROXY_URL.startswith("socks5://"):
        fixed_proxy = PROXY_URL.replace("socks5://", "socks5h://")
    else:
        fixed_proxy = PROXY_URL

    mounts = {"all://": httpx.HTTPProxy(fixed_proxy)} if fixed_proxy else None
    
    async with httpx.AsyncClient(mounts=mounts, timeout=30.0) as client: 
        try:
            url = f"https://api.bybit.com{endpoint}"
            if method == "GET":
                r = await client.get(url, headers=headers, params=params)
            else:
                r = await client.post(url, headers=headers, content=json.dumps(body or {}, separators=(',', ':')))
            return r.json()
        except Exception as e:
            logger.error(f"❌ BYBIT SOCKS5 ERROR: {e}")
            return None

# --- P2P HANDLER ---
async def handle_order(app, cid, ak, sk, order):
    oid = order.get("id")
    if not oid or oid in seen_orders.get(cid, set()): return
    seen_orders.setdefault(cid, set()).add(oid)

    detail = await bybit_request(ak, sk, "POST", "/v5/p2p/order/info", body={"orderId": str(oid)})
    if not detail or detail.get("retCode") != 0: return
    
    res = detail["result"]
    amount, currency, seller = res["amount"], res["currencyId"], res["targetNickName"]
    
    p_id, p_type, bank_info = None, None, ""
    for term in res.get("paymentTermList", []):
        if term.get("online") == "1": continue
        p_id, p_type = term.get("id"), term.get("paymentType")
        bank_info = f"👤 `{term.get('realName')}`\n🏦 `{term.get('bankName')}`\n💳 `{term.get('accountNo')}`"
        break

    # AUTO-CLICK PAID
    action = "⚠️ Manual Action Needed"
    if p_id and p_type:
        p_res = await bybit_request(ak, sk, "POST", "/v5/p2p/order/pay", 
                                   body={"orderId": str(oid), "paymentType": str(p_type), "paymentId": str(p_id)})
        if p_res and p_res.get("retCode") == 0: action = "✅ Auto-Marked as Paid"

    # AUTO-MESSAGE SELLER
    msg = f"Paid {amount} {currency}. Please release. Thanks!"
    await bybit_request(ak, sk, "POST", "/v5/p2p/order/message/send", 
                       body={"orderId": str(oid), "message": msg, "contentType": "str", "msgUuid": uuid.uuid4().hex})

    # TELEGRAM ALERT
    text = (f"🚨 *NEW P2P TRADE*\n━━━━━━━━━━━━━\n📦 ID: `{oid}`\n👤 Seller: {seller}\n"
            f"💰 Send: *{amount} {currency}*\n━━━━━━━━━━━━━\n🏦 *BANK:*\n{bank_info}\n"
            f"━━━━━━━━━━━━━\n🤖 Bot: {action}")
    await app.bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")

# --- MONITORING LOOP ---
async def monitor_loop(app):
    while True:
        for cid, session in list(user_sessions.items()):
            if not monitoring_active.get(cid): continue
            resp = await bybit_request(session["api_key"], session["api_secret"], 
                                      "POST", "/v5/p2p/order/pending/simplifyList", body={"status": 10})
            if resp and resp.get("retCode") == 0:
                for order in resp.get("result", {}).get("items", []):
                    await handle_order(app, cid, session["api_key"], session["api_secret"], order)
        await asyncio.sleep(POLL_INTERVAL)

# --- TELEGRAM INTERFACE ---
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔑 Enter Bybit *API Key*:")
    return ASK_API_KEY

async def handle_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ak"] = update.message.text.strip()
    await update.message.reply_text("🔐 Enter *API Secret*:")
    return ASK_SECRET_KEY

async def handle_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid, ak, sk = update.effective_chat.id, context.user_data["ak"], update.message.text.strip()
    test = await bybit_request(ak, sk, "GET", "/v5/user/query-api")
    if test and test.get("retCode") == 0:
        user_sessions[cid] = {"api_key": ak, "api_secret": sk}
        monitoring_active[cid] = True
        await update.message.reply_text("✅ *Connection Verified!* Monitoring starting now.")
    else:
        await update.message.reply_text("❌ *Verification Failed.* Check Proxy or API Whitelist.")
    return ConversationHandler.END

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("login", login)],
        states={
            ASK_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key)],
            ASK_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_secret)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    app.add_handler(conv)
    asyncio.get_event_loop().create_task(monitor_loop(app))
    app.run_polling()

if __name__ == "__main__":
    main()
