import os
import time
import hmac
import hashlib
import requests
import logging
import json
import asyncio
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ASK_API_KEY, ASK_SECRET_KEY = range(2)
EDIT_API_KEY, EDIT_SECRET_KEY = range(2, 4)

# ─────────────────────────────────────────────────────────────────────────────
# ENV — set in Railway Variables
# TELEGRAM_TOKEN  =  bot token from @BotFather  (Telegram direct, NO proxy)
# PROXY_URL       =  socks5://user:pass@host:port  (Bybit calls via IPRoyal)
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PROXY_URL      = os.environ.get("PROXY_URL", "")
POLL_INTERVAL  = 25   # seconds

def get_proxy():
    """Proxy used ONLY for Bybit API calls. Telegram uses direct Railway IP."""
    if PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STORAGE  (sessions.json survives Railway restarts)
# ─────────────────────────────────────────────────────────────────────────────
SESSIONS_FILE = "sessions.json"

def load_sessions():
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r") as f:
                data = json.load(f)
            sessions   = {}
            monitoring = {}
            for k, v in data.items():
                cid = int(k)
                sessions[cid]   = {"api_key": v["api_key"], "api_secret": v["api_secret"]}
                monitoring[cid] = v.get("monitoring_active", True)
            logger.info(f"✅ Loaded {len(sessions)} session(s)")
            return sessions, monitoring
    except Exception as e:
        logger.error(f"Load sessions error: {e}")
    return {}, {}

def save_sessions():
    try:
        data = {}
        for cid, s in user_sessions.items():
            data[str(cid)] = {
                "api_key":           s["api_key"],
                "api_secret":        s["api_secret"],
                "monitoring_active": monitoring_active.get(cid, True)
            }
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save sessions error: {e}")

user_sessions, monitoring_active = load_sessions()

# seen_orders[cid]   = set of order IDs already processed
# active_orders[cid] = { order_id: { status, amount, currency, token,
#                                    seller, pay_method, bank_lines } }
seen_orders   = {}
active_orders = {}

# ─────────────────────────────────────────────────────────────────────────────
# BYBIT REQUEST + SIGNATURE
# ─────────────────────────────────────────────────────────────────────────────
def bybit_sign(api_key, api_secret, params=None, body=None, method="GET"):
    """
    Mainnet HMAC-SHA256 signature.
    CRITICAL: json.dumps with separators=(',',':') — NO spaces.
    Bybit Mainnet rejects any signature that has spaces in the JSON body.
    """
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    if method == "GET":
        qs = "&".join([f"{k}={v}" for k, v in sorted((params or {}).items())])
        ps = ts + api_key + rw + qs
    else:
        ps = ts + api_key + rw + json.dumps(body or {}, separators=(',', ':'))
    sig = hmac.new(api_secret.encode(), ps.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": rw,
        "Content-Type":       "application/json"
    }

def bybit_request(api_key, api_secret, method, endpoint, params=None, body=None):
    """
    All Bybit calls route through IPRoyal SOCKS5 proxy.
    Bybit sees IPRoyal IP (whitelisted) not Railway IP.
    Telegram calls are made directly without this function.
    """
    try:
        headers = bybit_sign(api_key, api_secret, params=params, body=body, method=method)
        proxies = get_proxy()
        if method == "GET":
            r = requests.get(
                "https://api.bybit.com" + endpoint,
                headers=headers, params=params or {},
                proxies=proxies, timeout=15
            )
        else:
            r = requests.post(
                "https://api.bybit.com" + endpoint,
                headers=headers,
                data=json.dumps(body or {}, separators=(',', ':')),
                proxies=proxies, timeout=15
            )
        return r.json()
    except Exception as e:
        logger.error(f"Bybit [{endpoint}] error: {e}")
        return None

def get_code(result):
    """P2P API returns ret_code. Standard V5 returns retCode. Check both."""
    if result is None:
        return None
    return result.get("ret_code", result.get("retCode"))

def get_msg(result):
    if result is None:
        return ""
    return result.get("ret_msg", result.get("retMsg", ""))

# ─────────────────────────────────────────────────────────────────────────────
# BYBIT P2P API CALLS
# ─────────────────────────────────────────────────────────────────────────────

def api_pending_orders(ak, sk):
    """
    GET PENDING ORDERS
    Endpoint: /v5/p2p/order/pending/simplifyList
    Status 10 = waiting for buyer to pay = bot must act NOW
    This is the dedicated pending endpoint — not the history endpoint.
    """
    return bybit_request(ak, sk, "POST",
        "/v5/p2p/order/pending/simplifyList",
        body={"page": 1, "size": 20, "status": 10}
    )

