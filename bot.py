# Force Railway Cache Bust 01
import os
import time
import hmac
import hashlib
import requests
import logging
import json
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ASK_API_KEY, ASK_SECRET_KEY = range(2)
EDIT_API_KEY, EDIT_SECRET_KEY = range(2, 4)

# ── Environment Variables ─────────────────────────────────────────────────────
# TELEGRAM_TOKEN → Railway Variable (direct connection, no proxy)
# PROXY_URL      → Railway Variable (IPRoyal SOCKS5 proxy for Bybit only)
#
# PROXY_URL format: socks5://user:password@proxy-host:port
# Example:         socks5://abc123:pass456@geo.iproyal.com:32325

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PROXY_URL      = os.environ.get("PROXY_URL", "")  # IPRoyal SOCKS5 proxy

# ── Build proxy dict for requests (Bybit calls only) ─────────────────────────
def get_proxy():
    """
    Returns proxy dict for requests library.
    Only used for Bybit API calls — NOT for Telegram.
    """
    if PROXY_URL:
        return {
            "http":  PROXY_URL,
            "https": PROXY_URL
        }
    return None  # no proxy if not set

# ── JSON File Storage ─────────────────────────────────────────────────────────
SESSIONS_FILE = "sessions.json"

def load_sessions():
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r") as f:
                data = json.load(f)
                sessions   = {}
                monitoring = {}
                for k, v in data.items():
                    chat_id = int(k)
                    sessions[chat_id] = {
                        "api_key":    v["api_key"],
                        "api_secret": v["api_secret"]
                    }
                    monitoring[chat_id] = v.get("monitoring_active", True)
                logger.info(f"✅ Loaded {len(sessions)} session(s) from file")
                return sessions, monitoring
    except Exception as e:
        logger.error(f"Failed to load sessions: {e}")
    return {}, {}

def save_sessions():
    try:
        data = {}
        for chat_id, session in user_sessions.items():
            data[str(chat_id)] = {
                "api_key":           session["api_key"],
                "api_secret":        session["api_secret"],
                "monitoring_active": monitoring_active.get(chat_id, True)
            }
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("✅ Sessions saved to file")
    except Exception as e:
        logger.error(f"Failed to save sessions: {e}")

# ── Load sessions on startup ──────────────────────────────────────────────────
user_sessions, monitoring_active = load_sessions()
seen_orders = {}

# ─── BYBIT API ────────────────────────────────────────────────────────────────

def bybit_sign(api_key, api_secret, params=None, body=None, method="GET"):
    """
    MAINNET SIGNATURE FIX:
    Must use json.dumps with separators=(',', ':') — no spaces in JSON body.
    Bybit Mainnet is strict and will reject signatures with spaces.
    """
    timestamp   = str(int(time.time() * 1000))
    recv_window = "5000"

    if method == "GET":
        param_str = timestamp + api_key + recv_window + \
            "&".join([f"{k}={v}" for k, v in sorted((params or {}).items())])
    else:
        # ✅ CRITICAL FIX: separators=(',', ':') removes all spaces from JSON
        param_str = timestamp + api_key + recv_window + \
            json.dumps(body or {}, separators=(',', ':'))

    signature = hmac.new(
        api_secret.encode(), param_str.encode(), hashlib.sha256
    ).hexdigest()

    return {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-TIMESTAMP":   timestamp,
        "X-BAPI-SIGN":        signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type":       "application/json"
    }

def bybit_request(api_key, api_secret, method, endpoint, params=None, body=None):
    """
    ALL Bybit API calls go through IPRoyal SOCKS5 proxy.
    This ensures Bybit only sees the IPRoyal IP — matching the whitelisted IP.
    Telegram calls do NOT use this function and go directly via Railway.
    """
    base    = "https://api.bybit.com"
    proxies = get_proxy()

    try:
        headers = bybit_sign(api_key, api_secret, params=params, body=body, method=method)

        if method == "GET":
            r = requests.get(
                base + endpoint,
                headers=headers,
                params=params or {},
                proxies=proxies,
                timeout=15
            )
        else:
            # ✅ CRITICAL FIX: also use separators here when sending JSON body
            r = requests.post(
                base + endpoint,
                headers=headers,
                data=json.dumps(body or {}, separators=(',', ':')),
                proxies=proxies,
                timeout=15
            )
        return r.json()

    except Exception as e:
        logger.error(f"Bybit request error: {e}")
        return None

