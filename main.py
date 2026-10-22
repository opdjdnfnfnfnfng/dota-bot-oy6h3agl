#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, subprocess, importlib, asyncio, json, base64, re, secrets, string, random, warnings, time, html
from datetime import datetime

warnings.filterwarnings("ignore", category=UserWarning, module="telegram.ext.conversationhandler")

# ================= AUTO-INSTALL =================
def ensure(pkg, name=None):
    name = name or pkg
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"📦 Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

ensure("aiohttp")
ensure("python-telegram-bot", "telegram")
ensure("instagrapi")

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from instagrapi import Client as IGClient
from instagrapi.exceptions import (
    TwoFactorRequired, ChallengeRequired, BadPassword, UserNotFound,
    RateLimitError, LoginRequired, PleaseWaitFewMinutes, ClientError,
    ClientForbiddenError
)

# ================= HARDCODED =================
GITHUB_PAT       = "REDACTED"
MAIN_BOT_TOKEN   = "REDACTED"
OWNER_ID         = 8437109558

GITHUB_USER      = ""
REPO_NAME        = ""

# ================= CONSTANTS =================
CONFIG_FILE      = "config.txt"
MAIN_FILE        = "main.py"
GITHUB_API       = "https://api.github.com"
MAX_IG_PER_USER  = 4
MAX_TG_PER_USER  = 2
MAX_IG_PER_PROXY = 2

S_TG_TOKEN, S_IG_USER, S_IG_PASS, S_IG_2FA, S_IG_TEXT = range(5)
S_ADD_BOT, S_BROADCAST = range(5, 7)

# ================= GLOBAL =================
CONFIG = {}
PROXY_POOL = set()
PROXY_ASSIGN = {}
PROXY_LOCK = asyncio.Lock()
IG_TASKS = {}
IG_CLIENTS = {}
TEMP = {}
STOP_FLAG = False
TG_APPS = {}
GH_SESSION = None
GH_DIRTY = False

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

# ================= HELPERS =================
def esc(s):
    if s is None:
        return ""
    return html.escape(str(s))

def is_valid_token(token):
    if not token or ":" not in token:
        return False
    try:
        bot_id, auth = token.split(":", 1)
        return bot_id.isdigit() and len(auth) >= 30
    except Exception:
        return False

def make_safe_ig_client():
    cl = IGClient()
    def _no_prompt(*args, **kwargs):
        return None
    for attr in ("challenge_code_handler", "change_password_handler",
                 "two_factor_handler", "verification_code_handler",
                 "totp_code_handler"):
        try:
            setattr(cl, attr, _no_prompt)
        except Exception:
            pass
    return cl

def cleanup_session_files():
    import glob
    for pattern in ("session_*.json", "*.session", "settings_*.json"):
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except Exception:
                pass

def is_network_error(e):
    msg = str(e).lower()
    keywords = (
        "proxy", "connect", "timeout", "tunnel", "connection reset",
        "read timed out", "connecterror", "proxyerror", "remotedisconnected",
        "10054", "10060", "eof occurred", "ssl", "unreachable"
    )
    return any(k in msg for k in keywords)

async def safe_edit(q, text, reply_markup=None, parse_mode="HTML"):
    try:
        await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass

# ================= CONFIG =================
def default_config():
    return {
        "version": 4, "last_sync": None,
        "github_user": "", "github_repo": "",
        "bots": [], "users": {}, "ig_ids": []
    }

def load_config():
    global CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        CONFIG = default_config()
        save_config()
        return

    if not isinstance(loaded, dict):
        loaded = {}

    base = default_config()
    for key, default_val in base.items():
        if key not in loaded:
            loaded[key] = default_val

    if not isinstance(loaded.get("bots"), list):
        loaded["bots"] = []
    if not isinstance(loaded.get("ig_ids"), list):
        loaded["ig_ids"] = []
    if not isinstance(loaded.get("users"), dict):
        loaded["users"] = {}

    CONFIG = loaded
    save_config()

def save_config():
    global GH_DIRTY
    CONFIG["last_sync"] = datetime.now().isoformat()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
        GH_DIRTY = True
    except Exception:
        pass

def save_stats_only():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception:
        pass

def user_tg_count(uid):
    return len([b for b in CONFIG["bots"] if b.get("owner_id") == uid and not b.get("is_main")])

def user_ig_count(uid):
    return len([ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid])

def get_owner_tg_bots(uid):
    return [b for b in CONFIG["bots"] if b.get("owner_id") == uid and not b.get("is_main")]

def find_owner_app(uid):
    for b in CONFIG["bots"]:
        if b.get("owner_id") == uid and b["token"] in TG_APPS:
            return TG_APPS[b["token"]]
    return None

# ================= GITHUB =================
async def gh_get(session, path):
    try:
        async with session.get(f"{GITHUB_API}{path}", timeout=15) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)

async def gh_post(session, path, payload):
    try:
        async with session.post(f"{GITHUB_API}{path}", json=payload, timeout=15) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)

async def gh_put(session, path, payload):
    try:
        async with session.put(f"{GITHUB_API}{path}", json=payload, timeout=15) as r:
            return r.status, await r.text()
    except Exception as e:
        return None, str(e)

async def gh_push_file(session, path, content, msg="auto"):
    try:
        status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
        sha = json.loads(body).get("sha") if status == 200 else None
        payload = {"message": msg, "content": base64.b64encode(content.encode("utf-8")).decode(), "branch": "main"}
        if sha:
            payload["sha"] = sha
        rstatus, _ = await gh_put(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}", payload)
        return rstatus in (200, 201)
    except Exception:
        return False

