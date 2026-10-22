#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, subprocess, importlib, asyncio, json, base64, re, secrets, string, random, warnings, time, html, glob, shutil
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

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    httpx = None

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

S_TG_TOKEN, S_IG_USER, S_IG_PASS, S_IG_2FA, S_IG_TEXT, S_IG_CHALLENGE = range(6)
S_ADD_BOT, S_BROADCAST = range(6, 8)

# ================= GLOBAL =================
CONFIG = {}
CONFIG_LOCK = asyncio.Lock()
PROXY_POOL = set()
PROXY_ASSIGN = {}
PROXY_LOCK = asyncio.Lock()
IG_TASKS = {}
IG_CLIENTS = {}
TEMP = {}
TEMP_TS = {}
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
    return html.escape(str(s)) if s else ""

def is_valid_token(token):
    if not token or ":" not in token: return False
    try:
        bot_id, auth = token.split(":", 1)
        return bot_id.isdigit() and len(auth) >= 30
    except: return False

def make_safe_ig_client():
    """Bug #1 Fix: No terminal OTP prompts"""
    cl = IGClient()
    def _no_prompt(*args, **kwargs): return None
    for attr in ("challenge_code_handler", "change_password_handler",
                 "two_factor_handler", "verification_code_handler",
                 "totp_code_handler"):
        try: setattr(cl, attr, _no_prompt)
        except: pass
    return cl

def cleanup_session_files():
    """Bug #72 Fix: Restrict to CWD"""
    cwd = os.getcwd()
    for pattern in ("session_*.json", "*.session"):
        for f in glob.glob(os.path.join(cwd, pattern)):
            if os.path.dirname(f) == cwd:
                try: os.remove(f)
                except: pass

def is_network_error(e):
    """Bug #31 Fix: Check httpx properly"""
    if HAS_HTTPX and isinstance(e, (httpx.ProxyError, httpx.ConnectError, httpx.TimeoutException)):
        return True
    msg = str(e).lower()
    keywords = ("proxy", "connect", "timeout", "tunnel", "reset", "10054", "10060", "eof", "ssl", "unreachable")
    return any(k in msg for k in keywords)

async def safe_edit(q, text, *, reply_markup=None, parse_mode="HTML"):
    """Bug #59 Fix: Truncate long messages"""
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>... truncated</i>"
    try:
        await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try: await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except: pass

def update_temp(uid):
    TEMP_TS[uid] = time.time()

# ================= CONFIG & DATA =================
def default_config():
    return {"version": 7, "last_sync": None, "github_user": "", "github_repo": "", "bots": [], "users": {}, "ig_ids": []}

def load_config():
    """Bug #71 Fix: Backup corrupt file"""
    global CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        try: shutil.copy(CONFIG_FILE, CONFIG_FILE + ".corrupt.bak")
        except: pass
        CONFIG = default_config()
        save_config()
        return

    if not isinstance(loaded, dict): loaded = {}
    base = default_config()
    for key, default_val in base.items():
        if key not in loaded: loaded[key] = default_val

    if not isinstance(loaded.get("bots"), list): loaded["bots"] = []
    if not isinstance(loaded.get("ig_ids"), list): loaded["ig_ids"] = []
    if not isinstance(loaded.get("users"), dict): loaded["users"] = {}

    CONFIG = loaded
    save_config()

def save_config():
    """Bug #70 Fix: Atomic write"""
    global GH_DIRTY
    CONFIG["last_sync"] = datetime.now().isoformat()
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        GH_DIRTY = True
    except Exception:
        try: os.remove(tmp)
        except: pass

def save_stats_only():
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except: pass

def user_tg_count(uid): return len([b for b in CONFIG["bots"] if b.get("owner_id") == uid and not b.get("is_main")])
def user_ig_count(uid): return len([ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid])

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
    except Exception as e: return None, str(e)

async def gh_post(session, path, payload):
    try:
        async with session.post(f"{GITHUB_API}{path}", json=payload, timeout=15) as r:
            return r.status, await r.text()
    except Exception as e: return None, str(e)

async def gh_put(session, path, payload):
    try:
        async with session.put(f"{GITHUB_API}{path}", json=payload, timeout=15) as r:
            return r.status, await r.text()
    except Exception as e: return None, str(e)

async def gh_push_file(session, path, content, msg="auto"):
    try:
        status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
        sha = json.loads(body).get("sha") if status == 200 else None
        payload = {"message": msg, "content": base64.b64encode(content.encode("utf-8")).decode(), "branch": "main"}
        if sha: payload["sha"] = sha
        rstatus, _ = await gh_put(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}", payload)
        return rstatus in (200, 201)
    except Exception: return False

async def gh_pull_file(session, path):
    try:
        status, body = await gh_get(session, f"/repos/{GITHUB_USER}/{REPO_NAME}/contents/{path}")
        if status == 200:
            return base64.b64decode(json.loads(body)["content"]).decode("utf-8")
    except Exception: pass
    return None