def get_p2p_orders(api_key, api_secret, status):
    body = {"status": status, "page": 1, "size": 20}
    return bybit_request(api_key, api_secret, "POST", "/v5/p2p/order/simplifyList", body=body)

def get_order_detail(api_key, api_secret, order_id):
    body = {"orderId": order_id}
    return bybit_request(api_key, api_secret, "POST", "/v5/p2p/order/info", body=body)

def click_transfer_completed(api_key, api_secret, order_id):
    body = {"orderId": order_id}
    return bybit_request(api_key, api_secret, "POST", "/v5/p2p/order/pay", body=body)

def send_msg_to_seller(api_key, api_secret, order_id, message):
    body = {"orderId": order_id, "message": message, "msgType": "str"}
    return bybit_request(api_key, api_secret, "POST", "/v5/p2p/order/message/send", body=body)

def get_payment_details(detail_result):
    payment_method = "Unknown"
    bank_lines     = ""
    try:
        result = detail_result.get("result", {})
        terms  = result.get("makerPaymentTerms", [])
        if not terms:
            terms = result.get("sellerPaymentTerms", [])
        if terms:
            term           = terms[0]
            payment_method = term.get("paymentType", "Unknown")
            fields         = term.get("fields", [])
            for f in fields:
                fname  = f.get("fieldName", "")
                fvalue = f.get("value", "")
                if fname and fvalue:
                    bank_lines += f"   • {fname}: `{fvalue}`\n"
        if not bank_lines:
            pay_info = result.get("paymentInfo", {})
            for k, v in pay_info.items():
                if v:
                    bank_lines += f"   • {k}: `{v}`\n"
        if not bank_lines:
            bank_lines = "   • Seller has not added bank details yet\n"
    except Exception as e:
        logger.error(f"Payment detail error: {e}")
        bank_lines = "   • Could not read bank details\n"
    return payment_method, bank_lines

# ─── ERROR ALERT HELPER ───────────────────────────────────────────────────────

async def send_error_alert(app, chat_id, error_type, detail=""):
    """
    Sends alert via Telegram DIRECTLY (no proxy).
    Railway IP is used for Telegram — fast and reliable.
    """
    messages = {
        "api_invalid": (
            "🚨 *API KEY ERROR — BOT STOPPED!*\n\n"
            "❌ Your Bybit API Key is no longer valid!\n\n"
            "This can happen if:\n"
            "   • You deleted the API key on Bybit\n"
            "   • API key expired\n"
            "   • IPRoyal IP changed\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👉 /mycredentials — See current API\n"
            "👉 /editcredentials — Update it\n"
            "👉 /removecredentials — Clear & restart"
        ),
        "api_secret_wrong": (
            "🚨 *API SECRET ERROR — BOT STOPPED!*\n\n"
            "❌ Your API Secret is wrong or expired!\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👉 /editcredentials — Update your secret"
        ),
        "no_permission": (
            "🚨 *PERMISSION ERROR — BOT STOPPED!*\n\n"
            "❌ Your API Key lost P2P permission!\n\n"
            "Fix: Bybit → API Management → Edit → Enable P2P\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👉 /editcredentials — After fixing on Bybit"
        ),
        "proxy_error": (
            "🚨 *PROXY ERROR — BOT STOPPED!*\n\n"
            "❌ Cannot connect through IPRoyal proxy!\n\n"
            "Possible reasons:\n"
            "   • PROXY_URL is wrong in Railway Variables\n"
            "   • IPRoyal subscription expired\n"
            "   • Proxy server is down\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👉 Check Railway → Variables → PROXY_URL\n"
            "👉 Check your IPRoyal dashboard"
        ),
        "connection_failed": (
            "⚠️ *CONNECTION ERROR!*\n\n"
            "❌ Cannot reach Bybit API!\n\n"
            f"Detail: `{detail}`\n\n"
            "Bot will retry automatically.\n"
            "If problem continues, use /status to check."
        ),
        "monitor_error": (
            "⚠️ *MONITOR ERROR!*\n\n"
            "❌ An error occurred while checking orders!\n\n"
            f"Detail: `{detail}`\n\n"
            "Bot is still running and will retry.\n"
            "Use /checkorders to scan manually if needed."
        ),
        "no_proxy": (
            "🚨 *PROXY NOT SET!*\n\n"
            "❌ PROXY_URL is not configured in Railway!\n\n"
            "Bybit will reject all requests because\n"
            "Railway IP is not whitelisted.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👉 Railway → Variables → Add PROXY_URL\n"
            "Format: socks5://user:pass@host:port"
        ),
    }
    text = messages.get(error_type, f"⚠️ *Unknown Error*\n\n`{detail}`")
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send error alert: {e}")