async def gh_pull_file(session, path):
    try:
        status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
        if status == 200:
            return base64.b64decode(json.loads(body)["content"]).decode("utf-8")
    except Exception:
        pass
    return None

async def gh_bootstrap():
    global GITHUB_USER, REPO_NAME, GH_SESSION
    headers = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github+json", "User-Agent": "DotaBot"}
    conn = aiohttp.TCPConnector(ssl=False)
    GH_SESSION = aiohttp.ClientSession(connector=conn, headers=headers)

    try:
        if CONFIG.get("github_user") and CONFIG.get("github_repo"):
            GITHUB_USER = CONFIG["github_user"]
            REPO_NAME = CONFIG["github_repo"]
        else:
            status, body = await gh_get(GH_SESSION, "/user")
            if status != 200:
                return
            GITHUB_USER = json.loads(body)["login"]
            REPO_NAME = f"dota-bot-{''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))}"
            await gh_post(GH_SESSION, "/user/repos", {"name": REPO_NAME, "private": False})
            CONFIG["github_user"] = GITHUB_USER
            CONFIG["github_repo"] = REPO_NAME
            save_config()
            await asyncio.sleep(1)

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                await gh_push_file(GH_SESSION, CONFIG_FILE, f.read(), "config")
        except Exception:
            pass

        try:
            with open(__file__, "r", encoding="utf-8") as f:
                mc = f.read()
            mc = re.sub(r'^(GITHUB_PAT\s*=\s*)".*?"', r'\1"REDACTED"', mc, flags=re.MULTILINE)
            mc = re.sub(r'^(MAIN_BOT_TOKEN\s*=\s*)".*?"', r'\1"REDACTED"', mc, flags=re.MULTILINE)
            await gh_push_file(GH_SESSION, MAIN_FILE, mc, "main")
        except Exception:
            pass
    except Exception as e:
        print(f"[GH] {type(e).__name__}: {e}")

async def gh_sync_loop():
    global GH_DIRTY
    while not STOP_FLAG:
        try:
            await asyncio.sleep(30)
            if not GH_SESSION or not GITHUB_USER:
                continue

            # Push if dirty
            if GH_DIRTY:
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        ok = await gh_push_file(GH_SESSION, CONFIG_FILE, f.read(), "config")
                    if ok:
                        GH_DIRTY = False
                except Exception:
                    pass

            # Pull ONLY if no active IG tasks (Bug #26 fix - prevent config race)
            if not IG_TASKS:
                remote_text = await gh_pull_file(GH_SESSION, CONFIG_FILE)
                if remote_text:
                    try:
                        rjson = json.loads(remote_text)
                        remote_ts = rjson.get("last_sync") or ""
                        local_ts = CONFIG.get("last_sync") or ""
                        if remote_ts > local_ts:
                            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                                f.write(remote_text)
                            load_config()
                            print("[SYNC] Pulled remote config (no active tasks)")
                    except Exception:
                        pass
        except Exception:
            pass

# ================= PROXY =================
def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        line = line.split("://", 1)[1]
    if "@" in line:
        return None
    parts = line.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"
    return None

async def proxy_fetcher():
    while not STOP_FLAG:
        try:
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=conn) as s:
                for url in PROXY_SOURCES:
                    try:
                        async with s.get(url, timeout=10) as r:
                            if r.status == 200:
                                text = await r.text()
                                for l in text.splitlines():
                                    p = parse_proxy(l)
                                    if p:
                                        async with PROXY_LOCK:
                                            PROXY_POOL.add(p)
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(30)

async def get_proxy_for(username):
    async with PROXY_LOCK:
        for proxy, igs in PROXY_ASSIGN.items():
            if username in igs:
                return proxy
        for proxy in PROXY_POOL:
            if len(PROXY_ASSIGN.get(proxy, [])) < MAX_IG_PER_PROXY:
                PROXY_ASSIGN.setdefault(proxy, []).append(username)
                return proxy
    return None

async def release_proxy_for(username):
    async with PROXY_LOCK:
        for proxy in list(PROXY_ASSIGN.keys()):
            if username in PROXY_ASSIGN[proxy]:
                PROXY_ASSIGN[proxy].remove(username)
                if not PROXY_ASSIGN[proxy]:
                    del PROXY_ASSIGN[proxy]
                return