async def gh_bootstrap():
    global GITHUB_USER, REPO_NAME, GH_SESSION
    headers = {"Authorization": f"Bearer {GITHUB_PAT}", "Accept": "application/vnd.github+json", "User-Agent": "DotaBot"}
    GH_SESSION = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), headers=headers)

    try:
        if CONFIG.get("github_user") and CONFIG.get("github_repo"):
            GITHUB_USER = CONFIG["github_user"]
            REPO_NAME = CONFIG["github_repo"]
        else:
            status, body = await gh_get(GH_SESSION, "/user")
            if status != 200: return
            GITHUB_USER = json.loads(body)["login"]
            REPO_NAME = f"dota-bot-{OWNER_ID}" # Bug #54 Fix: Deterministic name
            status, _ = await gh_post(GH_SESSION, "/user/repos", {"name": REPO_NAME, "private": False})
            if status not in (201, 422): return # 422 = exists
            CONFIG["github_user"] = GITHUB_USER
            CONFIG["github_repo"] = REPO_NAME
            save_config()
            await asyncio.sleep(1)

        # Bug #55 Fix: Redact secrets before pushing
        redacted = json.loads(json.dumps(CONFIG))
        for ig in redacted.get("ig_ids", []):
            if ig.get("password"): ig["password"] = "REDACTED"
            if ig.get("settings"): ig["settings"] = {"_redacted": True}
        
        await gh_push_file(GH_SESSION, CONFIG_FILE, json.dumps(redacted, indent=2), "config")

        try:
            with open(__file__, "r", encoding="utf-8") as f: mc = f.read()
            mc = re.sub(r'^(GITHUB_PAT\s*=\s*)".*?"', r'\1"REDACTED"', mc, flags=re.MULTILINE)
            mc = re.sub(r'^(MAIN_BOT_TOKEN\s*=\s*)".*?"', r'\1"REDACTED"', mc, flags=re.MULTILINE)
            await gh_push_file(GH_SESSION, MAIN_FILE, mc, "main")
        except: pass
    except Exception as e:
        print(f"[GH] {type(e).__name__}: {e}")

async def gh_sync_loop():
    global GH_DIRTY
    while not STOP_FLAG:
        try:
            await asyncio.sleep(30)
            if not GH_SESSION or not GITHUB_USER: continue

            if GH_DIRTY:
                redacted = json.loads(json.dumps(CONFIG))
                for ig in redacted.get("ig_ids", []):
                    if ig.get("password"): ig["password"] = "REDACTED"
                    if ig.get("settings"): ig["settings"] = {"_redacted": True}
                if await gh_push_file(GH_SESSION, CONFIG_FILE, json.dumps(redacted, indent=2), "config"):
                    GH_DIRTY = False

            if not IG_TASKS and not GH_DIRTY: # Safe to pull
                remote_text = await gh_pull_file(GH_SESSION, CONFIG_FILE)
                if remote_text:
                    try:
                        rjson = json.loads(remote_text)
                        if (rjson.get("last_sync") or "") > (CONFIG.get("last_sync") or ""):
                            # Merge secrets from local to avoid losing them
                            for r_ig in rjson.get("ig_ids", []):
                                for l_ig in CONFIG.get("ig_ids", []):
                                    if r_ig["username"] == l_ig["username"] and r_ig["owner_id"] == l_ig["owner_id"]:
                                        if r_ig.get("password") == "REDACTED": r_ig["password"] = l_ig.get("password")
                                        if r_ig.get("settings") == {"_redacted": True}: r_ig["settings"] = l_ig.get("settings")
                            
                            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                                json.dump(rjson, f, indent=2)
                            load_config()
                            print("[SYNC] Pulled remote config")
                    except: pass
        except: pass

# ================= PROXY =================
def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"): return None
    if "://" in line: line = line.split("://", 1)[1]
    if "@" in line: return None
    parts = line.split(":")
    if len(parts) == 2 and parts[1].isdigit(): return f"http://{parts[0]}:{parts[1]}"
    return None

async def proxy_fetcher():
    while not STOP_FLAG:
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as s:
                for url in PROXY_SOURCES:
                    try:
                        async with s.get(url, timeout=10) as r:
                            if r.status == 200:
                                for l in (await r.text()).splitlines():
                                    p = parse_proxy(l)
                                    if p:
                                        async with PROXY_LOCK: PROXY_POOL.add(p)
                    except: pass
        except: pass
        await asyncio.sleep(30)

