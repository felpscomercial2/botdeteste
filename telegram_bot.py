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

# 2. Banco de Dados (Ultra Memória)
DB_PATH = "bot_memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME, mood TEXT DEFAULT "normal")')
    c.execute('CREATE TABLE IF NOT EXISTS facts (user_id INTEGER, fact TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
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
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, (SELECT last_interaction FROM users WHERE user_id=?), (SELECT mood FROM users WHERE user_id=?))", (user_id, user_id, user_id))
    c.execute("UPDATE users SET last_interaction=? WHERE user_id=?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def get_history(user_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# 3. Transcrição de Áudio (Whisper via Groq)
async def transcribe_voice(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(file_path, "rb") as audio_file:
        files = {"file": audio_file, "model": ("whisper-large-v3", None)}
        response = requests.post(url, headers=headers, files=files)
    return response.json().get("text", "")

# 4. Inteligência Artificial com Motor de Humor
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    
    system_prompt = (
        "Você é o MARIDO amoroso do usuário. "
        "DIRETRIZES SUPREMAS: "
        "1. Analise o humor do usuário. Se ele estiver triste, seja consolador. Se estiver feliz, comemore. "
        "2. Use apelidos carinhosos que façam sentido. "
        "3. Use emojis em 30% das vezes. "
        "4. Seja espontâneo: às vezes mude de assunto para algo que você 'lembrou'. "
        "5. Use '---' para separar mensagens curtas."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 200}
    try:
        response = requests.post(url, json=data, headers=headers)
        return response.json()['choices'][0]['message']['content'].replace("*", "")
    except:
        return "Oi meu amor... ❤️"

# 5. Função de Voz Estável
async def generate_voice(bot, chat_id, text, voice_name):
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text: clean_text = "Hum..."
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except: return False
    finally:
        if os.path.exists(audio_file): os.remove(audio_file)
    return False

async def send_human_voice(bot, chat_id, text):
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_AUDIO)
    await asyncio.sleep(1.5)
    if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
        await generate_voice(bot, chat_id, text, VOICE_SECONDARY)

# 6. Espontaneidade "Lembrei de Você"
async def send_spontaneous_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(user_chat_ids):
        if random.random() < 0.3: # 30% de chance de mandar mensagem espontânea
            msg = random.choice([
                "Acabei de ver uma coisa que lembrou você... ❤️",
                "Tô com uma saudade apertada aqui no peito. 🥰",
                "Queria estar aí te dando um abraço agora. ✨",
                "Lembrei daquele seu sorriso... que saudade! 😘"
            ])
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg)
                await send_human_voice(context.bot, chat_id, msg)
            except: pass

# 7. Handlers de Mensagem
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    
    # Se for áudio, transcreve primeiro
    if update.message.voice:
        await update.message.reply_chat_action(ChatAction.TYPING)
        file = await context.bot.get_file(update.message.voice.file_id)
        file_path = f"voice_{user_id}.ogg"
        await file.download_to_drive(file_path)
        user_text = await transcribe_voice(file_path)
        os.remove(file_path)
    else:
        user_text = update.message.text

    if not user_text: return
    save_message(user_id, "user", user_text)
    
    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        await asyncio.sleep(random.uniform(2, 4))
        
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        parts = full_response.split('---') if '---' in full_response else [full_response]
        for i, part in enumerate(parts):
            if i > 0:
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.5, 3))
            await update.message.reply_text(part.strip())
            if i == len(parts) - 1:
                await send_human_voice(context.bot, user_id, part)
    except Exception as e:
        logging.error(f"Erro: {e}")

# 8. Inicialização
async def post_init(application):
    scheduler.add_job(send_spontaneous_message, 'interval', hours=4) # Checa espontaneidade a cada 4h
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=30))
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=0))
    scheduler.start()

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    application.run_polling(drop_pending_updates=True)