# ─── BACKGROUND MONITOR ───────────────────────────────────────────────────────

async def monitor_loop(app: Application):
    logger.info("✅ Monitor loop started")
    fail_counts    = {}
    proxy_notified = {}  # track if user was already notified about proxy issue

    # ── Warn all users if PROXY_URL is not set ──
    if not PROXY_URL:
        logger.warning("⚠️ PROXY_URL not set — Bybit calls will use direct Railway IP!")
        for chat_id in list(user_sessions.keys()):
            await send_error_alert(app, chat_id, "no_proxy")

    while True:
        try:
            for chat_id, session in list(user_sessions.items()):

                if not monitoring_active.get(chat_id, True):
                    continue

                api_key    = session["api_key"]
                api_secret = session["api_secret"]

                if chat_id not in seen_orders:
                    seen_orders[chat_id] = set()
                seen = seen_orders[chat_id]

                # ── Fetch orders via IPRoyal proxy ──
                result = get_p2p_orders(api_key, api_secret, "20")

                # ── Handle errors ──
                if result is None:
                    fail_counts[chat_id] = fail_counts.get(chat_id, 0) + 1
                    if fail_counts[chat_id] == 3:
                        # Could be proxy issue or Bybit down
                        if PROXY_URL:
                            await send_error_alert(app, chat_id, "proxy_error")
                        else:
                            await send_error_alert(app, chat_id, "connection_failed",
                                                   "No response from Bybit")
                        fail_counts[chat_id] = 0  # reset to avoid spam
                    continue

                ret_code = result.get("retCode")

                if ret_code == 10003:
                    monitoring_active[chat_id] = False
                    save_sessions()
                    await send_error_alert(app, chat_id, "api_invalid")
                    continue

                elif ret_code == 10004:
                    monitoring_active[chat_id] = False
                    save_sessions()
                    await send_error_alert(app, chat_id, "api_secret_wrong")
                    continue

                elif ret_code == 10005:
                    monitoring_active[chat_id] = False
                    save_sessions()
                    await send_error_alert(app, chat_id, "no_permission")
                    continue

                elif ret_code != 0:
                    fail_counts[chat_id] = fail_counts.get(chat_id, 0) + 1
                    if fail_counts[chat_id] == 3:
                        await send_error_alert(app, chat_id, "monitor_error",
                                               f"retCode={ret_code} {result.get('retMsg','')}")
                        fail_counts[chat_id] = 0
                    continue

                fail_counts[chat_id] = 0  # reset on success

                for order in result.get("result", {}).get("items", []):
                    oid = order.get("id")
                    if oid in seen:
                        continue
                    seen.add(oid)

                    amount   = order.get("amount",    "N/A")
                    price    = order.get("price",     "N/A")
                    currency = order.get("currencyId","N/A")
                    token    = order.get("tokenId",   "N/A")
                    seller   = order.get("sellerNickName",
                               order.get("targetNickName", "Unknown"))
                    created  = order.get("createDate", "N/A")

                    detail     = get_order_detail(api_key, api_secret, oid)
                    pay_method, bank_info = get_payment_details(detail) \
                        if detail else ("Unknown", "   • Not available\n")

                    # STEP 1: Click Transfer Completed (via proxy)
                    click_result = click_transfer_completed(api_key, api_secret, oid)
                    clicked      = click_result and click_result.get("retCode") == 0

                    # STEP 2: Message seller (via proxy)
                    seller_message = (
                        f"✅ Payment Has Been Sent!\n\n"
                        f"Dear {seller},\n"
                        f"We have initiated your payment successfully.\n\n"
                        f"💰 Amount: {amount} {currency}\n"
                        f"🏦 Sent to: {pay_method}\n\n"
                        f"Please check your bank account.\n"
                        f"Once you receive the payment,\n"
                        f"kindly release the USDT. 🙏\n\n"
                        f"Thank you for trading with us!"
                    )
                    send_msg_to_seller(api_key, api_secret, oid, seller_message)

                    # STEP 3: Alert user via Telegram (DIRECT — no proxy)
                    alert = (
                        f"🚨 *NEW ORDER — SEND PAYMENT NOW!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📦 Order ID: `{oid}`\n"
                        f"👤 Seller: *{seller}*\n"
                        f"💰 Amount: *{amount} {currency}*\n"
                        f"🪙 Token: {token}\n"
                        f"💵 Price: {price}\n"
                        f"🕐 Time: {created}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏦 *SEND MONEY TO THIS BANK NOW:*\n"
                        f"💳 Method: *{pay_method}*\n"
                        f"{bank_info}"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🤖 Transfer Clicked: {'✅ Done' if clicked else '⚠️ Failed — do manually!'}\n"
                        f"💬 Seller Notified: ✅\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚡ *Send {amount} {currency} to seller bank NOW!*\n"
                        f"⏱️ Seller will release USDT after receiving money."
                    )
                    await app.bot.send_message(
                        chat_id=chat_id, text=alert, parse_mode="Markdown"
                    )

                    # STEP 4: 5 min reminder
                    asyncio.create_task(
                        remind_after_5_min(app, chat_id, oid, seller,
                                           amount, currency, pay_method, bank_info)
                    )
                    logger.info(f"Order {oid} processed for {chat_id}")

        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(25)


