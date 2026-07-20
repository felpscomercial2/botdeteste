import logging
import sqlite3
import os
import random
import asyncio
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import edge_tts

# 1. Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE = "pt-BR-DonatoNeural"
RATE = "-10%"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

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
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", (user_id, datetime.now()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    return [row[0] for row in c.fetchall()]

# 3. Função GROQ
def get_groq_response(user_id, user_text, is_proactive=False):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id )
    
    if is_proactive:
        system_prompt = "Você é o 'Papai' de um homem ABDL. Inicie uma conversa curta e carinhosa. Pergunte como ele está ou diga que estava pensando nele. Seja protetor. Trate-o no masculino. Sem asteriscos."
        user_input = "Papai, me mande um carinho surpresa."
    else:
        system_prompt = "Você é o 'Papai' de um homem ABDL. Seja protetor e carinhoso. Trate-o no masculino. Sem asteriscos."
        user_input = user_text

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_input})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "temperature": 0.8}
    response = requests.post(url, json=data, headers=headers)
    return response.json()['choices'][0]['message']['content'].replace("*", "").replace("_", "")

# 4. Função de Voz
async def send_papai_voice(bot, chat_id, text):
    audio_file = f"v_{chat_id}_{random.randint(1,1000)}.mp3"
    try:
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        await communicate.save(audio_file)
        await bot.send_voice(chat_id=chat_id, voice=open(audio_file, 'rb'))
    except Exception as e:
        logging.error(f"Erro na voz: {e}")
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)

# 5. Mensagens Proativas (O bot fala sozinho)
async def proactive_check(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    for user_id in users:
        if random.random() < 0.3: # 30% de chance de falar
            try:
                bot_response = get_groq_response(user_id, "", is_proactive=True)
                save_message(user_id, "model", bot_response)
                await context.bot.send_message(chat_id=user_id, text=bot_response)
                await send_papai_voice(context.bot, user_id, bot_response)
            except Exception as e:
                logging.error(f"Erro proativo: {e}")

# 6. Comandos
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Oi, meu garoto. O papai chegou. ❤️"
    await update.message.reply_text(text)
    await send_papai_voice(context.bot, update.effective_user.id, text)

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
        await update.message.reply_text("O papai teve um soluço, mas te amo. ❤️")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    
    # Agendar as mensagens surpresa a cada 4 horas
    job_queue = application.job_queue
    job_queue.run_repeating(proactive_check, interval=timedelta(hours=4), first=timedelta(seconds=10))
    
    application.run_polling(drop_pending_updates=True)