async def proxy_cleaner():
    """Bug #69 Fix: Clean dead proxies"""
    while not STOP_FLAG:
        await asyncio.sleep(120)
        async with PROXY_LOCK:
            sample = list(PROXY_POOL)[:300]
        if not sample: continue
        
        dead = []
        for p in sample:
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as s:
                    async with s.get("https://i.instagram.com/", proxy=p, timeout=5) as r:
                        if r.status >= 500: dead.append(p)
            except: dead.append(p)
            
        if dead:
            async with PROXY_LOCK:
                for p in dead:
                    PROXY_POOL.discard(p)
                    PROXY_ASSIGN.pop(p, None)

async def get_proxy_for(username):
    async with PROXY_LOCK:
        for proxy, igs in PROXY_ASSIGN.items():
            if username in igs: return proxy
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
                if not PROXY_ASSIGN[proxy]: del PROXY_ASSIGN[proxy]
                return

# ================= TEMP CLEANUP =================
async def temp_cleanup_loop():
    """Bug #61 Fix: Memory leak prevent"""
    while not STOP_FLAG:
        await asyncio.sleep(300)
        now = time.time()
        for uid in list(TEMP.keys()):
            if now - TEMP_TS.get(uid, 0) > 900: # 15 min inactivity
                TEMP.pop(uid, None)
                TEMP_TS.pop(uid, None)

# ================= IG LOOP =================
async def ig_loop(ig, app):
    username = ig["username"]
    owner_id = ig["owner_id"]

    async def notify(text):
        try: await app.bot.send_message(owner_id, text, parse_mode="HTML")
        except: pass

    consecutive_errors = 0
    cl = make_safe_ig_client()

    try:
        if ig.get("settings"): cl.set_settings(ig["settings"])

        session_ok = False
        try:
            await asyncio.to_thread(cl.user_info_v1, cl.user_id) # Bug #68 Fix: Force network check
            session_ok = True
        except (LoginRequired, TwoFactorRequired): session_ok = False
        except Exception: session_ok = True # Assume network issue

        if not session_ok:
            if not ig.get("password"):
                await notify(f"❌ @{esc(username)} session dead, no password. Delete & re-add.")
                ig["running"] = False; save_config(); return
            try:
                await asyncio.to_thread(cl.login, username, ig["password"])
                ig["settings"] = cl.get_settings(); save_config()
            except ChallengeRequired:
                await notify(f"⚠️ @{esc(username)} Challenge required. Delete & re-add.")
                ig["running"] = False; save_config(); return
            except TwoFactorRequired:
                await notify(f"🔐 @{esc(username)} 2FA needed. Delete & re-add.")
                ig["running"] = False; save_config(); return
            except Exception as e:
                await notify(f"❌ @{esc(username)} re-login failed: {esc(type(e).__name__)}")
                ig["running"] = False; save_config(); return

        IG_CLIENTS[username] = cl

        proxy = await get_proxy_for(username)
        if not proxy:
            await asyncio.sleep(30)
            proxy = await get_proxy_for(username)
            if not proxy:
                ig["running"] = False; save_config(); return

        ig["proxy"] = proxy; save_config()
        try: await asyncio.to_thread(cl.set_proxy, proxy)
        except: pass

        count = ig.get("stats", {}).get("sent", 0)
        total_errors = ig.get("stats", {}).get("errors_total", 0)

        while not STOP_FLAG and ig.get("running", True):
            try:
                msg = f"{random.randint(1, 10**10)} {ig['text']}" if random.choice([True, False]) else f"{ig['text']} {random.randint(1, 10**10)}"
                await asyncio.to_thread(cl.direct_send, msg, [ig["gc_id"]])
                
                count += 1; consecutive_errors = 0
                ig["stats"] = {"sent": count, "errors_total": total_errors, "errors_current": 0, "last_send": datetime.now().isoformat()}
                save_stats_only()
                await asyncio.sleep(random.uniform(30, 50))

            except RateLimitError:
                consecutive_errors += 1; await asyncio.sleep(120)
            except PleaseWaitFewMinutes:
                consecutive_errors += 1; await asyncio.sleep(600)
            except TwoFactorRequired:
                await notify(f"🔐 @{esc(username)} 2FA needed mid-loop. Stopping.")
                ig["running"] = False; save_config(); return
            except LoginRequired:
                try:
                    await asyncio.to_thread(cl.login, username, ig["password"])
                    ig["settings"] = cl.get_settings(); save_config()
                    continue
                except ChallengeRequired:
                    await notify(f"⚠️ @{esc(username)} Challenge required mid-loop. Delete & re-add.")
                    ig["running"] = False; save_config(); return
                except Exception as e:
                    await notify(f"❌ @{esc(username)} re-login failed: {esc(type(e).__name__)}")
                    ig["running"] = False; save_config(); return
            except ClientForbiddenError:
                await notify(f"🚫 @{esc(username)} forbidden. Stopping.")
                ig["running"] = False; save_config(); return
            except ClientError as e:
                msg_lower = str(e).lower()
                if "thread" in msg_lower and "not found" in msg_lower:
                    await notify(f"❌ @{esc(username)} GC not found. Delete & re-add.")
                    ig["running"] = False; save_config(); return

                total_errors += 1; consecutive_errors += 1
                ig["stats"]["errors_total"] = total_errors; ig["stats"]["errors_current"] = consecutive_errors
                save_stats_only()

                if is_network_error(e):
                    await release_proxy_for(username)
                    new_proxy = await get_proxy_for(username)
                    if new_proxy:
                        ig["proxy"] = new_proxy; save_config()
                        try: await asyncio.to_thread(cl.set_proxy, new_proxy)
                        except: pass
                        await asyncio.sleep(5); continue
                    else:
                        await asyncio.sleep(30); continue

                await asyncio.sleep(min(30 * (2 ** min(consecutive_errors - 1, 4)), 600))
            except Exception as e:
                total_errors += 1; consecutive_errors += 1
                ig["stats"]["errors_total"] = total_errors; ig["stats"]["errors_current"] = consecutive_errors
                save_stats_only()

                if is_network_error(e) or consecutive_errors >= 3:
                    await release_proxy_for(username)
                    new_proxy = await get_proxy_for(username)
                    if new_proxy:
                        ig["proxy"] = new_proxy; save_config()
                        try: await asyncio.to_thread(cl.set_proxy, new_proxy)
                        except: pass
                        consecutive_errors = 0
                        await asyncio.sleep(5); continue
                    else:
                        await asyncio.sleep(30); continue

                await asyncio.sleep(30)

    except asyncio.CancelledError: pass
    except Exception as e:
        await notify(f"❌ @{esc(username)} fatal: {esc(type(e).__name__)}")
        ig["running"] = False; save_config()
    finally:
        await release_proxy_for(username)
        IG_CLIENTS.pop(username, None)
        IG_TASKS.pop(username, None)