# ================= IG LOOP =================
async def ig_loop(ig, app):
    username = ig["username"]
    owner_id = ig["owner_id"]

    async def notify(text):
        try:
            await app.bot.send_message(owner_id, text, parse_mode="HTML")
        except Exception:
            pass

    consecutive_errors = 0

    try:
        cl = make_safe_ig_client()

        if ig.get("settings"):
            try:
                cl.set_settings(ig["settings"])
            except Exception:
                pass

        session_ok = False
        try:
            await asyncio.to_thread(cl.account_info)
            session_ok = True
        except (LoginRequired, TwoFactorRequired):
            session_ok = False
        except Exception as e:
            print(f"[IG] {username} session check: {type(e).__name__}")
            session_ok = True

        if not session_ok:
            if not ig.get("password"):
                await notify(f"❌ @{esc(username)} session dead, no password. Delete & re-add.")
                ig["running"] = False
                save_config()
                return
            try:
                await notify(f"🔄 @{esc(username)} session expired, re-login...")
                await asyncio.to_thread(cl.login, username, ig["password"])
                ig["settings"] = cl.get_settings()
                save_config()
                await notify(f"✅ @{esc(username)} session refreshed.")
            except TwoFactorRequired:
                await notify(f"🔐 @{esc(username)} 2FA needed. Delete & re-add with 2FA.")
                ig["running"] = False
                save_config()
                return
            except Exception as e:
                await notify(f"❌ @{esc(username)} re-login failed: {esc(type(e).__name__)}")
                ig["running"] = False
                save_config()
                return

        IG_CLIENTS[username] = cl

        proxy = await get_proxy_for(username)
        if not proxy:
            await notify(f"⚠️ @{esc(username)} no proxy. Waiting 30s...")
            await asyncio.sleep(30)
            proxy = await get_proxy_for(username)
            if not proxy:
                await notify(f"❌ @{esc(username)} still no proxy, stopping.")
                ig["running"] = False
                save_config()
                return

        ig["proxy"] = proxy
        save_config()
        try:
            await asyncio.to_thread(cl.set_proxy, proxy)
        except Exception:
            pass

        count = ig.get("stats", {}).get("sent", 0)
        total_errors = ig.get("stats", {}).get("errors_total", 0)

        while not STOP_FLAG and ig.get("running", True):
            try:
                num = random.randint(1, 10**10)
                txt = ig["text"]
                msg = f"{num} {txt}" if random.choice([True, False]) else f"{txt} {num}"

                await asyncio.to_thread(cl.direct_send, msg, [ig["gc_id"]])
                count += 1
                consecutive_errors = 0

                ig["stats"] = {
                    "sent": count,
                    "errors_total": total_errors,
                    "errors_current": 0,
                    "last_send": datetime.now().isoformat()
                }
                save_stats_only()

                await asyncio.sleep(random.uniform(30, 50))

            except RateLimitError:
                consecutive_errors += 1
                await notify(f"⏳ @{esc(username)} rate limited. Waiting 2 min...")
                await asyncio.sleep(120)

            except PleaseWaitFewMinutes:
                consecutive_errors += 1
                await notify(f"⏳ @{esc(username)} please wait 10 min...")
                await asyncio.sleep(600)

            except TwoFactorRequired:
                await notify(f"🔐 @{esc(username)} 2FA needed mid-loop. Stopping.")
                ig["running"] = False
                save_config()
                return

            except LoginRequired:
                try:
                    await asyncio.to_thread(cl.login, username, ig["password"])
                    ig["settings"] = cl.get_settings()
                    save_config()
                    await notify(f"🔄 @{esc(username)} session refreshed.")
                    continue
                except TwoFactorRequired:
                    await notify(f"🔐 @{esc(username)} 2FA needed. Delete & re-add.")
                    ig["running"] = False
                    save_config()
                    return
                except Exception as e:
                    await notify(f"❌ @{esc(username)} re-login failed: {esc(type(e).__name__)}")
                    ig["running"] = False
                    save_config()
                    return

            except ClientForbiddenError:
                await notify(f"🚫 @{esc(username)} forbidden (account flagged). Stopping.")
                ig["running"] = False
                save_config()
                return

            except ClientError as e:
                msg_lower = str(e).lower()
                if "thread" in msg_lower and "not found" in msg_lower:
                    await notify(f"❌ @{esc(username)} GC/thread not found. Delete & re-add.")
                    ig["running"] = False
                    save_config()
                    return

                total_errors += 1
                consecutive_errors += 1
                ig["stats"]["errors_total"] = total_errors
                ig["stats"]["errors_current"] = consecutive_errors
                ig["last_error"] = {"type": type(e).__name__, "msg": str(e)[:200], "ts": datetime.now().isoformat()}
                save_stats_only()

                if is_network_error(e):
                    await notify(f"🔀 @{esc(username)} network error ({esc(type(e).__name__)}), migrating proxy...")
                    await release_proxy_for(username)
                    new_proxy = await get_proxy_for(username)
                    if new_proxy:
                        ig["proxy"] = new_proxy
                        save_config()
                        try:
                            await asyncio.to_thread(cl.set_proxy, new_proxy)
                        except Exception:
                            pass
                        await asyncio.sleep(5)
                        continue
                    else:
                        await notify(f"⚠️ @{esc(username)} no proxy available. Waiting 30s...")
                        await asyncio.sleep(30)
                        continue

                backoff = min(30 * (2 ** min(consecutive_errors - 1, 4)), 600)
                await notify(f"⚠️ @{esc(username)} error ({esc(type(e).__name__)}): {esc(str(e)[:100])}")
                await asyncio.sleep(backoff)

            except Exception as e:
                total_errors += 1
                consecutive_errors += 1
                ig["stats"]["errors_total"] = total_errors
                ig["stats"]["errors_current"] = consecutive_errors
                ig["last_error"] = {"type": type(e).__name__, "msg": str(e)[:200], "ts": datetime.now().isoformat()}
                save_stats_only()

                if is_network_error(e):
                    await notify(f"🔀 @{esc(username)} network error, migrating...")
                    await release_proxy_for(username)
                    new_proxy = await get_proxy_for(username)
                    if new_proxy:
                        ig["proxy"] = new_proxy
                        save_config()
                        try:
                            await asyncio.to_thread(cl.set_proxy, new_proxy)
                        except Exception:
                            pass
                        await asyncio.sleep(5)
                        continue
                    else:
                        await asyncio.sleep(30)
                        continue

                if consecutive_errors >= 3:
                    await notify(f"🔀 @{esc(username)} 3+ errors, trying new proxy...")
                    await release_proxy_for(username)
                    new_proxy = await get_proxy_for(username)
                    if new_proxy:
                        ig["proxy"] = new_proxy
                        save_config()
                        try:
                            await asyncio.to_thread(cl.set_proxy, new_proxy)
                        except Exception:
                            pass
                        consecutive_errors = 0
                        await asyncio.sleep(5)
                        continue

                await notify(f"❓ @{esc(username)} error: {esc(type(e).__name__)}: {esc(str(e)[:100])}")
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await notify(f"❌ @{esc(username)} fatal: {esc(type(e).__name__)}: {esc(str(e)[:150])}")
        ig["running"] = False
        save_config()
    finally:
        await release_proxy_for(username)
        IG_CLIENTS.pop(username, None)
        IG_TASKS.pop(username, None)