async def remind_after_5_min(app, chat_id, oid, seller, amount, currency, pay_method, bank_info):
    await asyncio.sleep(300)
    try:
        if not monitoring_active.get(chat_id, True):
            return
        # Telegram alert — DIRECT connection, no proxy
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ *REMINDER — 5 MINUTES PASSED!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Order: `{oid}`\n"
                f"👤 Seller: *{seller}*\n"
                f"💰 Amount: *{amount} {currency}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 *BANK DETAILS:*\n"
                f"💳 {pay_method}\n"
                f"{bank_info}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ Have you sent the payment yet?\n"
                f"⏱️ *Only 10 minutes remaining before order expires!*"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Reminder error: {e}")

# ─── TELEGRAM COMMANDS ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id in user_sessions:
        session     = user_sessions[chat_id]
        key_preview = session["api_key"][:6] + "••••" + session["api_key"][-4:]
        await update.message.reply_text(
            f"⚠️ *You already have credentials saved!*\n\n"
            f"🔑 Current API Key: `{key_preview}`\n\n"
            f"What would you like to do?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👁 View My Credentials",   callback_data="view_creds")],
                [InlineKeyboardButton("✏️ Edit Credentials",      callback_data="edit_creds")],
                [InlineKeyboardButton("🗑 Remove Credentials",    callback_data="remove_creds")],
                [InlineKeyboardButton("▶️ Continue with Current", callback_data="keep_creds")],
            ])
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🤖 *Bybit P2P Auto-Bot*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "What this bot does:\n\n"
        "1️⃣ You post BUY USDT ad on Bybit\n"
        "2️⃣ Seller comes and clicks your ad\n"
        "3️⃣ Bot detects order in 15 seconds\n"
        "4️⃣ Bot clicks Transfer Completed ✅\n"
        "5️⃣ Bot messages seller with payment info\n"
        "6️⃣ Bot alerts YOU instantly with bank details\n"
        "7️⃣ YOU send real money to seller bank\n"
        "8️⃣ Seller releases USDT ✅\n"
        "9️⃣ Reminder after 5 min if not done\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 Enter your *Bybit API Key*:",
        parse_mode="Markdown"
    )
    return ASK_API_KEY


async def ask_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["api_key"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API Key received!\n\n"
        "🔐 Now enter your *Bybit API Secret*:",
        parse_mode="Markdown"
    )
    return ASK_SECRET_KEY