def api_order_detail(ak, sk, order_id):
    """
    GET ORDER DETAIL
    Endpoint: /v5/p2p/order/info
    Returns:
      - paymentTermList → seller bank details + paymentId + paymentType
      - transferLastSeconds → exact seconds left for buyer to pay
      - status → current order status
      - cancelReason → why cancelled if status=40
    """
    return bybit_request(ak, sk, "POST",
        "/v5/p2p/order/info",
        body={"orderId": str(order_id)}
    )

def api_mark_paid(ak, sk, order_id, payment_type, payment_id):
    """
    CLICK 'PAYMENT COMPLETED' BUTTON
    Endpoint: /v5/p2p/order/pay
    REQUIRES all 3 fields:
      orderId     → the order
      paymentType → from order's paymentTermList (NOT from saved payments)
      paymentId   → from order's paymentTermList (NOT from saved payments)
    NOTE: Balance (type 377 / online=1) NOT supported by API — offline only.
    """
    return bybit_request(ak, sk, "POST",
        "/v5/p2p/order/pay",
        body={
            "orderId":     str(order_id),
            "paymentType": str(payment_type),
            "paymentId":   str(payment_id)
        }
    )

def api_send_message(ak, sk, order_id, message):
    """
    SEND CHAT MESSAGE TO SELLER
    Endpoint: /v5/p2p/order/message/send
    REQUIRES:
      contentType → 'str' for text (NOT msgType — that field doesn't exist)
      msgUuid     → unique UUID per message (REQUIRED — missing causes failure)
    """
    return bybit_request(ak, sk, "POST",
        "/v5/p2p/order/message/send",
        body={
            "orderId":     str(order_id),
            "message":     message,
            "contentType": "str",
            "msgUuid":     uuid.uuid4().hex
        }
    )

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def extract_payment(detail_result):
    """
    Extract seller bank details from order detail response.
    Uses paymentTermList from the ORDER ITSELF.
    Returns: (pay_method, bank_lines, payment_id, payment_type, is_online)
    """
    pay_method   = "Unknown"
    bank_lines   = ""
    payment_id   = None
    payment_type = None
    is_online    = False

    try:
        r     = detail_result.get("result", {})
        terms = r.get("paymentTermList", [])

        for term in terms:
            ptype  = str(term.get("paymentType", ""))
            online = str(term.get("online", "0"))

            # Skip Balance (type 377 or online=1)
            # Bybit API does not support marking paid for online orders
            if ptype == "377" or online == "1":
                continue

            pid   = str(term.get("id", ""))
            pname = term.get("paymentConfigVo", {}).get("paymentName", "")
            pay_method   = pname if pname else f"Type {ptype}"
            payment_id   = pid
            payment_type = ptype

            real_name  = term.get("realName",  "")
            bank_name  = term.get("bankName",  "")
            branch     = term.get("branchName","")
            account_no = term.get("accountNo", "")
            mobile     = term.get("mobile",    "")

            if real_name:  bank_lines += f"   👤 Name: `{real_name}`\n"
            if bank_name:  bank_lines += f"   🏦 Bank: `{bank_name}`\n"
            if branch:     bank_lines += f"   🏢 Branch: `{branch}`\n"
            if account_no: bank_lines += f"   💳 Account: `{account_no}`\n"
            if mobile:     bank_lines += f"   📱 Mobile: `{mobile}`\n"
            break

        # If all payment terms were online-only
        if not bank_lines and terms:
            is_online  = True
            t          = terms[0]
            pname      = t.get("paymentConfigVo", {}).get("paymentName", "Unknown")
            pay_method = pname
            bank_lines = "   ⚠️ Online/Balance payment — cannot auto-click!\n"

        if not bank_lines:
            bank_lines = "   ⚠️ No bank details in order\n"

    except Exception as e:
        logger.error(f"extract_payment error: {e}")
        bank_lines = "   ⚠️ Could not read bank details\n"

    return pay_method, bank_lines, payment_id, payment_type, is_online

def format_timer(seconds_str):
    """Convert transferLastSeconds to readable MM:SS format."""
    try:
        total = int(seconds_str)
        mins  = total // 60
        secs  = total % 60
        return f"{mins} min {secs} sec"
    except:
        return "check Bybit"