# ================= MENUS & VIEWS =================
def main_menu(uid):
    rows = [
        [InlineKeyboardButton("📱 My TG Bots", callback_data="tg_view"), InlineKeyboardButton("📸 My IG IDs", callback_data="ig_view")],
        [InlineKeyboardButton("➕ Add TG Bot", callback_data="add_tg"), InlineKeyboardButton("➕ Add IG ID", callback_data="add_ig")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dash"), InlineKeyboardButton("🧹 Clean All", callback_data="clean")],
    ]
    if uid == OWNER_ID: rows.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner")])
    return InlineKeyboardMarkup(rows)

def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Host New Bot", callback_data="owner_addbot")],
        [InlineKeyboardButton("📋 List All Bots", callback_data="owner_listbots")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast")],
        [InlineKeyboardButton("📊 Global Stats", callback_data="dash")],
        [InlineKeyboardButton("🔙 Back", callback_data="home")],
    ])

def back_menu(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="home")]])
def back_owner(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Owner Panel", callback_data="owner")]])

async def show_home(q):
    uid = q.from_user.id
    text = f"💗 <b>Dota Bot</b>\n\nWelcome, <b>{esc(q.from_user.first_name)}</b>!\n\n<b>Your Limits:</b>\n📱 TG Bots: <code>{user_tg_count(uid)}/{MAX_TG_PER_USER}</code>\n📸 IG IDs: <code>{user_ig_count(uid)}/{MAX_IG_PER_USER}</code>"
    await safe_edit(q, text, reply_markup=main_menu(uid))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) not in CONFIG["users"]:
        CONFIG["users"][str(uid)] = {"first_seen": datetime.now().isoformat()}
        save_config()
    await show_home(update.message) # Can't edit message, use reply logic inside

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    TEMP.pop(update.effective_user.id, None)
    await update.message.reply_text("🛑 Cancelled.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END

async def cb_tg_view(q):
    uid = q.from_user.id
    tokens = [b for b in CONFIG["bots"] if b.get("owner_id") == uid and not b.get("is_main")]
    text = f"📱 <b>Your TG Bots ({len(tokens)}/{MAX_TG_PER_USER})</b>\n\n" if tokens else "📱 <b>No TG Bots</b>"
    kb = [[InlineKeyboardButton("➕ Add New Bot", callback_data="add_tg")]]
    for t in tokens:
        bot_id = t["token"].split(":")[0]
        text += f"Bot ID: <code>{esc(bot_id)}</code>\n"
        kb.append([InlineKeyboardButton(f"🗑 Remove {bot_id}", callback_data=f"del_tg:{bot_id}")])
    kb.append([InlineKeyboardButton("🔙 Menu", callback_data="home")])
    await safe_edit(q, text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_ig_view(q):
    uid = q.from_user.id
    igs = [ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid]
    text = f"📸 <b>Your IG IDs ({len(igs)}/{MAX_IG_PER_USER})</b>\n\n" if igs else "📸 <b>No IG IDs</b>"
    kb = [[InlineKeyboardButton("➕ Add New IG", callback_data="add_ig")]]
    for idx, ig in enumerate(igs):
        status = "🟢" if ig["username"] in IG_TASKS else "🔴"
        text += f"{status} <b>@{esc(ig['username'])}</b>\n   ├ Sent: <code>{ig.get('stats', {}).get('sent', 0)}</code>\n   └ Proxy: <code>{esc((ig.get('proxy') or 'none')[:20])}</code>\n\n"
        # Bug #58 Fix: Index instead of username for callback_data
        if ig["username"] in IG_TASKS:
            kb.append([InlineKeyboardButton(f"⏹ Stop @{ig['username'][:15]}", callback_data=f"stop_ig:{idx}")])
        else:
            kb.append([InlineKeyboardButton(f"▶️ Start @{ig['username'][:15]}", callback_data=f"start_ig:{idx}")])
        kb.append([InlineKeyboardButton(f"🗑 Delete @{ig['username'][:15]}", callback_data=f"del_ig:{idx}")])
    kb.append([InlineKeyboardButton("🔙 Menu", callback_data="home")])
    await safe_edit(q, text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_dash(q):
    uid = q.from_user.id
    my_ig = [ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid]
    running = [ig for ig in my_ig if ig["username"] in IG_TASKS]
    text = f"📊 <b>Dashboard</b>\n\n<b>Your Activity:</b>\n📸 IG IDs: <code>{len(my_ig)}/{MAX_IG_PER_USER}</code>\n🟢 Running: <code>{len(running)}</code>\n📤 Total Sent: <code>{sum(ig.get('stats', {}).get('sent', 0) for ig in my_ig)}</code>\n\n<b>Global:</b>\n🌐 Proxy Pool: <code>{len(PROXY_POOL)}</code>\n🔗 In Use: <code>{len(PROXY_ASSIGN)}</code>\n👥 Users: <code>{len(CONFIG['users'])}</code>"
    await safe_edit(q, text, reply_markup=back_menu())

async def cb_owner(q):
    await safe_edit(q, "👑 <b>Owner Panel</b>", reply_markup=owner_menu())

async def cb_owner_listbots(q):
    if q.from_user.id != OWNER_ID: return await q.answer("Owner only!", show_alert=True)
    text = f"📋 <b>All Hosted Bots ({len(CONFIG['bots'])})</b>\n\n"
    for i, b in enumerate(CONFIG["bots"], 1):
        text += f"{i}. {'🟢' if b['token'] in TG_APPS else '🔴'} <code>{esc(b['token'].split(':')[0])}</code>{' 👑' if b.get('is_main') else ''}\n"
    await safe_edit(q, text, reply_markup=back_owner())
    await q.answer()

# ================= WIZARD FLOWS =================
async def cb_add_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if user_tg_count(q.from_user.id) >= MAX_TG_PER_USER: return await safe_edit(q, "❌ Capacity Reached", reply_markup=back_menu())
    await safe_edit(q, "📱 <b>Add TG Bot</b>\n\nSend bot token:\n\n/cancel to abort.", reply_markup=back_menu())
    update_temp(q.from_user.id)
    return S_TG_TOKEN

async def tg_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    token = update.message.text.strip()
    if not is_valid_token(token): return await update.message.reply_text("❌ Invalid token. Try again or /cancel.")
    
    ok = await start_bot(token, uid)
    if ok:
        CONFIG["bots"].append({"token": token, "owner_id": uid, "added_at": datetime.now().isoformat(), "is_main": False})
        save_config()
        await update.message.reply_text("✅ Hosted!", reply_markup=main_menu(uid))
    else:
        await update.message.reply_text("❌ Start failed.", reply_markup=main_menu(uid))
    return ConversationHandler.END

async def cb_add_ig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if user_ig_count(q.from_user.id) >= MAX_IG_PER_USER: return await safe_edit(q, "❌ Capacity Reached", reply_markup=back_menu())
    TEMP[q.from_user.id] = {}
    update_temp(q.from_user.id)
    await safe_edit(q, "📸 <b>Step 1/4</b>\n\nSend IG username:\n\n/cancel to abort.", reply_markup=back_menu())
    return S_IG_USER

async def ig_user_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP: return await update.message.reply_text("⚠️ Expired. /start", reply_markup=main_menu(uid))
    TEMP[uid]["username"] = update.message.text.strip().lstrip("@")
    update_temp(uid)
    await update.message.reply_text("📸 <b>Step 2/4</b>\n\nSend password:", parse_mode="HTML")
    return S_IG_PASS

async def ig_pass_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP: return await update.message.reply_text("⚠️ Expired. /start", reply_markup=main_menu(uid))
    TEMP[uid]["password"] = update.message.text.strip()
    update_temp(uid)
    try: await update.message.delete()
    except: pass
    
    msg = await update.message.reply_text("⏳ Logging in...")
    cl = make_safe_ig_client()
    try:
        await asyncio.to_thread(cl.login, TEMP[uid]["username"], TEMP[uid]["password"])
        TEMP[uid]["client"] = cl
        await msg.edit_text("✅ Success! Fetching GCs...")
        return await show_gcs(msg, uid)
    except TwoFactorRequired:
        TEMP[uid]["client"] = cl
        await msg.edit_text("🔐 <b>Step 3/4: 2FA</b>\n\nSend 2FA code:", parse_mode="HTML")
        return S_IG_2FA
    except ChallengeRequired as e:
        TEMP[uid]["client"] = cl
        await msg.edit_text("🔐 <b>Challenge!</b>\n\nSend OTP from Email/SMS:", parse_mode="HTML")
        return S_IG_CHALLENGE
    except Exception as e:
        TEMP.pop(uid, None)
        await msg.edit_text(f"❌ Login failed: {esc(type(e).__name__)}", parse_mode="HTML")
        return ConversationHandler.END

async def ig_2fa_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP: return await update.message.reply_text("⚠️ Expired. /start", reply_markup=main_menu(uid))
    code = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    
    msg = await update.message.reply_text("⏳ Verifying...")
    cl = TEMP[uid]["client"]
    try:
        await asyncio.to_thread(cl.login, TEMP[uid]["username"], TEMP[uid]["password"], verification_code=code)
        await msg.edit_text("✅ Success! Fetching GCs...")
        return await show_gcs(msg, uid)
    except Exception as e:
        TEMP.pop(uid, None)
        await msg.edit_text(f"❌ Failed: {esc(type(e).__name__)}", parse_mode="HTML")
        return ConversationHandler.END

async def ig_challenge_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in TEMP: return await update.message.reply_text("⚠️ Expired. /start", reply_markup=main_menu(uid))
    code = update.message.text.strip()
    try: await update.message.delete()
    except: pass
    
    msg = await update.message.reply_text("⏳ Verifying OTP...")
    cl = TEMP[uid]["client"]
    try:
        await asyncio.to_thread(cl.challenge_resolve) # Bug #34 Fix: No args
        await asyncio.to_thread(cl.login, TEMP[uid]["username"], TEMP[uid]["password"])
        await msg.edit_text("✅ Success! Fetching GCs...")
        return await show_gcs(msg, uid)
    except Exception as e:
        TEMP.pop(uid, None)
        await msg.edit_text(f"❌ Failed: {esc(type(e).__name__)}", parse_mode="HTML")
        return ConversationHandler.END

async def show_gcs(msg, uid):
    cl = TEMP[uid]["client"]
    try: threads = await asyncio.to_thread(cl.direct_threads, 10)
    except: return await msg.edit_text("❌ Failed to fetch GCs.")
    if not threads: return await msg.edit_text("❌ No GCs found.")
    
    TEMP[uid]["gcs"] = {}; kb = []
    for t in threads:
        title = t.thread_title or f"GC {t.id}"
        TEMP[uid]["gcs"][str(t.id)] = title
        kb.append([InlineKeyboardButton(f"📁 {title[:30]}", callback_data=f"gc:{t.id}")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_ig")])
    await msg.edit_text("📸 <b>Step 4/4: Choose GC</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    return S_IG_TEXT

async def cb_gc_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if q.data == "cancel_ig":
        TEMP.pop(uid, None); await show_home(q); return ConversationHandler.END
    if uid not in TEMP or "gcs" not in TEMP[uid]: return await q.answer("Expired", show_alert=True)
    
    gc_id = q.data.split(":")[1]
    TEMP[uid]["gc_id"] = gc_id
    TEMP[uid]["gc_name"] = TEMP[uid]["gcs"][gc_id]
    update_temp(uid)
    await safe_edit(q, f"✅ GC: <b>{esc(TEMP[uid]['gc_name'])}</b>\n\n💬 Send text:", reply_markup=InlineKeyboardMarkup([])) # Bug #35 Fix
    return S_IG_TEXT

async def ig_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text: return await update.message.reply_text("❌ Can't be empty.")
    if uid not in TEMP or "gc_id" not in TEMP[uid]: return await update.message.reply_text("⚠️ Expired. /start")
    
    data = TEMP[uid]
    if any(ig["username"] == data["username"] and ig["owner_id"] == uid for ig in CONFIG["ig_ids"]): # Bug #41 Fix
        TEMP.pop(uid, None); return await update.message.reply_text("⚠️ IG ID already added.", reply_markup=main_menu(uid))
    
    try: settings = data["client"].get_settings() # Bug #36 Fix
    except Exception as e: return await update.message.reply_text(f"❌ Failed: {esc(type(e).__name__)}")
    
    ig_entry = {
        "username": data["username"], "password": data["password"], "settings": settings,
        "owner_id": uid, "gc_id": data["gc_id"], "gc_name": data["gc_name"], "text": text,
        "running": True, "proxy": None, "added_at": datetime.now().isoformat(),
        "stats": {"sent": 0, "errors_total": 0, "errors_current": 0}
    }
    CONFIG["ig_ids"].append(ig_entry); save_config(); TEMP.pop(uid, None)
    
    await update.message.reply_text(f"✅ <b>IG @{esc(ig_entry['username'])} Added!</b>\n▶️ Starting...", reply_markup=main_menu(uid), parse_mode="HTML")
    IG_TASKS[ig_entry["username"]] = asyncio.create_task(ig_loop(ig_entry, context.application))
    return ConversationHandler.END

# ================= CONTROLS =================
async def cb_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 1)
    action, idx_str = parts
    uid = q.from_user.id
    
    igs = [ig for ig in CONFIG["ig_ids"] if ig["owner_id"] == uid]
    try:
        idx = int(idx_str)
        ig = igs[idx]
    except (ValueError, IndexError):
        return await q.answer("Stale button. Refresh.", show_alert=True)
    
    await q.answer()
    username = ig["username"]
    
    if action == "stop_ig":
        task = IG_TASKS.pop(username, None)
        if task:
            task.cancel()
            try: await task
            except (asyncio.CancelledError, Exception): pass
        ig["running"] = False; save_config()
        await release_proxy_for(username)
        await cb_ig_view(q)
    elif action == "start_ig":
        existing = IG_TASKS.get(username)
        if not existing or existing.done():
            IG_TASKS.pop(username, None)
            ig["running"] = True; save_config()
            IG_TASKS[username] = asyncio.create_task(ig_loop(ig, context.application))
        await cb_ig_view(q)
    elif action == "del_ig":
        task = IG_TASKS.pop(username, None)
        if task:
            task.cancel()
            try: await task
            except (asyncio.CancelledError, Exception): pass
        await release_proxy_for(username)
        CONFIG["ig_ids"] = [x for x in CONFIG["ig_ids"] if not (x["username"] == username and x["owner_id"] == uid)]
        save_config(); await cb_ig_view(q)

async def cb_del_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    bot_id = q.data.split(":")[1]
    uid = q.from_user.id
    to_del = next((b for b in CONFIG["bots"] if b["owner_id"] == uid and b["token"].split(":")[0] == bot_id and not b.get("is_main")), None)
    if to_del:
        if to_del["token"] in TG_APPS:
            try: await TG_APPS[to_del["token"]].updater.stop(); await TG_APPS[to_del["token"]].stop(); del TG_APPS[to_del["token"]]
            except: pass
        CONFIG["bots"] = [b for b in CONFIG["bots"] if b["token"] != to_del["token"]]
        save_config()
        # Bug #65 Fix: Check if user has no bots left
        if not any(b["owner_id"] == uid and b["token"] in TG_APPS for b in CONFIG["bots"]):
            for ig in [x for x in CONFIG["ig_ids"] if x["owner_id"] == uid]:
                task = IG_TASKS.pop(ig["username"], None)
                if task:
                    task.cancel()
                    try: await task
                    except (asyncio.CancelledError, Exception): pass
                ig["running"] = False; await release_proxy_for(ig["username"])
            save_config()
    await cb_tg_view(q)

async def cb_clean(q):
    await safe_edit(q, "⚠️ <b>Delete ALL your data?</b>\n\nCannot be undone.", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Yes, DELETE", callback_data="clean_yes")],
        [InlineKeyboardButton("🔙 No", callback_data="home")],
    ]))

async def cb_clean_yes(q):
    uid = q.from_user.id; await q.answer()
    for ig in [x for x in CONFIG["ig_ids"] if x["owner_id"] == uid]:
        task = IG_TASKS.pop(ig["username"], None)
        if task:
            task.cancel()
            try: await task
            except (asyncio.CancelledError, Exception): pass
        await release_proxy_for(ig["username"])
    for b in [x for x in CONFIG["bots"] if x["owner_id"] == uid and not x.get("is_main")]:
        if b["token"] in TG_APPS:
            try: await TG_APPS[b["token"]].updater.stop(); await TG_APPS[b["token"]].stop(); del TG_APPS[b["token"]]
            except: pass
    CONFIG["ig_ids"] = [x for x in CONFIG["ig_ids"] if x["owner_id"] != uid]
    CONFIG["bots"] = [x for x in CONFIG["bots"] if x["owner_id"] != uid or x.get("is_main")]
    CONFIG["users"].pop(str(uid), None); save_config()
    await safe_edit(q, "🧹 Cleaned!", reply_markup=main_menu(uid))

# ================= ROUTER & WIZARD EXIT =================
async def cmd_exit_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id
    TEMP.pop(uid, None); await q.answer()
    if q.data == "home": await show_home(q)
    elif q.data == "tg_view": await cb_tg_view(q)
    elif q.data == "ig_view": await cb_ig_view(q)
    elif q.data == "dash": await cb_dash(q)
    elif q.data == "clean": await cb_clean(q)
    elif q.data == "owner": await cb_owner(q)
    elif q.data == "add_ig": return await cb_add_ig(update, context)
    elif q.data == "add_tg": return await cb_add_tg(update, context)
    return ConversationHandler.END

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q.data
    if d == "owner_listbots":
        await cb_owner_listbots(q); return
    await q.answer()
    if d == "home": await show_home(q)
    elif d == "tg_view": await cb_tg_view(q)
    elif d == "ig_view": await cb_ig_view(q)
    elif d == "dash": await cb_dash(q)
    elif d == "clean": await cb_clean(q)
    elif d == "clean_yes": await cb_clean_yes(q)
    elif d == "owner": await cb_owner(q)

# ================= BOT BUILDER & MAIN =================
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
            S_IG_CHALLENGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ig_challenge_received)],
            S_IG_TEXT:  [CallbackQueryHandler(cb_gc_pick, pattern="^(gc:\\d+|cancel_ig)$"), MessageHandler(filters.TEXT & ~filters.COMMAND, ig_text_received)],
            S_ADD_BOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, owner_addbot_received)],
            S_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, owner_broadcast_received)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_cancel),
            CallbackQueryHandler(cmd_exit_wizard, pattern="^(home|tg_view|ig_view|dash|clean|owner|add_ig|add_tg)$"),
        ],
        per_user=True, per_chat=True, per_message=False,
    )
    app.add_handler(conv) # Bug #49 Fix: Conv first
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(cb_router, pattern="^(home|tg_view|ig_view|dash|clean|clean_yes|owner|owner_listbots)$"))
    app.add_handler(CallbackQueryHandler(cb_control, pattern="^(stop_ig|start_ig|del_ig):.+$"))
    app.add_handler(CallbackQueryHandler(cb_del_tg, pattern="^del_tg:.+$"))
    return app