# ================= MENUS =================
def main_menu(uid):
    rows = [
        [InlineKeyboardButton("📱 My TG Bots", callback_data="tg_view"),
         InlineKeyboardButton("📸 My IG IDs", callback_data="ig_view")],
        [InlineKeyboardButton("➕ Add TG Bot", callback_data="add_tg"),
         InlineKeyboardButton("➕ Add IG ID", callback_data="add_ig")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dash"),
         InlineKeyboardButton("🧹 Clean All", callback_data="clean")],
    ]
    if uid == OWNER_ID:
        rows.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner")])
    return InlineKeyboardMarkup(rows)

def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Host New Bot", callback_data="owner_addbot")],
        [InlineKeyboardButton("📋 List All Bots", callback_data="owner_listbots")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast")],
        [InlineKeyboardButton("📊 Global Stats", callback_data="dash")],
        [InlineKeyboardButton("🔙 Back", callback_data="home")],
    ])

def back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="home")]])

def back_owner():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Owner Panel", callback_data="owner")]])

async def show_home(q):
    uid = q.from_user.id
    text = (
        f"💗 <b>Dota Bot</b>\n\n"
        f"Welcome, <b>{esc(q.from_user.first_name)}</b>!\n\n"
        f"<b>Your Limits:</b>\n"
        f"📱 TG Bots: <code>{user_tg_count(uid)}/{MAX_TG_PER_USER}</code>\n"
        f"📸 IG IDs: <code>{user_ig_count(uid)}/{MAX_IG_PER_USER}</code>"
    )
    await safe_edit(q, text, reply_markup=main_menu(uid))