# ─────────────────────────────────────────────────────────────────────────────
# ERROR ALERTS  (Telegram direct — no proxy)
# ─────────────────────────────────────────────────────────────────────────────
async def send_error_alert(app, cid, etype, detail=""):
    msgs = {
        "api_invalid": (
            "🚨 *API KEY ERROR — BOT STOPPED!*\n\n"
            "❌ Bybit API Key invalid or deleted!\n\n"
            "Possible reasons:\n"
            "   • Key deleted on Bybit\n"
            "   • IPRoyal IP changed — update Bybit whitelist\n\n"
            "👉 /editcredentials — Update key"
        ),
        "api_secret_wrong": (
            "🚨 *API SECRET ERROR — BOT STOPPED!*\n\n"
            "❌ Wrong API Secret!\n\n"
            "👉 /editcredentials — Update secret"
        ),
        "no_permission": (
            "🚨 *P2P PERMISSION ERROR — BOT STOPPED!*\n\n"
            "❌ API Key has no P2P permission!\n\n"
            "Fix: Bybit → API Management → Edit → Enable P2P\n\n"
            "👉 /editcredentials — After fixing"
        ),
        "proxy_error": (
            "🚨 *PROXY ERROR!*\n\n"
            "❌ IPRoyal proxy connection failed!\n\n"
            "Check:\n"
            "   • Railway → Variables → PROXY_URL correct?\n"
            "   • IPRoyal subscription active?\n"
            "   • Proxy host/port correct?"
        ),
        "no_proxy": (
            "🚨 *PROXY NOT SET!*\n\n"
            "❌ PROXY_URL missing in Railway Variables!\n\n"
            "Bybit will reject all requests.\n"
            "Add PROXY_URL → Railway → Variables\n"
            "Format: socks5://user:pass@host:port"
        ),
        "connection_failed": (
            f"⚠️ *CONNECTION ERROR!*\n\n"
            f"❌ {detail}\n\n"
            f"Bot will retry in {POLL_INTERVAL} seconds."
        ),
        "monitor_error": (
            f"⚠️ *MONITOR ERROR!*\n\n"
            f"❌ {detail}\n\n"
            f"Bot still running."
        ),
    }
    text = msgs.get(etype, f"⚠️ *Error*\n\n`{detail}`")
    try:
        await app.bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Alert send failed [{cid}]: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# STATUS CHANGE ALERTS
# ─────────────────────────────────────────────────────────────────────────────
async def send_status_alert(app, cid, status, order_info):
    """Alert user when order status changes after bot processed it."""
    oid        = order_info.get("order_id", "N/A")
    amount     = order_info.get("amount",   "N/A")
    currency   = order_info.get("currency", "N/A")
    token      = order_info.get("token",    "USDT")
    seller     = order_info.get("seller",   "Unknown")
    pay_method = order_info.get("pay_method","")
    bank_lines = order_info.get("bank_lines","")
    cancel_rsn = order_info.get("cancel_reason","")

    if status == 20:
        text = (
            f"⏳ *SELLER CHECKING PAYMENT*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Order: `{oid}`\n"
            f"👤 Seller: *{seller}*\n"
            f"💰 {amount} {currency} → {token}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Payment marked as done\n"
            f"Seller is checking their bank account\n"
            f"USDT will be released once confirmed\n\n"
            f"⏱️ Sit tight!"
        )
    elif status == 30:
        text = (
            f"🚨 *APPEAL OPENED!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Order: `{oid}`\n"
            f"👤 Seller: *{seller}*\n"
            f"💰 {amount} {currency}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Someone opened a dispute!\n"
            f"Go to Bybit immediately!\n"
            f"P2P Support will contact you in chat."
        )
    elif status == 40:
        reason_text = f"\n📋 Reason: {cancel_rsn}" if cancel_rsn else ""
        text = (
            f"❌ *ORDER CANCELLED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Order: `{oid}`\n"
            f"👤 Seller: *{seller}*\n"
            f"💰 {amount} {currency}{reason_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Seller's USDT has been unlocked\n"
            f"No payment needed for this order"
        )
    elif status == 50:
        text = (
            f"🎉 *USDT RECEIVED!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Order: `{oid}`\n"
            f"👤 Seller: *{seller}*\n"
            f"💰 {amount} {currency}\n"
            f"🪙 {token} is now in your wallet! ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Trade completed successfully! 🎊"
        )
    else:
        return

    try:
        await app.bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Status alert failed [{cid}]: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# REMINDER