async def start_bot(token, owner_id, is_main=False):
    if token in TG_APPS: return True
    app = None
    try:
        app = build_app(token, owner_id, is_main)
        await app.initialize(); await app.start(); await app.updater.start_polling()
        TG_APPS[token] = app
        return True
    except Exception as e:
        print(f"[BOT] {token[:15]}... fail: {type(e).__name__}: {e}")
        if app:
            try: await app.updater.stop()
            except: pass
            try: await app.stop()
            except: pass
            try: await app.shutdown()
            except: pass
        return False

async def main():
    global STOP_FLAG, GITHUB_USER, REPO_NAME
    cleanup_session_files()
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(default_config(), f, indent=2)
    load_config()

    main_entries = [b for b in CONFIG["bots"] if b.get("is_main")]
    if not main_entries:
        CONFIG["bots"].insert(0, {"token": MAIN_BOT_TOKEN, "owner_id": OWNER_ID, "added_at": datetime.now().isoformat(), "is_main": True}); save_config()
    elif main_entries[0].get("token") != MAIN_BOT_TOKEN:
        main_entries[0]["token"] = MAIN_BOT_TOKEN; main_entries[0]["owner_id"] = OWNER_ID; save_config()

    print("🚀 Dota Bot starting...")
    bg_tasks = [asyncio.create_task(proxy_fetcher()), asyncio.create_task(proxy_cleaner()), asyncio.create_task(gh_sync_loop()), asyncio.create_task(temp_cleanup_loop())]
    await gh_bootstrap()

    await start_bot(MAIN_BOT_TOKEN, OWNER_ID, is_main=True)

    for b in CONFIG["bots"]:
        if not b.get("is_main"): await start_bot(b["token"], b["owner_id"], is_main=False)

    for ig in CONFIG["ig_ids"]:
        if ig.get("running"):
            owner_app = find_owner_app(ig["owner_id"])
            if owner_app: IG_TASKS[ig["username"]] = asyncio.create_task(ig_loop(ig, owner_app))
            else: ig["running"] = False; save_config()

    print(f"✅ {len(TG_APPS)} bots running, {len(CONFIG['ig_ids'])} IG IDs loaded")
    try:
        while not STOP_FLAG: await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        STOP_FLAG = True
        for task in list(IG_TASKS.values()): task.cancel()
        for app in list(TG_APPS.values()):
            try: await app.updater.stop(); await app.stop()
            except: pass
        for t in bg_tasks:
            if not t.done():
                t.cancel()
                try: await t
                except: pass
        if GH_SESSION:
            try: await GH_SESSION.close()
            except: pass

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass