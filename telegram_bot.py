import logging
import sqlite3
import os
import random
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import google.generativeai as genai
import edge_tts

# Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
VOICE = "pt-BR-AntonioNeural"

logging.basicConfig(level=logging.INFO)

# O SEGREDO: Configurar o cérebro do Google
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

def init_db():
    conn = sqlite3.connect("bot_memory.db")
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)')
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = sqlite3.connect("bot_memory.db")
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", (user_id, datetime.now()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    conn = sqlite3.connect("bot_memory.db")
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "parts": [c]} for r, c in reversed(rows)]

async def generate_voice(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

SYSTEM_PROMPT = "Você é o 'Papai' de um homem ABDL. Seja protetor, carinhoso e trate-o no masculino. Responda sempre com muito afeto."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Oi, meu garoto! O papai chegou para cuidar de você. ❤️"
    await update.message.reply_text(text)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    try:
        # Tenta falar com o cérebro (Google)
        response = model.generate_content(f"{SYSTEM_PROMPT}\n\nFilho: {user_text}")
        bot_response = response.text
        save_message(user_id, "model", bot_response)
        
        await update.message.reply_text(bot_response)
        
        # Tenta mandar áudio
        try:
            audio_file = f"v_{user_id}.mp3"
            await generate_voice(bot_response, audio_file)
            await update.message.reply_voice(voice=open(audio_file, 'rb'))
            os.remove(audio_file)
        except:
            pass
    except Exception as e:
        print(f"Erro: {e}")
        await update.message.reply_text("O papai está com um probleminha para pensar agora, mas ainda te amo muito! ❤️")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    app.run_polling()