# ─────────────────────────────────────────────────────────────────────────────
async def send_reminder(app, cid, oid, seller, amount, currency, pay_method, bank_lines, remind_secs):
    """Send payment reminder at halfway point of payment timer."""
    await asyncio.sleep(remind_secs)
    try:
        # Check order still active before reminding
        if cid not in active_orders or oid not in active_orders.get(cid, {}):
            return
        cur_status = active_orders[cid][oid].get("status", 10)
        if cur_status != 10:
            return  # Already paid or cancelled — no need to remind
        await app.bot.send_message(
            chat_id=cid,
            text=(
                f"⏰ *PAYMENT REMINDER!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Order: `{oid}`\n"
                f"👤 Seller: *{seller}*\n"
                f"💰 *{amount} {currency}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 *Seller Bank:*\n"
                f"💳 {pay_method}\n"
                f"{bank_lines}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ Have you sent the payment?\n"
                f"⏱️ *Time is running out!*"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Reminder error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MONITOR LOOP  (runs every 25 seconds)
# ─────────────────────────────────────────────────────────────────────────────
async def monitor_loop(app: Application):
    logger.info(f"✅ Monitor loop started — polling every {POLL_INTERVAL} sec")
    fail_counts = {}

    if not PROXY_URL:
        logger.warning("⚠️ PROXY_URL not set!")
        for cid in list(user_sessions.keys()):
            await send_error_alert(app, cid, "no_proxy")

    while True:
        try:
            for cid, session in list(user_sessions.items()):
                if not monitoring_active.get(cid, True):
                    continue

                ak = session["api_key"]
                sk = session["api_secret"]

                if cid not in seen_orders:
                    seen_orders[cid] = set()
                if cid not in active_orders:
                    active_orders[cid] = {}

                # ══════════════════════════════════════════
                # LOOP 1 — Check for NEW orders (status=10)
                # ══════════════════════════════════════════
                result = api_pending_orders(ak, sk)
                code   = get_code(result)

                if result is None:
                    fail_counts[cid] = fail_counts.get(cid, 0) + 1
                    if fail_counts[cid] == 3:
                        await send_error_alert(app, cid,
                            "proxy_error" if PROXY_URL else "connection_failed",
                            "No response from Bybit API")
                        fail_counts[cid] = 0
                    logger.warning(f"[{cid}] No response from Bybit")
                    continue

                logger.info(f"[{cid}] Pending poll: code={code} msg={get_msg(result)}")

                # Handle API errors
                if code in (10003, "10003"):
                    monitoring_active[cid] = False; save_sessions()
                    await send_error_alert(app, cid, "api_invalid"); continue
                elif code in (10004, "10004"):
                    monitoring_active[cid] = False; save_sessions()
                    await send_error_alert(app, cid, "api_secret_wrong"); continue
                elif code in (10005, "10005"):
                    monitoring_active[cid] = False; save_sessions()
                    await send_error_alert(app, cid, "no_permission"); continue
                elif code not in (0, "0"):
                    fail_counts[cid] = fail_counts.get(cid, 0) + 1
                    if fail_counts[cid] == 3:
                        await send_error_alert(app, cid, "monitor_error",
                                               f"ret_code={code} {get_msg(result)}")
                        fail_counts[cid] = 0
                    continue

                fail_counts[cid] = 0
                orders = result.get("result", {}).get("items", [])
                logger.info(f"[{cid}] New pending orders found: {len(orders)}")

                for order in orders:
                    oid = order.get("id")
                    if not oid or oid in seen_orders[cid]:
                        continue
                    seen_orders[cid].add(oid)

                    # Basic info from pending list
                    amount   = order.get("amount",         "N/A")
                    price    = order.get("price",          "N/A")
                    currency = order.get("currencyId",     "N/A")
                    token    = order.get("tokenId",        "USDT")
                    seller   = order.get("targetNickName", "Unknown")
                    timer_s  = order.get("transferLastSeconds", "900")
                    timer    = format_timer(timer_s)

                    # ── Get full order detail for bank info ──────────────
                    detail = api_order_detail(ak, sk, oid)
                    if detail and get_code(detail) in (0, "0"):
                        pay_method, bank_lines, payment_id, payment_type, is_online = \
                            extract_payment(detail)
                        # Also get full quantity from detail
                        qty = detail.get("result", {}).get("quantity", token)
                    else:
                        pay_method   = "Unknown"
                        bank_lines   = "   ⚠️ Could not read bank details\n"
                        payment_id   = None
                        payment_type = None
                        is_online    = False
                        qty          = token

                    # ── Click Payment Completed ──────────────────────────
                    clicked    = False
                    click_note = ""
                    if is_online:
                        click_note = "⚠️ Online payment — cannot auto-click!"
                    elif payment_id and payment_type:
                        cr    = api_mark_paid(ak, sk, oid, payment_type, payment_id)
                        ccode = get_code(cr)
                        if ccode in (0, "0"):
                            clicked    = True
                            click_note = "✅ Done automatically!"
                        else:
                            click_note = f"⚠️ Failed (code={ccode}) — do manually on Bybit!"
                        logger.info(f"[{oid}] Mark as paid: code={ccode} msg={get_msg(cr)}")
                    else:
                        click_note = "⚠️ No payment info — do manually on Bybit!"

                    # ── Send message to seller ───────────────────────────
                    seller_msg = (
                        f"✅ Payment Sent!\n\n"
                        f"Dear {seller},\n"
                        f"We have sent your payment of {amount} {currency}.\n\n"
                        f"Please check your {pay_method} account and\n"
                        f"release the {token} once received. 🙏\n\n"
                        f"Thank you for trading with us!"
                    )
                    msg_r  = api_send_message(ak, sk, oid, seller_msg)
                    msg_ok = get_code(msg_r) in (0, "0")
                    logger.info(f"[{oid}] Seller msg: ok={msg_ok}")

                    # ── Alert YOU on Telegram ────────────────────────────
                    alert_text = (
                        f"🚨 *NEW ORDER — ACT NOW!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📦 Order ID: `{oid}`\n"
                        f"👤 Seller: *{seller}*\n"
                        f"💰 Pay: *{amount} {currency}*\n"
                        f"🪙 Get: *{qty} {token}*\n"
                        f"💵 Price: {price}\n"
                        f"⏱️ Time Left: *{timer}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏦 *SEND MONEY TO SELLER NOW:*\n"
                        f"💳 Method: *{pay_method}*\n"
                        f"{bank_lines}"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🤖 Transfer Clicked: {click_note}\n"
                        f"💬 Seller Messaged: {'✅' if msg_ok else '⚠️ Failed'}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚡ *Send {amount} {currency} to seller bank NOW!*"
                    )
                    await app.bot.send_message(
                        chat_id=cid, text=alert_text, parse_mode="Markdown"
                    )

                    # ── Save to active orders for status tracking ────────
                    active_orders[cid][oid] = {
                        "order_id":    oid,
                        "status":      10,
                        "amount":      amount,
                        "currency":    currency,
                        "token":       token,
                        "seller":      seller,
                        "pay_method":  pay_method,
                        "bank_lines":  bank_lines,
                        "cancel_reason": ""
                    }

                    # ── Reminder at halfway point of timer ───────────────
                    try:
                        remind_at = int(timer_s) // 2
                    except:
                        remind_at = 450  # fallback 7.5 min
                    asyncio.create_task(
                        send_reminder(app, cid, oid, seller, amount,
                                      currency, pay_method, bank_lines, remind_at)
                    )
                    logger.info(f"✅ New order [{oid}] processed for [{cid}]")

                # ══════════════════════════════════════════════════════════
                # LOOP 2 — Track ACTIVE orders for status changes
                # ══════════════════════════════════════════════════════════
                for oid, order_info in list(active_orders.get(cid, {}).items()):
                    old_status = order_info.get("status", 10)

                    detail = api_order_detail(ak, sk, oid)
                    if not detail or get_code(detail) not in (0, "0"):
                        continue

                    new_status    = detail.get("result", {}).get("status", old_status)
                    cancel_reason = detail.get("result", {}).get("cancelReason", "")

                    if new_status == old_status:
                        continue  # No change — skip

                    logger.info(f"[{oid}] Status changed: {old_status} → {new_status}")

                    # Update stored status + cancel reason
                    active_orders[cid][oid]["status"]        = new_status
                    active_orders[cid][oid]["cancel_reason"] = cancel_reason

                    # Send status change alert to user
                    await send_status_alert(app, cid, new_status, active_orders[cid][oid])

                    # Remove from active tracking if order is done
                    if new_status in (40, 50):
                        del active_orders[cid][oid]
                        logger.info(f"[{oid}] Removed from active tracking (done)")

        except Exception as e:
            logger.error(f"Monitor loop exception: {e}")

        await asyncio.sleep(POLL_INTERVAL)

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in user_sessions:
        k = user_sessions[cid]["api_key"]
        await update.message.reply_text(
            f"⚠️ *Credentials already saved!*\n\n"
            f"🔑 Key: `{k[:6]}••••{k[-4:]}`\n\nWhat to do?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👁 View Credentials",    callback_data="view_creds")],
                [InlineKeyboardButton("✏️ Edit Credentials",    callback_data="edit_creds")],
                [InlineKeyboardButton("🗑 Remove Credentials",  callback_data="remove_creds")],
                [InlineKeyboardButton("▶️ Continue Monitoring", callback_data="keep_creds")],
            ])
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🤖 *Bybit P2P Auto-Bot*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "What this bot does:\n\n"
        "1️⃣ You post BUY USDT ad on Bybit P2P\n"
        "2️⃣ Seller accepts your ad\n"
        f"3️⃣ Bot detects in {POLL_INTERVAL} seconds\n"
        "4️⃣ Bot reads full seller bank details\n"
        "5️⃣ Bot clicks Transfer Completed ✅\n"
        "6️⃣ Bot messages seller automatically\n"
        "7️⃣ Bot alerts YOU with bank details\n"
        "8️⃣ YOU send money to seller bank\n"
        "9️⃣ Bot alerts when seller releases USDT\n"
        "🔟 Reminder at halfway mark if not paid\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 Enter your *Bybit API Key*:",
        parse_mode="Markdown"
    )
    return ASK_API_KEY