# ================= START =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) not in CONFIG["users"]:
        CONFIG["users"][str(uid)] = {"first_seen": datetime.now().isoformat()}
        save_config()
    text = (
        f"💗 <b>Dota Bot</b>\n\n"
        f"Welcome, <b>{esc(update.effective_user.first_name)}</b>!\n\n"
        f"<b>Your Limits:</b>\n"
        f"📱 TG Bots: <code>{user_tg_count(uid)}/{MAX_TG_PER_USER}</code>\n"
        f"📸 IG IDs: <code>{user_ig_count(uid)}/{MAX_IG_PER_USER}</code>"
    )
    await update.message.reply_text(text, reply_markup=main_menu(uid), parse_mode="HTML")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    TEMP.pop(update.effective_user.id, None)
    await update.message.reply_text("🛑 Cancelled.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END

# ================= VIEWS =================
async def cb_tg_view(q):
    uid = q.from_user.id
    tokens = get_owner_tg_bots(uid)
    if not tokens:
        text = f"📱 <b>No TG Bots</b>\n\nHost up to {MAX_TG_PER_USER} bots."
    else:
        text = f"📱 <b>Your TG Bots ({len(tokens)}/{MAX_TG_PER_USER})</b>\n\n"
        for i, t in enumerate(tokens, 1):
            bot_id = t["token"].split(":")[0]
            text += f"{i}. Bot ID: <code>{esc(bot_id)}</code>\n"

    kb = [[InlineKeyboardButton("➕ Add New Bot", callback_data="add_tg")]]
    for t in tokens:
        bot_id = t["token"].split(":")[0]
        kb.append([InlineKeyboardButton(f"🗑 Remove {bot_id}", callback_data=f"del_tg:{bot_id}")])
    kb.append([InlineKeyboardButton("🔙 Menu", callback_data="home")])
    await safe_edit(q, text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_ig_view(q):
    uid = q.from_user.id
    igs = [ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid]
    if not igs:
        text = f"📸 <b>No IG IDs</b>\n\nAdd up to {MAX_IG_PER_USER} accounts."
    else:
        text = f"📸 <b>Your IG IDs ({len(igs)}/{MAX_IG_PER_USER})</b>\n\n"
        for ig in igs:
            status = "🟢" if ig["username"] in IG_TASKS else "🔴"
            sent = ig.get("stats", {}).get("sent", 0)
            errs = ig.get("stats", {}).get("errors_current", 0)
            proxy_short = esc((ig.get("proxy") or "none")[:25])
            text += (
                f"{status} <b>@{esc(ig['username'])}</b>\n"
                f"   ├ GC: <code>{esc(ig['gc_name'][:35])}</code>\n"
                f"   ├ Sent: <code>{sent}</code> | Errors: <code>{errs}</code>\n"
                f"   └ Proxy: <code>{proxy_short}</code>\n\n"
            )

    kb = [[InlineKeyboardButton("➕ Add New IG", callback_data="add_ig")]]
    for ig in igs:
        safe_uname = ig["username"].replace(":", "_")
        if ig["username"] in IG_TASKS:
            kb.append([InlineKeyboardButton(f"⏹ Stop @{ig['username'][:20]}", callback_data=f"stop_ig:{safe_uname}")])
        else:
            kb.append([InlineKeyboardButton(f"▶️ Start @{ig['username'][:20]}", callback_data=f"start_ig:{safe_uname}")])
        kb.append([InlineKeyboardButton(f"🗑 Delete @{ig['username'][:20]}", callback_data=f"del_ig:{safe_uname}")])
    kb.append([InlineKeyboardButton("🔙 Menu", callback_data="home")])
    await safe_edit(q, text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_dash(q):
    uid = q.from_user.id
    my_ig = [ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid]
    running = [ig for ig in my_ig if ig["username"] in IG_TASKS]
    total_sent = sum(ig.get("stats", {}).get("sent", 0) for ig in my_ig)
    text = (
        f"📊 <b>Dashboard</b>\n\n"
        f"<b>Your Activity:</b>\n"
        f"📸 IG IDs: <code>{len(my_ig)}/{MAX_IG_PER_USER}</code>\n"
        f"🟢 Running: <code>{len(running)}</code>\n"
        f"📤 Total Sent: <code>{total_sent}</code>\n\n"
        f"<b>Global:</b>\n"
        f"🌐 Proxy Pool: <code>{len(PROXY_POOL)}</code>\n"
        f"🔗 In Use: <code>{len(PROXY_ASSIGN)}</code>\n"
        f"👥 Users: <code>{len(CONFIG['users'])}</code>\n"
        f"📱 All Bots: <code>{len(CONFIG['bots'])}</code>\n"
        f"📸 All IG: <code>{len(CONFIG['ig_ids'])}</code>"
    )
    await safe_edit(q, text, reply_markup=back_menu())

# ================= OWNER =================
async def cb_owner(q):
    await safe_edit(q, "👑 <b>Owner Panel</b>\n\nManage hosted bots, broadcast, stats.", reply_markup=owner_menu())

async def cb_owner_listbots(q):
    if q.from_user.id != OWNER_ID:
        await q.answer("Owner only!", show_alert=True)
        return
    await q.answer()
    text = f"📋 <b>All Hosted Bots ({len(CONFIG['bots'])})</b>\n\n"
    for i, b in enumerate(CONFIG["bots"], 1):
        main_tag = " 👑" if b.get("is_main") else ""
        status = "🟢" if b["token"] in TG_APPS else "🔴"
        bot_id = b["token"].split(":")[0]
        text += f"{i}. {status} <code>{esc(bot_id)}</code>{main_tag}\n"
    await safe_edit(q, text, reply_markup=back_owner())

async def cb_owner_addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("Owner only!", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    await safe_edit(q, "➕ <b>Host New Bot</b>\n\nSend bot token:\n\n/cancel to abort.", reply_markup=back_owner())
    return S_ADD_BOT

async def owner_addbot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return ConversationHandler.END
    token = update.message.text.strip()

    if not is_valid_token(token):
        await update.message.reply_text("❌ Invalid token format. Try again or /cancel.")
        return S_ADD_BOT

    if any(b["token"] == token for b in CONFIG["bots"]):
        await update.message.reply_text("⚠️ Already hosted!", reply_markup=main_menu(uid))
        return ConversationHandler.END

    ok = await start_bot(token, OWNER_ID, is_main=False)
    if ok:
        CONFIG["bots"].append({
            "token": token, "owner_id": OWNER_ID,
            "added_at": datetime.now().isoformat(), "is_main": False
        })
        save_config()
        bot_id = token.split(":")[0]
        await update.message.reply_text(f"✅ Hosted! Bot ID: <code>{esc(bot_id)}</code>", reply_markup=main_menu(uid), parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Start failed. Token invalid or bot offline?", reply_markup=main_menu(uid))
    return ConversationHandler.END

async def cb_owner_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("Owner only!", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    await safe_edit(q, "📢 <b>Broadcast</b>\n\nSend the message:\n\n/cancel to abort.", reply_markup=back_owner())
    return S_BROADCAST

async def owner_broadcast_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return ConversationHandler.END

    main_app = TG_APPS.get(MAIN_BOT_TOKEN)
    if not main_app:
        await update.message.reply_text("❌ Main bot not running. Cannot broadcast.", reply_markup=main_menu(OWNER_ID))
        return ConversationHandler.END

    msg = update.message.text
    sent, failed = 0, 0
    for uid_str in list(CONFIG["users"].keys()):
        try:
            await main_app.bot.send_message(int(uid_str), f"📢 <b>Broadcast</b>\n\n{esc(msg)}", parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Sent: {sent}, Failed: {failed}", reply_markup=main_menu(OWNER_ID))
    return ConversationHandler.END

# ================= ADD TG =================
async def cb_add_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if user_tg_count(uid) >= MAX_TG_PER_USER:
        await safe_edit(q, f"❌ <b>Dota Capacity Reached</b>\n\nMax {MAX_TG_PER_USER} TG bots.\nRemove one first.", reply_markup=back_menu())
        return ConversationHandler.END
    await safe_edit(q, "📱 <b>Add TG Bot</b>\n\nSend bot token from @BotFather.\n\n/cancel to abort.", reply_markup=back_menu())
    return S_TG_TOKEN

async def tg_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    token = update.message.text.strip()

    if not is_valid_token(token):
        await update.message.reply_text("❌ Invalid token format. Try again or /cancel.")
        return S_TG_TOKEN
    if any(b["token"] == token for b in CONFIG["bots"]):
        await update.message.reply_text("⚠️ Already added!", reply_markup=main_menu(uid))
        return ConversationHandler.END

    ok = await start_bot(token, uid, is_main=False)
    if ok:
        CONFIG["bots"].append({
            "token": token, "owner_id": uid,
            "added_at": datetime.now().isoformat(), "is_main": False
        })
        save_config()
        bot_id = token.split(":")[0]
        await update.message.reply_text(
            f"✅ <b>TG Bot Added!</b>\nBot ID: <code>{esc(bot_id)}</code>\nHosted: <code>{user_tg_count(uid)}/{MAX_TG_PER_USER}</code>",
            reply_markup=main_menu(uid), parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Start failed. Token invalid?", reply_markup=main_menu(uid))
    return ConversationHandler.END

# ================= ADD IG =================
async def cb_add_ig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if user_ig_count(uid) >= MAX_IG_PER_USER:
        await safe_edit(q, f"❌ <b>Dota Capacity Reached</b>\n\nMax {MAX_IG_PER_USER} IG IDs.\nDelete one first.", reply_markup=back_menu())
        return ConversationHandler.END
    TEMP[uid] = {}
    await safe_edit(q, "📸 <b>Add IG ID — Step 1/4</b>\n\nSend IG <b>username</b>:\n\n/cancel to abort.", reply_markup=back_menu())
    return S_IG_USER

async def ig_user_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP:
        TEMP[uid] = {}
    uname = update.message.text.strip().lstrip("@")
    if not uname:
        await update.message.reply_text("❌ Username can't be empty. Try again or /cancel.")
        return S_IG_USER
    TEMP[uid]["username"] = uname
    await update.message.reply_text(
        f"📸 <b>Step 2/4</b>\n\nUsername: <code>@{esc(uname)}</code>\n\nSend <b>password</b>:",
        parse_mode="HTML"
    )
    return S_IG_PASS

async def ig_pass_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP:
        await update.message.reply_text("⚠️ Session expired. Use /start.", reply_markup=main_menu(uid))
        return ConversationHandler.END

    pwd = update.message.text.strip()
    if not pwd:
        await update.message.reply_text("❌ Password can't be empty. Try again or /cancel.")
        return S_IG_PASS

    TEMP[uid]["password"] = pwd
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("⏳ Logging into Instagram...")
    cl = make_safe_ig_client()
    try:
        await asyncio.to_thread(cl.login, TEMP[uid]["username"], pwd)
        TEMP[uid]["client"] = cl
        await msg.edit_text("✅ Login success! Fetching GCs...")
        return await show_gcs(msg, uid)
    except TwoFactorRequired:
        TEMP[uid]["client"] = cl
        await msg.edit_text("🔐 <b>Step 3/4 — 2FA</b>\n\nSend 2FA code (6 or 8 digit):", parse_mode="HTML")
        return S_IG_2FA
    except ChallengeRequired:
        await msg.edit_text("⚠️ Challenge. Approve in IG app, then /start again.")
        TEMP.pop(uid, None)
        return ConversationHandler.END
    except BadPassword:
        await msg.edit_text("❌ Wrong password. /cancel to abort.")
        TEMP.pop(uid, None)
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ Login failed: {esc(type(e).__name__)}. /cancel to abort.", parse_mode="HTML")
        TEMP.pop(uid, None)
        return ConversationHandler.END

async def ig_2fa_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP or "client" not in TEMP[uid]:
        await update.message.reply_text("⚠️ Session expired. Use /start.", reply_markup=main_menu(uid))
        return ConversationHandler.END

    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("⏳ Verifying 2FA...")
    cl = TEMP[uid]["client"]
    try:
        await asyncio.to_thread(cl.login, TEMP[uid]["username"], TEMP[uid]["password"], verification_code=code)
        await msg.edit_text("✅ 2FA success! Fetching GCs...")
        return await show_gcs(msg, uid)
    except Exception as e:
        await msg.edit_text(f"❌ 2FA failed: {esc(type(e).__name__)}. /cancel to abort.", parse_mode="HTML")
        TEMP.pop(uid, None)
        return ConversationHandler.END

async def show_gcs(msg, uid):
    if uid not in TEMP or "client" not in TEMP[uid]:
        await msg.edit_text("⚠️ Session expired.")
        return ConversationHandler.END
    cl = TEMP[uid]["client"]
    try:
        threads = await asyncio.to_thread(cl.direct_threads, 10)
    except Exception as e:
        await msg.edit_text(f"❌ Couldn't fetch GCs: {esc(type(e).__name__)}", parse_mode="HTML")
        TEMP.pop(uid, None)
        return ConversationHandler.END
    if not threads:
        await msg.edit_text("❌ No GCs/DMs. Open a chat in IG app first.")
        TEMP.pop(uid, None)
        return ConversationHandler.END

    TEMP[uid]["gcs"] = {}
    kb = []
    for t in threads:
        title = t.thread_title or f"GC {t.id}"
        TEMP[uid]["gcs"][str(t.id)] = title
        kb.append([InlineKeyboardButton(f"📁 {title[:40]}", callback_data=f"gc:{t.id}")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_ig")])

    await msg.edit_text(
        f"📸 <b>Step 4/4 — Choose GC</b>\n\nFound {len(threads)}:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )
    return S_IG_TEXT

async def cb_gc_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "cancel_ig":
        TEMP.pop(uid, None)
        await show_home(q)
        return ConversationHandler.END

    if uid not in TEMP or "gcs" not in TEMP[uid]:
        await q.answer("Session expired", show_alert=True)
        return ConversationHandler.END

    gc_id = q.data.split(":", 1)[1] if ":" in q.data else ""
    if gc_id not in TEMP[uid]["gcs"]:
        await q.answer("GC not found", show_alert=True)
        return ConversationHandler.END

    TEMP[uid]["gc_id"] = gc_id
    TEMP[uid]["gc_name"] = TEMP[uid]["gcs"][gc_id]
    await safe_edit(q, f"✅ GC: <b>{esc(TEMP[uid]['gc_name'])}</b>\n\n💬 Send the spam text:", reply_markup=None)
    return S_IG_TEXT

async def ig_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("❌ Text can't be empty. Send text or /cancel.")
        return S_IG_TEXT

    data = TEMP.get(uid)
    if not data or "gc_id" not in data or "client" not in data:
        await update.message.reply_text("⚠️ Session expired. Use /start.", reply_markup=main_menu(uid))
        return ConversationHandler.END

    cl = data["client"]
    ig_entry = {
        "username": data["username"],
        "password": data["password"],
        "settings": cl.get_settings(),
        "owner_id": uid,
        "gc_id": data["gc_id"],
        "gc_name": data["gc_name"],
        "text": text,
        "running": True,
        "proxy": None,
        "added_at": datetime.now().isoformat(),
        "stats": {"sent": 0, "errors_total": 0, "errors_current": 0}
    }
    CONFIG["ig_ids"].append(ig_entry)
    save_config()
    TEMP.pop(uid, None)

    await update.message.reply_text(
        f"✅ <b>IG @{esc(ig_entry['username'])} Added!</b>\n\n"
        f"📁 GC: <code>{esc(ig_entry['gc_name'])}</code>\n"
        f"▶️ Starting loop...",
        reply_markup=main_menu(uid), parse_mode="HTML"
    )

    IG_TASKS[ig_entry["username"]] = asyncio.create_task(ig_loop(ig_entry, context.application))
    return ConversationHandler.END

# ================= CONTROLS =================
async def cb_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 1)
    if len(parts) != 2:
        await q.answer("Invalid", show_alert=True)
        return
    action, safe_uname = parts
    uid = q.from_user.id

    ig = next((ig for ig in CONFIG["ig_ids"]
               if ig["username"].replace(":", "_") == safe_uname and ig["owner_id"] == uid), None)
    if not ig:
        await q.answer("Not found", show_alert=True)
        return

    await q.answer()

    username = ig["username"]
    if action == "stop_ig":
        task = IG_TASKS.pop(username, None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        ig["running"] = False
        save_config()
        await release_proxy_for(username)
        await cb_ig_view(q)
    elif action == "start_ig":
        existing = IG_TASKS.get(username)
        if existing and not existing.done():
            pass
        else:
            if existing:
                IG_TASKS.pop(username, None)
            ig["running"] = True
            save_config()
            IG_TASKS[username] = asyncio.create_task(ig_loop(ig, context.application))
        await cb_ig_view(q)
    elif action == "del_ig":
        task = IG_TASKS.pop(username, None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        await release_proxy_for(username)
        CONFIG["ig_ids"] = [x for x in CONFIG["ig_ids"] if not (x["username"] == username and x["owner_id"] == uid)]
        save_config()
        await cb_ig_view(q)

async def cb_del_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 1)
    if len(parts) != 2:
        await q.answer("Invalid", show_alert=True)
        return
    bot_id = parts[1]
    uid = q.from_user.id
    to_del = next((b for b in CONFIG["bots"] if b["owner_id"] == uid and b["token"].split(":")[0] == bot_id and not b.get("is_main")), None)
    if not to_del:
        await q.answer("Not found", show_alert=True)
        return
    await q.answer()
    if to_del["token"] in TG_APPS:
        try:
            await TG_APPS[to_del["token"]].updater.stop()
            await TG_APPS[to_del["token"]].stop()
            del TG_APPS[to_del["token"]]
        except Exception:
            pass
    CONFIG["bots"] = [b for b in CONFIG["bots"] if b["token"] != to_del["token"]]
    save_config()
    await cb_tg_view(q)

async def cb_clean(q):
    kb = [
        [InlineKeyboardButton("⚠️ Yes, DELETE", callback_data="clean_yes")],
        [InlineKeyboardButton("🔙 No, Go Back", callback_data="home")],
    ]
    await safe_edit(q, "⚠️ <b>Delete ALL your data?</b>\n\n• All IG IDs\n• All your hosted bots\n• All stats\n\n<b>Cannot be undone.</b>", reply_markup=InlineKeyboardMarkup(kb))

async def cb_clean_yes(q):
    uid = q.from_user.id

    for ig in [x for x in CONFIG["ig_ids"] if x["owner_id"] == uid]:
        task = IG_TASKS.pop(ig["username"], None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        await release_proxy_for(ig["username"])

    for b in [x for x in CONFIG["bots"] if x["owner_id"] == uid and not x.get("is_main")]:
        if b["token"] in TG_APPS:
            try:
                await TG_APPS[b["token"]].updater.stop()
                await TG_APPS[b["token"]].stop()
                del TG_APPS[b["token"]]
            except Exception:
                pass

    CONFIG["ig_ids"] = [x for x in CONFIG["ig_ids"] if x["owner_id"] != uid]
    CONFIG["bots"] = [x for x in CONFIG["bots"] if x["owner_id"] != uid or x.get("is_main")]
    CONFIG["users"].pop(str(uid), None)
    save_config()

    await safe_edit(q, "🧹 Cleaned! Fresh start.", reply_markup=main_menu(uid))

# ================= WIZARD EXIT =================
async def cmd_exit_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    TEMP.pop(uid, None)
    await q.answer()
    d = q.data
    if d == "home":
        await show_home(q)
    elif d == "tg_view":
        await cb_tg_view(q)
    elif d == "ig_view":
        await cb_ig_view(q)
    elif d == "dash":
        await cb_dash(q)
    elif d == "clean":
        await cb_clean(q)
    elif d == "owner":
        await cb_owner(q)
    return ConversationHandler.END

# ================= ROUTER =================
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data

    await q.answer()

    if d == "home":
        await show_home(q)
    elif d == "tg_view":
        await cb_tg_view(q)
    elif d == "ig_view":
        await cb_ig_view(q)
    elif d == "dash":
        await cb_dash(q)
    elif d == "clean":
        await cb_clean(q)
    elif d == "clean_yes":
        await cb_clean_yes(q)
    elif d == "owner":
        await cb_owner(q)
    elif d == "owner_listbots":
        # Bug #27 fix: call function directly (it answers itself)
        await cb_owner_listbots(q)

# ================= BOT BUILDER =================
def build_app(token, owner_id, is_main=False):
    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_add_tg, pattern="^add_tg$"),
            CallbackQueryHandler(cb_add_ig, pattern="^add_ig$"),
            CallbackQueryHandler(cb_gc_pick, pattern="^(gc:\\d+|cancel_ig)$"),
            CallbackQueryHandler(cb_owner_addbot, pattern="^owner_addbot$"),
            CallbackQueryHandler(cb_owner_broadcast, pattern="^owner_broadcast$"),
        ],
        states={
            S_TG_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, tg_token_received)],
            S_IG_USER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ig_user_received)],
            S_IG_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ig_pass_received)],
            S_IG_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ig_2fa_received)],
            S_IG_TEXT:  [
                CallbackQueryHandler(cb_gc_pick, pattern="^(gc:\\d+|cancel_ig)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ig_text_received),
            ],
            S_ADD_BOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, owner_addbot_received)],
            S_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, owner_broadcast_received)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_cancel),
            CallbackQueryHandler(cmd_exit_wizard, pattern="^(home|tg_view|ig_view|dash|clean|owner)$"),
        ],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_router, pattern="^(home|tg_view|ig_view|dash|clean|clean_yes|owner|owner_listbots)$"))
    app.add_handler(CallbackQueryHandler(cb_control, pattern="^(stop_ig|start_ig|del_ig):.+$"))
    app.add_handler(CallbackQueryHandler(cb_del_tg, pattern="^del_tg:.+$"))
    return app

