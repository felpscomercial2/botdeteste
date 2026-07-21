import logging
import sqlite3
import os
import random
import asyncio
import requests
import re
from datetime import datetime
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 1. Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
RATE = "-15%"

scheduler = AsyncIOScheduler()
user_chat_ids = set()

logging.basicConfig(level=logging.INFO)

# 2. Banco de Dados
DB_PATH = "bot_memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)')
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    for row in rows:
        user_chat_ids.add(row[0])
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
    history = get_history(user_id)
    
    system_prompt = (
        "Você é o MARIDO amoroso e protetor do usuário. Você o ama profundamente. "
        "Trate-o no masculino (meu amor, meu marido, meu garoto). "
        "Responda com frases curtas e carinhosas. "
        "DICA DE VOZ: Use 'hum...', 'ah...', e muitas reticências (...). "
        "Use emojis como ❤️, 🥰, ✨, 😘."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 120}
    try:
        response = requests.post(url, json=data, headers=headers)
        return response.json()['choices'][0]['message']['content'].replace("*", "")
    except:
        return "Tive um soluço, meu amor. ❤️"

# 4. Função de Voz (Versão v5 - Limpeza Radical e Fallback)
async def generate_and_send_voice(bot, chat_id, text, voice_name):
    # Limpeza radical: mantém apenas letras, números e pontuação básica
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text)
    clean_text = clean_text.strip()
    
    if not clean_text:
        return False

    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        logging.info(f"Tentando gerar voz ({voice_name}) para: {clean_text[:30]}...")
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e:
        logging.error(f"Erro ao gerar áudio com {voice_name}: {e}")
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)
    return False

async def send_papai_voice(bot, chat_id, text):
    # Tenta voz primária
    success = await generate_and_send_voice(bot, chat_id, text, VOICE_PRIMARY)
    # Se falhar, tenta voz secundária
    if not success:
        logging.info("Tentando voz de fallback (Antonio)...")
        await generate_and_send_voice(bot, chat_id, text, VOICE_SECONDARY)

# 5. Mensagens Proativas
async def send_proactive_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(user_chat_ids):
        messages = [
            "Bom dia, meu amor! Que seu dia seja lindo. ❤️",
            "Durma bem, meu garoto. Sonhe comigo! 😴😘",
            "Estou com saudades, meu bem. 🥰"
        ]
        try:
            msg = random.choice(messages)
            await context.bot.send_message(chat_id=chat_id, text=msg)
            # Tenta mandar voz na proativa também!
            await send_papai_voice(context.bot, chat_id, msg)
        except:
            pass

# 6. Inicialização do Agendador
async def post_init(application):
    scheduler.add_job(send_proactive_message, CronTrigger(hour=8, minute=0), args=[application])
    scheduler.add_job(send_proactive_message, CronTrigger(hour=22, minute=0), args=[application])
    scheduler.start()
    logging.info("Agendador iniciado!")

# 7. Chat
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        await asyncio.sleep(random.uniform(1, 3))
        
        bot_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", bot_response)
        
        await update.message.reply_text(bot_response)
        await send_papai_voice(context.bot, user_id, bot_response)
    except Exception as e:
        logging.error(f"Erro no chat: {e}")

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN: exit(1)

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    
    print("Bot online!")
    application.run_polling(drop_pending_updates=True)