async def cmd_ask_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["api_key"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API Key saved!\n\n🔐 Enter your *Bybit API Secret*:",
        parse_mode="Markdown"
    )
    return ASK_SECRET_KEY

async def cmd_save_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ak  = context.user_data.get("api_key", "")
    sk  = update.message.text.strip()
    cid = update.effective_chat.id

    if len(ak) < 10 or len(sk) < 10:
        await update.message.reply_text("❌ Key or secret too short. Run /start again.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⏳ *Verifying via IPRoyal proxy...*", parse_mode="Markdown"
    )

    if not PROXY_URL:
        await update.message.reply_text(
            "⚠️ *PROXY_URL not set in Railway!*\n"
            "Bybit will reject requests — add PROXY_URL first.",
            parse_mode="Markdown"
        )

    result = bybit_request(ak, sk, "GET", "/v5/user/query-api")
    code   = get_code(result)

    if result is None or code is None:
        await update.message.reply_text(
            "❌ *Cannot connect to Bybit*\n\n"
            "Check PROXY_URL in Railway Variables.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if code in (0, "0"):
        perms   = result.get("result", {}).get("permissions", {})
        has_p2p = "P2P" in str(perms)

        user_sessions[cid]     = {"api_key": ak, "api_secret": sk}
        seen_orders[cid]       = set()
        active_orders[cid]     = {}
        monitoring_active[cid] = True
        save_sessions()

        await update.message.reply_text(
            "🎉 *Verification Successful!*\n\n"
            "🔑 API Key: ✅ Valid\n"
            "🔐 API Secret: ✅ Valid\n"
            "🌐 Bybit: ✅ Connected via IPRoyal\n"
            "📡 Telegram: ✅ Direct Railway\n"
            f"📋 P2P Permission: {'✅ Enabled' if has_p2p else '⚠️ Enable P2P on API key!'}\n\n"
            "💾 *Credentials saved permanently!*\n\n"
            f"🤖 *Bot is LIVE — monitoring every {POLL_INTERVAL} sec!*\n\n"
            "📋 /checkorders — Scan now\n"
            "📊 /status — Bot status\n"
            "👁 /mycredentials — Manage keys\n",
            parse_mode="Markdown"
        )
    elif code in (10003, "10003"):
        await update.message.reply_text("❌ *Invalid API Key* — not found on Bybit.\nRun /start again.", parse_mode="Markdown")
    elif code in (10004, "10004"):
        await update.message.reply_text("❌ *Wrong API Secret*\nRun /start again.", parse_mode="Markdown")
    elif code in (10005, "10005"):
        await update.message.reply_text("❌ *No P2P Permission*\nEnable P2P on Bybit API key.\nRun /start again.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Bybit Error `{code}`: {get_msg(result)}\nRun /start again.", parse_mode="Markdown")

    return ConversationHandler.END

async def cmd_mycredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    s   = user_sessions.get(cid)
    if not s:
        await update.message.reply_text("❌ No credentials. Run /start first.")
        return
    k  = s["api_key"];    km = k[:6] + "••••••••••" + k[-4:]
    sc = s["api_secret"]; sm = sc[:4] + "••••••••••••••••" + sc[-4:]
    await update.message.reply_text(
        f"👁 *Your Credentials*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Key:\n`{km}`\n\n"
        f"🔐 Secret:\n`{sm}`\n\n"
        f"📡 Proxy: {'✅ Set' if PROXY_URL else '❌ Not set!'}\n"
        f"📊 Monitoring: {'✅ Running' if monitoring_active.get(cid) else '⏹ Stopped'}\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit",   callback_data="edit_creds")],
            [InlineKeyboardButton("🗑 Remove", callback_data="remove_creds")],
            [InlineKeyboardButton("❌ Close",  callback_data="close_menu")],
        ])
    )

