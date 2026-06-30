import os
import json
import logging
import asyncio
import time
import re
import threading  # Added for running Flask in the background
from datetime import datetime
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

# Flask import for keeping Render awake
# pyrefly: ignore [missing-import]
from flask import Flask

# Telegram imports
# pyrefly: ignore [missing-import]
from telegram import Update, ReplyKeyboardRemove
# pyrefly: ignore [missing-import]
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Scraping imports
# pyrefly: ignore [missing-import]
from selenium import webdriver
# pyrefly: ignore [missing-import]
from selenium.webdriver.common.by import By
# pyrefly: ignore [missing-import]
from selenium.webdriver.chrome.options import Options
# pyrefly: ignore [missing-import]
from selenium.webdriver.support.ui import WebDriverWait
# pyrefly: ignore [missing-import]
from selenium.webdriver.support import expected_conditions as EC

# Scheduler imports
# pyrefly: ignore [missing-import]
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Load bot token only
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# File paths
ALERTS_FILE = "alerts.json"
USERS_FILE = "users.json"
LOG_FILE = "log.txt"

# State for Conversation Handler
SET_ACCOUNT = 1

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- FLASK WEB SERVER SETUP ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!", 200

def run_web_server():
    # Render automatically assigns a PORT variable you must listen to
    port = int(os.environ.get("PORT", 8080))
    # Disabling the reloader is important when running inside a thread
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False)

# --- LOCAL FILE STORAGE HELPERS ---