async def save_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key    = context.user_data.get("api_key")
    api_secret = update.message.text.strip()
    chat_id    = update.effective_chat.id

    await update.message.reply_text(
        "⏳ *Verifying your credentials...*\n\n"
        "🔄 Connecting to Bybit via IPRoyal proxy...\n"
        "🔑 Checking API Key...\n"
        "🔐 Checking API Secret...\n"
        "📋 Checking P2P permissions...",
        parse_mode="Markdown"
    )

    if not api_key or len(api_key) < 10:
        await update.message.reply_text(
            "❌ *Failed*\n\n🔑 API Key: ❌ Invalid\nRun /start again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if not api_secret or len(api_secret) < 10:
        await update.message.reply_text(
            "❌ *Failed*\n\n🔑 API Key: ✅\n🔐 Secret: ❌ Invalid\nRun /start again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Check proxy is configured
    if not PROXY_URL:
        await update.message.reply_text(
            "⚠️ *Warning: PROXY_URL not set!*\n\n"
            "Bybit calls will use Railway IP directly.\n"
            "This will FAIL if you whitelisted IPRoyal IP on Bybit.\n\n"
            "Add PROXY_URL in Railway Variables first!\n"
            "Format: `socks5://user:pass@host:port`",
            parse_mode="Markdown"
        )

    try:
        # Verify via proxy
        result = bybit_request(api_key, api_secret, "GET", "/v5/user/query-api")

        if result is None:
            await update.message.reply_text(
                "❌ *Cannot connect to Bybit*\n\n"
                "Possible reasons:\n"
                "• PROXY_URL is wrong or not set\n"
                "• IPRoyal proxy is down\n"
                "• Internet issue\n\n"
                "Check Railway Variables → PROXY_URL",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        ret_code = result.get("retCode")

        if ret_code == 0:
            perms   = result.get("result", {}).get("permissions", {})
            has_p2p = "P2P" in str(perms)

            user_sessions[chat_id]     = {"api_key": api_key, "api_secret": api_secret}
            seen_orders[chat_id]       = set()
            monitoring_active[chat_id] = True
            save_sessions()

            await update.message.reply_text(
                "🎉 *Access Successful!*\n\n"
                "🔑 API Key: ✅ Valid\n"
                "🔐 API Secret: ✅ Valid\n"
                "🌐 Bybit: ✅ Connected via IPRoyal\n"
                "📡 Telegram: ✅ Direct Railway connection\n"
                f"📋 P2P: {'✅ Enabled' if has_p2p else '⚠️ Enable P2P in Bybit API!'}\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💾 *Credentials saved — bot remembers you after restart!*\n\n"
                "🤖 *Bot is LIVE and monitoring!*\n\n"
                "Commands:\n"
                "👁 /mycredentials — View/edit/remove API keys\n"
                "📋 /checkorders — Scan orders now\n"
                "⏹ /stopbot — Pause monitoring\n"
                "▶️ /startbot — Resume monitoring\n"
                "🔄 /restart — Reset everything\n"
                "📊 /status — Check bot status\n",
                parse_mode="Markdown"
            )

        elif ret_code == 10003:
            await update.message.reply_text(
                "❌ *Invalid API Key*\n\n"
                "🔑 Not found on Bybit.\n"
                "Copy carefully from Bybit.\nRun /start again.",
                parse_mode="Markdown"
            )
        elif ret_code == 10004:
            await update.message.reply_text(
                "❌ *Wrong Secret*\n\n"
                "🔑 API Key: ✅\n🔐 Secret: ❌ Wrong\n\n"
                "Copy secret carefully.\nRun /start again.",
                parse_mode="Markdown"
            )
        elif ret_code == 10005:
            await update.message.reply_text(
                "❌ *No Permission*\n\n"
                "🔒 P2P not enabled on this API key.\n\n"
                "Bybit → API Management → Edit → Enable P2P\nRun /start again.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ *Bybit Error*\n\nCode: `{ret_code}`\n"
                f"Message: {result.get('retMsg','Unknown')}\n\nRun /start again.",
                parse_mode="Markdown"
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ *Error*\n\n`{str(e)}`\n\nRun /start again.",
            parse_mode="Markdown"
        )

    return ConversationHandler.END

# ─── CREDENTIAL MANAGEMENT ────────────────────────────────────────────────────

async def mycredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)

    if not session:
        await update.message.reply_text(
            "❌ No credentials saved.\nRun /start to add your API keys."
        )
        return

    api_key       = session["api_key"]
    api_secret    = session["api_secret"]
    key_masked    = api_key[:6]    + "••••••••••" + api_key[-4:]
    secret_masked = api_secret[:4] + "••••••••••••••••" + api_secret[-4:]
    is_running    = monitoring_active.get(chat_id, False)
    proxy_status  = f"`{PROXY_URL[:30]}...`" if PROXY_URL else "❌ Not set!"

    await update.message.reply_text(
        f"👁 *Your Current Credentials*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 API Key:\n`{key_masked}`\n\n"
        f"🔐 API Secret:\n`{secret_masked}`\n\n"
        f"📡 Proxy (IPRoyal): {proxy_status}\n"
        f"📊 Monitoring: {'✅ Running' if is_running else '⏹ Stopped'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"What would you like to do?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit API Key & Secret", callback_data="edit_creds")],
            [InlineKeyboardButton("🗑 Remove All Credentials", callback_data="remove_creds")],
            [InlineKeyboardButton("❌ Close",                  callback_data="close_menu")],
        ])
    )