async def cmd_editcredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in user_sessions:
        await update.message.reply_text("❌ No credentials. Run /start first.")
        return ConversationHandler.END
    monitoring_active[cid] = False
    save_sessions()
    await update.message.reply_text(
        "✏️ *Edit Credentials*\n\n⏸ Monitoring paused.\n\n🔑 New API Key:",
        parse_mode="Markdown"
    )
    return EDIT_API_KEY

async def cmd_edit_ask_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_api_key"] = update.message.text.strip()
    await update.message.reply_text("✅ Key received!\n\n🔐 New API Secret:", parse_mode="Markdown")
    return EDIT_SECRET_KEY

async def cmd_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ak  = context.user_data.get("new_api_key", "")
    sk  = update.message.text.strip()

    result = bybit_request(ak, sk, "GET", "/v5/user/query-api")
    code   = get_code(result)

    if result is None or code not in (0, "0"):
        await update.message.reply_text(
            f"❌ Verification failed! Code: `{code}`\nOld credentials still active.",
            parse_mode="Markdown"
        )
        monitoring_active[cid] = True; save_sessions()
        return ConversationHandler.END

    user_sessions[cid]     = {"api_key": ak, "api_secret": sk}
    seen_orders[cid]       = set()
    active_orders[cid]     = {}
    monitoring_active[cid] = True
    save_sessions()
    await update.message.reply_text(
        f"✅ *Credentials Updated!*\n\n🔑 `{ak[:6]}••••{ak[-4:]}`\n▶️ Monitoring resumed!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cmd_removecredentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in user_sessions:
        await update.message.reply_text("❌ No credentials to remove.")
        return
    await update.message.reply_text(
        "🗑 *Remove all credentials?*\n\n⚠️ This will delete everything.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Remove", callback_data="confirm_remove")],
            [InlineKeyboardButton("❌ Cancel",      callback_data="close_menu")],
        ])
    )