async def start_bot(token, owner_id, is_main=False):
    if token in TG_APPS:
        return True
    app = None
    try:
        app = build_app(token, owner_id, is_main)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        TG_APPS[token] = app
        return True
    except Exception as e:
        print(f"[BOT] {token[:15]}... fail: {type(e).__name__}: {e}")
        if app:
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass
            try:
                await app.shutdown()
            except Exception:
                pass
        return False

# ================= MAIN =================
async def main():
    global STOP_FLAG, GITHUB_USER, REPO_NAME

    cleanup_session_files()

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config(), f, indent=2)
    load_config()

    main_entries = [b for b in CONFIG["bots"] if b.get("is_main")]
    if not main_entries:
        CONFIG["bots"].insert(0, {
            "token": MAIN_BOT_TOKEN, "owner_id": OWNER_ID,
            "added_at": datetime.now().isoformat(), "is_main": True
        })
        save_config()
    elif main_entries[0].get("token") != MAIN_BOT_TOKEN:
        main_entries[0]["token"] = MAIN_BOT_TOKEN
        save_config()

    print("🚀 Dota Bot starting...")

    asyncio.create_task(proxy_fetcher())
    asyncio.create_task(gh_sync_loop())
    await gh_bootstrap()

    await start_bot(MAIN_BOT_TOKEN, OWNER_ID, is_main=True)

    for b in CONFIG["bots"]:
        if not b.get("is_main"):
            await start_bot(b["token"], b["owner_id"], is_main=False)

    for ig in CONFIG["ig_ids"]:
        if ig.get("running"):
            owner_app = find_owner_app(ig["owner_id"])
            if owner_app:
                IG_TASKS[ig["username"]] = asyncio.create_task(ig_loop(ig, owner_app))
            else:
                print(f"[IG] {ig['username']} running but no owner app — setting stopped")
                ig["running"] = False
                save_config()

    print(f"✅ {len(TG_APPS)} bots running, {len(CONFIG['ig_ids'])} IG IDs loaded")

    try:
        while not STOP_FLAG:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        STOP_FLAG = True
        for task in list(IG_TASKS.values()):
            task.cancel()
        for app in list(TG_APPS.values()):
            try:
                await app.updater.stop()
                await app.stop()
            except Exception:
                pass
        if GH_SESSION:
            try:
                await GH_SESSION.close()
            except Exception:
                pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass