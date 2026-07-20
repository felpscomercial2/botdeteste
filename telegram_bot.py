import logging
import sqlite3
import os
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)

# Configurar o cérebro
genai.configure(api_key=GEMINI_API_KEY)

def init_db():
    conn = sqlite3.connect("bot_memory.db")
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = sqlite3.connect("bot_memory.db")
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    conn.commit()
    conn.close()

SYSTEM_PROMPT = "Você é o 'Papai' de um homem ABDL. Seja protetor e carinhoso. Responda sempre no masculino."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Oi, meu garoto! O papai chegou. ❤️")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    try:
        # Tenta pensar
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(f"{SYSTEM_PROMPT}\n\nFilho: {user_text}")
        bot_response = response.text
        
        save_message(update.effective_user.id, "user", user_text)
        save_message(update.effective_user.id, "model", bot_response)
        
        await update.message.reply_text(bot_response)
    except Exception as e:
        print(f"Erro no cérebro: {e}")
        await update.message.reply_text("O papai está tentando pensar, mas a chave do meu cérebro está com sono. Verifique se a sua GEMINI_API_KEY no Railway está certinha! ❤️")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    app.run_polling()