async def cmd_stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in user_sessions:
        await update.message.reply_text("⚠️ No session. Run /start first.")
        return
    monitoring_active[cid] = False; save_sessions()
    await update.message.reply_text(
        "⏹ *Monitoring STOPPED*\n\n▶️ /startbot to resume.", parse_mode="Markdown"
    )

async def cmd_startbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in user_sessions:
        await update.message.reply_text("⚠️ No credentials. Run /start first.")
        return
    monitoring_active[cid] = True
    seen_orders[cid]       = set()
    active_orders[cid]     = {}
    save_sessions()
    await update.message.reply_text(
        f"▶️ *Monitoring STARTED!*\n\n"
        f"✅ Checking every {POLL_INTERVAL} seconds\n"
        f"✅ Bybit via IPRoyal proxy\n"
        f"✅ Auto-click Transfer Completed\n"
        f"✅ Auto-message seller\n"
        f"✅ Instant Telegram alerts\n"
        f"✅ Status change alerts (20/30/40/50)\n"
        f"✅ Reminder at halfway timer\n\n"
        f"⏹ /stopbot to pause.",
        parse_mode="Markdown"
    )

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    user_sessions.pop(cid, None)
    seen_orders.pop(cid, None)
    active_orders.pop(cid, None)
    monitoring_active.pop(cid, None)
    save_sessions()
    await update.message.reply_text("🔄 *Reset complete!*\n\nRun /start to set up again.", parse_mode="Markdown")