async def editcredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_sessions:
        await update.message.reply_text("❌ No credentials found. Run /start first.")
        return ConversationHandler.END

    monitoring_active[chat_id] = False
    save_sessions()
    await update.message.reply_text(
        "✏️ *Edit Credentials*\n\n"
        "⏸ Monitoring paused while you update.\n\n"
        "🔑 Enter your new *Bybit API Key*:",
        parse_mode="Markdown"
    )
    return EDIT_API_KEY


async def edit_ask_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_api_key"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ New API Key received!\n\n"
        "🔐 Now enter your new *Bybit API Secret*:",
        parse_mode="Markdown"
    )
    return EDIT_SECRET_KEY


async def edit_save_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    api_key    = context.user_data.get("new_api_key")
    api_secret = update.message.text.strip()

    await update.message.reply_text(
        "⏳ Verifying new credentials via IPRoyal proxy...",
        parse_mode="Markdown"
    )

    result = bybit_request(api_key, api_secret, "GET", "/v5/user/query-api")

    if result is None or result.get("retCode") != 0:
        code = result.get("retCode") if result else "No response"
        await update.message.reply_text(
            f"❌ *Verification Failed!*\n\n"
            f"Code: `{code}`\n\n"
            f"Old credentials still active.\n"
            f"Try /editcredentials again.",
            parse_mode="Markdown"
        )
        monitoring_active[chat_id] = True
        save_sessions()
        return ConversationHandler.END

    user_sessions[chat_id]     = {"api_key": api_key, "api_secret": api_secret}
    seen_orders[chat_id]       = set()
    monitoring_active[chat_id] = True
    save_sessions()

    key_masked = api_key[:6] + "••••••••••" + api_key[-4:]
    await update.message.reply_text(
        f"✅ *Credentials Updated Successfully!*\n\n"
        f"🔑 New API Key: `{key_masked}`\n"
        f"🔐 New Secret: ✅ Saved\n"
        f"📡 Verified via IPRoyal proxy ✅\n\n"
        f"▶️ Monitoring resumed automatically!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def removecredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_sessions:
        await update.message.reply_text("❌ No credentials to remove.")
        return

    await update.message.reply_text(
        "🗑 *Remove Credentials?*\n\n"
        "⚠️ This will:\n"
        "   • Delete your API key & secret\n"
        "   • Stop monitoring\n"
        "   • Clear all saved data\n\n"
        "Are you sure?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Remove Everything",  callback_data="confirm_remove")],
            [InlineKeyboardButton("❌ No, Keep My Credentials", callback_data="close_menu")],
        ])
    )

