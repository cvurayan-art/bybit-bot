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

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG & GLOBALS ---
SESSIONS_FILE = "sessions.json"
POLL_INTERVAL = 20 
ASK_API_KEY, ASK_SECRET_KEY = range(2)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PROXY_URL      = os.environ.get("PROXY_URL", "") # socks5://user:pass@host:port

user_sessions = {}
monitoring_active = {}
seen_orders = {}
active_orders = {}

# --- PERSISTENCE ---
def load_data():
    global user_sessions, monitoring_active
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as f:
                data = json.load(f)
            for k, v in data.items():
                cid = int(k)
                user_sessions[cid] = {"api_key": v["api_key"], "api_secret": v["api_secret"]}
                monitoring_active[cid] = v.get("monitoring_active", True)
            logger.info(f"✅ Loaded {len(user_sessions)} sessions.")
        except Exception as e: logger.error(f"Load error: {e}")

def save_data():
    try:
        data = {str(k): {**v, "monitoring_active": monitoring_active.get(k, True)} 
                for k, v in user_sessions.items()}
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e: logger.error(f"Save error: {e}")

# --- ASYNC BYBIT CLIENT ---
async def bybit_request(api_key, api_secret, method, endpoint, params=None, body=None):
    """Force all Bybit calls through IPRoyal Proxy using Non-blocking HTTPX."""
    ts = str(int(time.time() * 1000))
    recv_window = "10000"
    
    # Prepare Signature
    if method == "GET":
        qs = "&".join([f"{k}={v}" for k, v in sorted((params or {}).items())])
        payload = ts + api_key + recv_window + qs
    else:
        # Crucial: No spaces in JSON for Bybit signatures
        payload = ts + api_key + recv_window + json.dumps(body or {}, separators=(',', ':'))
    
    signature = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json"
    }

    # Proxy Configuration
    mounts = {"all://": httpx.HTTPProxy(PROXY_URL)} if PROXY_URL else None
    
    async with httpx.AsyncClient(mounts=mounts, timeout=15.0) as client:
        try:
            url = f"https://api.bybit.com{endpoint}"
            if method == "GET":
                r = await client.get(url, headers=headers, params=params)
            else:
                r = await client.post(url, headers=headers, content=json.dumps(body or {}, separators=(',', ':')))
            return r.json()
        except Exception as e:
            logger.error(f"Proxy/API Error on {endpoint}: {e}")
            return None

# --- P2P PROCESSING ---
async def handle_order(app, cid, ak, sk, order):
    oid = order.get("id")
    if not oid or oid in seen_orders.get(cid, set()): return
    
    seen_orders.setdefault(cid, set()).add(oid)
    logger.info(f"New Order Detected: {oid}")

    # 1. Fetch Order Details (Bank info)
    detail = await bybit_request(ak, sk, "POST", "/v5/p2p/order/info", body={"orderId": str(oid)})
    if not detail or detail.get("retCode") != 0: return
    
    res = detail["result"]
    amount, currency, seller = res["amount"], res["currencyId"], res["targetNickName"]
    
    # Extract Payment IDs
    p_id, p_type, bank_str = None, None, ""
    for term in res.get("paymentTermList", []):
        if term.get("online") == "1": continue
        p_id, p_type = term.get("id"), term.get("paymentType")
        bank_str = f"👤 `{term.get('realName')}`\n🏦 `{term.get('bankName')}`\n💳 `{term.get('accountNo')}`"
        break

    # 2. Action: Click 'Paid'
    action_log = "⚠️ Manual Click Needed"
    if p_id and p_type:
        paid_res = await bybit_request(ak, sk, "POST", "/v5/p2p/order/pay", 
                                      body={"orderId": str(oid), "paymentType": str(p_type), "paymentId": str(p_id)})
        if paid_res and paid_res.get("retCode") == 0: action_log = "✅ Auto-marked as Paid"

    # 3. Action: Send Chat Message
    msg = f"Paid {amount} {currency}. Please release USDT. Thanks!"
    await bybit_request(ak, sk, "POST", "/v5/p2p/order/message/send", 
                       body={"orderId": str(oid), "message": msg, "contentType": "str", "msgUuid": uuid.uuid4().hex})

    # 4. Telegram Alert
    text = (f"🚨 *NEW P2P TRADE*\n━━━━━━━━━━━━━\n📦 ID: `{oid}`\n👤 Seller: {seller}\n"
            f"💰 Send: *{amount} {currency}*\n━━━━━━━━━━━━━\n🏦 *BANK DETAILS:*\n{bank_str}\n"
            f"━━━━━━━━━━━━━\n🤖 Bot: {action_log}\n💬 Chat message sent.")
    await app.bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
    active_orders.setdefault(cid, {})[oid] = {"status": 10}

# --- LOOPS ---
async def monitor_loop(app):
    while True:
        for cid, session in list(user_sessions.items()):
            if not monitoring_active.get(cid): continue
            ak, sk = session["api_key"], session["api_secret"]
            
            # Poll Pending Orders
            resp = await bybit_request(ak, sk, "POST", "/v5/p2p/order/pending/simplifyList", body={"status": 10})
            if resp and resp.get("retCode") == 0:
                for order in resp.get("result", {}).get("items", []):
                    await handle_order(app, cid, ak, sk, order)
        await asyncio.sleep(POLL_INTERVAL)

# --- TELEGRAM INTERFACE ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *Bybit P2P Automation Active.*\nUse /login to setup keys.", parse_mode="Markdown")
    return ASK_API_KEY

async def handle_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tmp_ak"] = update.message.text.strip()
    await update.message.reply_text("🔐 Now send your *API Secret*:", parse_mode="Markdown")
    return ASK_SECRET_KEY

async def handle_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid, ak, sk = update.effective_chat.id, context.user_data["tmp_ak"], update.message.text.strip()
    
    # Test Connection via IPRoyal Proxy
    test = await bybit_request(ak, sk, "GET", "/v5/user/query-api")
    if test and test.get("retCode") == 0:
        user_sessions[cid] = {"api_key": ak, "api_secret": sk}
        monitoring_active[cid] = True
        save_data()
        await update.message.reply_text("✅ *Connection Verified!*\nMonitoring is now LIVE.")
    else:
        await update.message.reply_text("❌ *Auth Failed.* Check Proxy or API whitelist.")
    return ConversationHandler.END

# --- ENTRY POINT ---
def main():
    load_data()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("login", start_cmd)],
        states={
            ASK_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_key)],
            ASK_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_secret)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    
    app.add_handler(conv)
    
    loop = asyncio.get_event_loop()
    loop.create_task(monitor_loop(app))
    
    logger.info("Bot is starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