async def cmd_checkorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    s   = user_sessions.get(cid)
    if not s:
        await update.message.reply_text("⚠️ No credentials. Run /start first.")
        return

    await update.message.reply_text("🔄 Scanning Bybit P2P orders via IPRoyal proxy...")
    ak, sk = s["api_key"], s["api_secret"]

    result = api_pending_orders(ak, sk)
    code   = get_code(result)
    logger.info(f"/checkorders [{cid}]: code={code} msg={get_msg(result)}")

    if not result or code not in (0, "0"):
        await update.message.reply_text(
            f"❌ *API Error*\nCode: `{code}`\nMsg: {get_msg(result)}\n\n"
            f"Check /status for details.",
            parse_mode="Markdown"
        )
        return

    items = result.get("result", {}).get("items", [])
    if not items:
        await update.message.reply_text(
            f"📭 *No pending orders right now.*\n\n"
            f"Bot checks every {POLL_INTERVAL} sec.\n"
            f"Will alert instantly when seller accepts your ad!",
            parse_mode="Markdown"
        )
        return

    for order in items[:5]:
        oid      = order.get("id")
        amount   = order.get("amount",         "N/A")
        price    = order.get("price",          "N/A")
        currency = order.get("currencyId",     "N/A")
        token    = order.get("tokenId",        "USDT")
        seller   = order.get("targetNickName", "Unknown")
        timer_s  = order.get("transferLastSeconds", "0")
        timer    = format_timer(timer_s)

        detail = api_order_detail(ak, sk, oid)
        if detail and get_code(detail) in (0, "0"):
            pay_method, bank_lines, _, _, _ = extract_payment(detail)
        else:
            pay_method = "Unknown"
            bank_lines = "   ⚠️ Could not read\n"

        await update.message.reply_text(
            f"⏳ *Pending Order*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 ID: `{oid}`\n"
            f"👤 Seller: {seller}\n"
            f"💰 Pay: *{amount} {currency}*\n"
            f"🪙 Token: {token}\n"
            f"💵 Price: {price}\n"
            f"⏱️ Time Left: *{timer}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Seller Bank:\n💳 {pay_method}\n{bank_lines}",
            parse_mode="Markdown"
        )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid        = update.effective_chat.id
    has_cred   = cid in user_sessions
    is_running = monitoring_active.get(cid, False)
    active_cnt = len(active_orders.get(cid, {}))
    key_info   = ""
    if has_cred:
        k = user_sessions[cid]["api_key"]
        key_info = f"\n🔑 `{k[:6]}••••{k[-4:]}`"

    await update.message.reply_text(
        f"📊 *Bot Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Credentials: {'✅ Saved' if has_cred else '❌ Run /start'}{key_info}\n"
        f"📡 Proxy: {'✅ IPRoyal set' if PROXY_URL else '❌ Not set!'}\n"
        f"🔍 Monitoring: {'✅ Every ' + str(POLL_INTERVAL) + ' sec' if is_running else '⏹ Stopped'}\n"
        f"📋 Active Orders: {active_cnt} being tracked\n"
        f"⚡ Auto Transfer Click: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"💬 Auto Seller Message: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"🔔 Status Alerts (20/30/40/50): {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"⏰ Reminder at half timer: {'✅ ON' if is_running else '⏹ OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"▶️ /startbot  ⏹ /stopbot  🔄 /restart\n"
        f"📋 /checkorders  👁 /mycredentials",
        parse_mode="Markdown"
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled. Use /start to begin.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# INLINE BUTTON HANDLER
# ─────────────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cid   = query.message.chat_id
    await query.answer()

    if query.data == "view_creds":
        s = user_sessions.get(cid)
        if s:
            k = s["api_key"]; km = k[:6] + "••••••••••" + k[-4:]
            sc = s["api_secret"]; sm = sc[:4] + "••••••••••••••••" + sc[-4:]
            await query.edit_message_text(
                f"👁 *Credentials*\n🔑 `{km}`\n🔐 `{sm}`\n"
                f"📡 Proxy: {'✅' if PROXY_URL else '❌'}\n"
                f"📊 {'✅ Running' if monitoring_active.get(cid) else '⏹ Stopped'}",
                parse_mode="Markdown"
            )
    elif query.data == "edit_creds":
        monitoring_active[cid] = False; save_sessions()
        await query.edit_message_text("✏️ Send /editcredentials to update.", parse_mode="Markdown")
    elif query.data == "remove_creds":
        await query.edit_message_text(
            "🗑 *Are you sure?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="confirm_remove")],
                [InlineKeyboardButton("❌ No",  callback_data="close_menu")],
            ])
        )
    elif query.data == "confirm_remove":
        user_sessions.pop(cid, None)
        seen_orders.pop(cid, None)
        active_orders.pop(cid, None)
        monitoring_active.pop(cid, None)
        save_sessions()
        await query.edit_message_text("🗑 Removed! Run /start to add new keys.", parse_mode="Markdown")
    elif query.data == "keep_creds":
        await query.edit_message_text(
            f"✅ Continuing.\n📊 {'✅ Running' if monitoring_active.get(cid) else '⏹ Use /startbot'}",
            parse_mode="Markdown"
        )
    elif query.data == "close_menu":
        await query.edit_message_text("✅ Closed.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ TELEGRAM_TOKEN not set in Railway Variables!")
    if not PROXY_URL:
        logger.warning("⚠️ PROXY_URL not set — Bybit calls will use Railway IP (not whitelisted!)")
    else:
        logger.info(f"✅ Proxy set — Bybit via IPRoyal SOCKS5")
        logger.info(f"✅ Telegram via direct Railway")
        logger.info(f"✅ Polling every {POLL_INTERVAL} seconds")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_API_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_ask_secret)],
            ASK_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_save_creds)],
        },
        fallbacks=[CommandHandler("cancel",  cmd_cancel),
                   CommandHandler("start",   cmd_start),
                   CommandHandler("restart", cmd_restart)],
        allow_reentry=True,
    )
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("editcredentials", cmd_editcredentials)],
        states={
            EDIT_API_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_edit_ask_secret)],
            EDIT_SECRET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_edit_save)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(edit_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("startbot",          cmd_startbot))
    app.add_handler(CommandHandler("stopbot",           cmd_stopbot))
    app.add_handler(CommandHandler("restart",           cmd_restart))
    app.add_handler(CommandHandler("checkorders",       cmd_checkorders))
    app.add_handler(CommandHandler("status",            cmd_status))
    app.add_handler(CommandHandler("mycredentials",     cmd_mycredentials))
    app.add_handler(CommandHandler("removecredentials", cmd_removecredentials))

    logger.info("🤖 Bot starting...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(monitor_loop(app))
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