def load_json_file(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {filepath}: {e}")
        return {}

def save_json_file(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving {filepath}: {e}")

# --- SCRAPING FUNCTION ---

def fetch_desco_data(customer_id, loop=None, progress_callback=None):
    """
    Scrapes the DESCO portal using Selenium with a dynamic Customer ID.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        login_url = "https://prepaid.desco.org.bd/customer/#/customer-login"
        driver.get(login_url)

        wait = WebDriverWait(driver, 15)

        username_field = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "form input, input[type='text'], input[placeholder*='account']"))
        )
        
        try:
            username_field.click()
            username_field.clear()
        except Exception:
            pass
            
        username_field.send_keys(str(customer_id))

        login_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "form button, button[type='submit'], .btn-primary"))
        )
        login_button.click()

        wait.until(EC.url_contains("customer-info"))

        logger.info(f"Waiting for dashboard baseline to load for ID {customer_id}...")
        wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Remaining Balance:')]")))
        time.sleep(2)

        # PROGRESSIVE LOADING: Extract Fast Data Instantly
        page_text = driver.find_element(By.TAG_NAME, "body").text
        
        balance = 0.0
        balance_match = re.search(r"Remaining Balance:\s*([\d,.]+)", page_text)
        if balance_match:
            balance = float(balance_match.group(1).replace(',', ''))

        usage_month = "N/A"
        usage_match = re.search(r"Used This Month:\s*([^\n]+)", page_text)
        if usage_match:
            usage_clean = re.split(r"(?=Recharged This|Max load)", usage_match.group(1))[0]
            usage_month = usage_clean.strip()

        if loop and progress_callback:
            partial_data = {
                "balance": balance,
                "usage_this_month": usage_month,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            asyncio.run_coroutine_threadsafe(progress_callback(partial_data), loop)

        # SMART POLLING LOOP FOR SLOW DATA
        logger.info("Waiting for slow historical data (Last Recharge) to populate...")
        max_wait = 60
        elapsed = 0

        while elapsed < max_wait:
            page_text = driver.find_element(By.TAG_NAME, "body").text
            
            if re.search(r"Last Recharge:\s*\d+", page_text):
                logger.info(f"Last Recharge data successfully populated after {elapsed} seconds!")
                time.sleep(1) 
                page_text = driver.find_element(By.TAG_NAME, "body").text
                break
                
            time.sleep(2)
            elapsed += 2
        else:
            logger.warning(f"Reached {max_wait}s timeout. Last Recharge might genuinely be N/A.")

        # FINAL DATA EXTRACTION
        recharge_amt_match = re.search(r"Last Recharge:\s*([\s\S]*?)\s*(?=Recharge time)", page_text)
        recharge_time_match = re.search(r"Recharge time:\s*([\s\S]*?)\s*(?=Remaining Balance|Reading time|Used This Month|Max load|$)", page_text)

        amt = recharge_amt_match.group(1).strip() if recharge_amt_match else "N/A"
        r_time = recharge_time_match.group(1).strip() if recharge_time_match else ""

        if not amt or amt.lower() == "n/a":
            amt = "N/A"
        if not r_time or r_time.lower() == "n/a":
            r_time = ""

        if r_time:
            last_recharge = f"{amt} (Time: {r_time})"
        else:
            last_recharge = amt

        max_load = "N/A"
        max_load_match = re.search(r"Max load last month:\s*([^\n]+)", page_text)
        if max_load_match:
            max_load = max_load_match.group(1).strip()

        # balance = 50.0  # Commented out for production
        data = {
            "balance": balance,
            "last_recharge": last_recharge,
            "usage_this_month": usage_month,
            "max_load": max_load,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        logger.info(f"Successfully fetched complete DESCO data. Balance: {balance}")
        return data

    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        return None
    finally:
        if driver:
            driver.quit()

# --- BOT ONBOARDING & COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    users = load_json_file(USERS_FILE)

    if chat_id in users and update.message.text.startswith("/start"):
        await update.message.reply_text(
            f"⚡ Welcome back! Your bot is active for DESCO Account: *{users[chat_id]}*.\n\n"
            "Available Commands:\n"
            "/balance - Check your current balance\n"
            "/setalert <amount> - Change low-balance alert threshold\n"
            "/stats - View current usage parameters\n"
            "/switch - Switch to a different DESCO account 🔄\n"
            "/reset - Delete your account details from this bot",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "🔄 **Switching/Registering Account**\n\n"
            "Please reply directly to this message with the new **DESCO Account Number** you want to monitor:",
            parse_mode='Markdown'
        )
        return SET_ACCOUNT

async def save_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    account_input = update.message.text.strip()

    if not account_input.isdigit() or len(account_input) < 5:
        await update.message.reply_text("⚠️ That doesn't look like a valid account number. Please enter digits only:")
        return SET_ACCOUNT

    users = load_json_file(USERS_FILE)
    users[chat_id] = account_input
    save_json_file(USERS_FILE, users)

    await update.message.reply_text(
        f"✅ Account registration complete! Bound to ID: **{account_input}**.\n\n"
        "You can now use `/balance` to pull your electricity layout, or use `/setalert 200` to monitor low threshold states.",
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Registration cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    users = load_json_file(USERS_FILE)
    alerts = load_json_file(ALERTS_FILE)

    if chat_id in users:
        del users[chat_id]
        save_json_file(USERS_FILE, users)
    if chat_id in alerts:
        del alerts[chat_id]
        save_json_file(ALERTS_FILE, alerts)

    await update.message.reply_text("🗑️ Your account layout parameters have been deleted. Use `/start` to register a new account.")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    users = load_json_file(USERS_FILE)

    if chat_id not in users:
        await update.message.reply_text("❌ You have not registered an account yet. Please send `/start` to configure your account.")
        return

    customer_id = users[chat_id]
    status_msg = await update.message.reply_text("⏳ Fetching data from DESCO portal. Please wait...")

    async def update_progress(partial_data):
        msg = (
            f"📊 **Current DESCO Balance**\n"
            f"🆔 Account: {customer_id}\n"
            f"💰 Balance: {partial_data['balance']} BDT\n"
            f"🔋 Last Recharge: ⏳ *Fetching history...*\n"
            f"📈 Usage This Month: {partial_data['usage_this_month']}\n"
            f"⏱️ Checked at: {partial_data['timestamp']}"
        )
        try:
            await status_msg.edit_text(msg, parse_mode='Markdown')
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    data = await asyncio.to_thread(fetch_desco_data, customer_id, loop, update_progress)

    if not data:
        await status_msg.edit_text("❌ Unable to fetch balance. Check your account number or internet connection.")
        return

    final_msg = (
        f"📊 **Current DESCO Balance**\n"
        f"🆔 Account: {customer_id}\n"
        f"💰 Balance: {data['balance']} BDT\n"
        f"🔋 Last Recharge: {data['last_recharge']}\n"
        f"📈 Usage This Month: {data['usage_this_month']}\n"
        f"⏱️ Checked at: {data['timestamp']}"
    )
    await status_msg.edit_text(final_msg, parse_mode='Markdown')

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    users = load_json_file(USERS_FILE)

    if chat_id not in users:
        await update.message.reply_text("❌ Please run `/start` to register your account before configuring alerts.")
        return
    
    try:
        if not context.args:
            await update.message.reply_text("⚠️ Please provide an amount. Usage: /setalert <amount>\nExample: /setalert 200")
            return
            
        threshold = int(context.args[0])
        if threshold <= 0 or threshold > 10000:
            await update.message.reply_text("⚠️ Threshold must be a positive integer between 1 and 10,000 BDT.")
            return

        alerts = load_json_file(ALERTS_FILE)
        alerts[chat_id] = threshold
        save_json_file(ALERTS_FILE, alerts)
        
        await update.message.reply_text(f"✅ Alert set successfully! I will notify you if your balance drops below {threshold} BDT.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid amount. Please enter a valid whole number (e.g., 200).")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    users = load_json_file(USERS_FILE)
    alerts = load_json_file(ALERTS_FILE)
    
    if chat_id not in users:
        await update.message.reply_text("❌ Please send `/start` to get configured.")
        return

    threshold = alerts.get(chat_id, "No alert set")
    customer_id = users[chat_id]
    
    await update.message.reply_text("⏳ Fetching recent statistics. Please wait...")
    data = await asyncio.to_thread(fetch_desco_data, customer_id)
    
    if not data:
        await update.message.reply_text("❌ Unable to fetch stats from the portal.")
        return

    msg = (
        f"📉 **Your DESCO Statistics**\n"
        f"🆔 Account: {customer_id}\n"
        f"🔔 Current Alert Threshold: {threshold} BDT\n"
        f"💰 Current Balance: {data['balance']} BDT\n"
        f"🔋 Last Recharge Date/Amount: {data['last_recharge']}\n"
        f"⚡ Usage This Month: {data['usage_this_month']}\n"
        f"📈 Max Load: {data['max_load']}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

# --- SCHEDULER BACKEND EXECUTION ---

async def check_balances_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled balance check for all active users...")
    alerts = load_json_file(ALERTS_FILE)
    users = load_json_file(USERS_FILE)
    
    if not alerts or not users:
        logger.info("No configurations available. Skipping scheduled pass.")
        return

    for chat_id, threshold in alerts.items():
        if chat_id not in users:
            continue
            
        customer_id = users[chat_id]
        data = await asyncio.to_thread(fetch_desco_data, customer_id)
        
        if not data:
            logger.error(f"Failed scheduled fetch pass for customer mapping ID {customer_id}")
            continue

        current_balance = data.get("balance", 0.0)
        if current_balance < threshold:
            warning_msg = (
                f"⚠️ **LOW BALANCE ALERT** ⚠️\n"
                f"Your DESCO balance has dropped below your threshold of {threshold} BDT.\n"
                f"Current Balance: **{current_balance} BDT**\n"
                f"Please recharge soon to avoid disconnection."
            )
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=warning_msg, parse_mode='Markdown')
                logger.info(f"Alert successfully pushed to user profile chat frame {chat_id}")
            except Exception as e:
                logger.error(f"Failed warning serialization dispatch toward destination routing path {chat_id}: {e}")

async def post_init(application) -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_balances_job, 
        'interval', 
        hours=6,  # Checked every 6 hours for production
        kwargs={'context': application}
    )
    scheduler.start()
    application.bot_data['scheduler'] = scheduler
    logger.info("Multi-user scheduler initialized successfully.")

async def post_shutdown(application) -> None:
    scheduler = application.bot_data.get('scheduler')
    if scheduler:
        scheduler.shutdown()
        logger.info("Scheduler loop destroyed.")

# --- MAIN RUNTIME LOADER ---

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment keying absent. Aborting initialization lifecycle.")
        exit(1)

    # 1. Start the Flask server in a background daemon thread right away
    logger.info("Starting background web server for Render keep-alive monitoring...")
    threading.Thread(target=run_web_server, daemon=True).start()

    # 2. Build the Telegram Application
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    start_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("switch", start_command)
        ],
        states={
            SET_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_account_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
    )

    app.add_handler(start_conv_handler)
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("setalert", setalert_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # 3. Start the bot's polling routine
    logger.info("Bot is initializing structural interfaces...")
    app.run_polling(drop_pending_updates=True)