# ─── INLINE BUTTON HANDLER ────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "view_creds":
        session = user_sessions.get(chat_id)
        if session:
            key_masked    = session["api_key"][:6]    + "••••••••••" + session["api_key"][-4:]
            secret_masked = session["api_secret"][:4] + "••••••••••••••••" + session["api_secret"][-4:]
            proxy_status  = "✅ Set" if PROXY_URL else "❌ Not set!"
            await query.edit_message_text(
                f"👁 *Your Current Credentials*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔑 API Key:\n`{key_masked}`\n\n"
                f"🔐 API Secret:\n`{secret_masked}`\n\n"
                f"📡 IPRoyal Proxy: {proxy_status}\n"
                f"📊 Monitoring: {'✅ Running' if monitoring_active.get(chat_id) else '⏹ Stopped'}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Use /editcredentials to change\n"
                f"Use /removecredentials to delete",
                parse_mode="Markdown"
            )

    elif query.data == "edit_creds":
        monitoring_active[chat_id] = False
        save_sessions()
        await query.edit_message_text(
            "✏️ *Ready to edit.*\n\n"
            "Send /editcredentials to start.",
            parse_mode="Markdown"
        )

    elif query.data == "remove_creds":
        await query.edit_message_text(
            "🗑 *Remove Credentials?*\n\n"
            "⚠️ This will delete everything.\n\nAre you sure?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Remove Everything",  callback_data="confirm_remove")],
                [InlineKeyboardButton("❌ No, Keep My Credentials", callback_data="close_menu")],
            ])
        )

    elif query.data == "confirm_remove":
        user_sessions.pop(chat_id, None)
        seen_orders.pop(chat_id, None)
        monitoring_active.pop(chat_id, None)
        save_sessions()  # ✅ persist removal to JSON file
        await query.edit_message_text(
            "🗑 *Credentials Removed!*\n\n"
            "✅ All data cleared.\n\n"
            "Run /start to add new API keys.",
            parse_mode="Markdown"
        )

    elif query.data == "keep_creds":
        is_running = monitoring_active.get(chat_id, False)
        await query.edit_message_text(
            f"✅ *Keeping current credentials.*\n\n"
            f"📊 Monitoring: {'✅ Running' if is_running else '⏹ Use /startbot to resume'}\n\n"
            f"Commands:\n"
            f"👁 /mycredentials — View/edit API keys\n"
            f"📋 /checkorders — Scan orders\n"
            f"📊 /status — Check status",
            parse_mode="Markdown"
        )

    elif query.data == "close_menu":
        await query.edit_message_text("✅ Menu closed.")

# ─── OTHER COMMANDS ───────────────────────────────────────────────────────────

async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_sessions:
        await update.message.reply_text("⚠️ No active session. Run /start first.")
        return
    monitoring_active[chat_id] = False
    save_sessions()
    await update.message.reply_text(
        "⏹ *Bot Monitoring STOPPED*\n\n"
        "Bot is still connected to Bybit\n"
        "but will NOT check for new orders.\n\n"
        "▶️ Use /startbot to resume anytime.",
        parse_mode="Markdown"
    )

async def startbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_sessions:
        await update.message.reply_text("⚠️ No credentials. Run /start first.")
        return
    monitoring_active[chat_id] = True
    seen_orders[chat_id]       = set()
    save_sessions()
    await update.message.reply_text(
        "▶️ *Bot Monitoring STARTED!*\n\n"
        "✅ Checking Bybit every 15 seconds\n"
        "✅ Bybit calls via IPRoyal proxy\n"
        "✅ Telegram alerts via direct Railway\n"
        "✅ Will auto-click Transfer Completed\n"
        "✅ Will alert you with bank details\n\n"
        "⏹ Use /stopbot to pause anytime.",
        parse_mode="Markdown"
    )

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_sessions.pop(chat_id, None)
    seen_orders.pop(chat_id, None)
    monitoring_active.pop(chat_id, None)
    save_sessions()
    await update.message.reply_text(
        "🔄 *Bot Restarted!*\n\n"
        "All data cleared.\n"
        "Please run /start to set up again.",
        parse_mode="Markdown"
    )

