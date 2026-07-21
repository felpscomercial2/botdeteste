import logging
import sqlite3
import os
import random
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from elevenlabs.client import ElevenLabs

# 1. Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY")

client_eleven = None
if ELEVEN_API_KEY:
    client_eleven = ElevenLabs(api_key=ELEVEN_API_KEY)

logging.basicConfig(level=logging.INFO)

# 2. Banco de Dados
DB_PATH = "bot_memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)')
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# 3. Função GROQ
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id )
    system_prompt = "Você é o Papai de um homem ABDL. Seja protetor e carinhoso. Trate-o no masculino. Sem asteriscos."
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    data = {"model": "llama-3.3-70b-versatile", "messages": messages}
    response = requests.post(url, json=data, headers=headers)
    return response.json()['choices'][0]['message']['content'].replace("*", "")

# 4. Função de Voz Real (Com o ID Secreto do Ethan)
async def send_papai_voice(bot, chat_id, text):
    if not client_eleven: return
    audio_file = f"v_{chat_id}.mp3"
    try:
        # Usamos o ID direto do Ethan: g5CIj9v6E6S30pBNoXhX
        audio = client_eleven.generate(
            text=text,
            voice="N2lNovelREB18aMvf5tn", # Este é o ID secreto do Ethan!
            model="eleven_multilingual_v2"
        )
        with open(audio_file, "wb") as f:
            for chunk in audio:
                if chunk: f.write(chunk)
        await bot.send_voice(chat_id=chat_id, voice=open(audio_file, 'rb'))
    except Exception as e:
        logging.error(f"Erro na voz: {e}")
    finally:
        if os.path.exists(audio_file): os.remove(audio_file)

# 5. Comandos
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    try:
        bot_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", bot_response)
        await update.message.reply_text(bot_response)
        await send_papai_voice(context.bot, user_id, bot_response)
    except Exception as e:
        logging.error(f"Erro no chat: {e}")
        await update.message.reply_text("O papai teve um soluço. ❤️")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    application.run_polling(drop_pending_updates=True)