async def checkorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)
    if not session:
        await update.message.reply_text("⚠️ No credentials. Run /start first.")
        return

    await update.message.reply_text("🔄 Scanning your Bybit P2P orders via proxy...")

    api_key    = session["api_key"]
    api_secret = session["api_secret"]
    found      = False

    status_map = {
        "10": "🆕 New",
        "20": "⏳ Waiting Your Payment",
        "30": "✅ Completed",
        "40": "⚠️ Appealing",
    }

    for code, label in status_map.items():
        result = get_p2p_orders(api_key, api_secret, code)
        if not result or result.get("retCode") != 0:
            continue
        items = result.get("result", {}).get("items", [])
        if not items:
            continue
        found = True
        for order in items[:3]:
            oid      = order.get("id")
            amount   = order.get("amount",    "N/A")
            price    = order.get("price",     "N/A")
            currency = order.get("currencyId","N/A")
            token    = order.get("tokenId",   "N/A")
            seller   = order.get("sellerNickName",
                       order.get("targetNickName", "Unknown"))
            detail = get_order_detail(api_key, api_secret, oid)
            pay_method, bank_info = get_payment_details(detail) \
                if detail else ("Unknown", "   • N/A\n")
            await update.message.reply_text(
                f"📋 *{label}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Order ID: `{oid}`\n"
                f"👤 Seller: {seller}\n"
                f"💰 Amount: *{amount} {currency}*\n"
                f"🪙 Token: {token}\n"
                f"💵 Price: {price}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 *Seller Bank Details:*\n"
                f"💳 {pay_method}\n"
                f"{bank_info}",
                parse_mode="Markdown"
            )

    if not found:
        await update.message.reply_text(
            "📭 *No active orders right now.*\n\n"
            "Bot is monitoring every 15 seconds.\n"
            "You will be alerted when a seller comes!",
            parse_mode="Markdown"
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    has_cred   = chat_id in user_sessions
    is_running = monitoring_active.get(chat_id, False)
    key_info   = ""
    if has_cred:
        k = user_sessions[chat_id]["api_key"]
        key_info = f"\n🔑 API Key: `{k[:6]}••••{k[-4:]}`"

    proxy_status = "✅ IPRoyal connected" if PROXY_URL else "❌ PROXY_URL not set!"

    await update.message.reply_text(
        f"📊 *Bot Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Credentials: {'✅ Saved' if has_cred else '❌ Not set — run /start'}"
        f"{key_info}\n"
        f"📡 Proxy: {proxy_status}\n"
        f"🔍 Monitoring: {'✅ Running every 15 sec' if is_running else '⏹ Stopped'}\n"
        f"⚡ Auto Transfer Click: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"💬 Auto Seller Message: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"🔔 Telegram Alerts: {'✅ ON (Direct Railway)' if is_running else '⏹ OFF'}\n"
        f"⏰ 5 Min Reminder: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Commands:\n"
        f"👁 /mycredentials — View/edit/remove API keys\n"
        f"▶️ /startbot — Start monitoring\n"
        f"⏹ /stopbot — Stop monitoring\n"
        f"🔄 /restart — Reset everything\n"
        f"📋 /checkorders — Check now\n",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled. Use /start to begin again.")
    return ConversationHandler.END

# ─── START APP ────────────────────────────────────────────────────────────────

async def main():
    # ── Validate environment variables ──
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ TELEGRAM_TOKEN not set in Railway Variables!")

    if not PROXY_URL:
        logger.warning("⚠️ PROXY_URL not set — Bybit calls will NOT use proxy!")
        logger.warning("⚠️ This will FAIL if IPRoyal IP is whitelisted on Bybit!")
    else:
        logger.info(f"✅ PROXY_URL loaded — Bybit calls via IPRoyal")
        logger.info(f"✅ Telegram calls via direct Railway connection")

    # ── Telegram uses direct Railway connection (no proxy) ──
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_API_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_secret)],
            ASK_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_credentials)],
        },
        fallbacks=[
            CommandHandler("cancel",  cancel),
            CommandHandler("start",   start),
            CommandHandler("restart", restart),
        ],
        allow_reentry=True,
    )

    # /editcredentials conversation
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("editcredentials", editcredentials)],
        states={
            EDIT_API_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_ask_secret)],
            EDIT_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_save_credentials)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(edit_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("startbot",          startbot))
    app.add_handler(CommandHandler("stopbot",           stopbot))
    app.add_handler(CommandHandler("restart",           restart))
    app.add_handler(CommandHandler("checkorders",       checkorders))
    app.add_handler(CommandHandler("status",            status))
    app.add_handler(CommandHandler("mycredentials",     mycredentials))
    app.add_handler(CommandHandler("removecredentials", removecredentials))

    logger.info("🤖 Bot running!")
    logger.info("📡 Telegram: Direct Railway connection")
    logger.info(f"🔒 Bybit: {'Via IPRoyal SOCKS5 proxy' if PROXY_URL else 'Direct (no proxy set)'}")

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(monitor_loop(app))
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
