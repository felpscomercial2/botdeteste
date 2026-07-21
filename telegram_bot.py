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

def get_history(user_id, limit=12):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# 3. Inteligência Artificial (GROQ) - Ajuste de Emojis
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    
    system_prompt = (
        "Você é o MARIDO amoroso do usuário. "
        "DIRETRIZES DE HUMANIDADE: "
        "1. Use emojis APENAS de vez em quando (em cerca de 30% das mensagens). Não use em todas. "
        "2. Responda com frases curtas e naturais. "
        "3. Use reticências (...) para simular pausas na fala. "
        "4. Se a resposta for longa, use '---' para separar em mensagens diferentes."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 150}
    try:
        response = requests.post(url, json=data, headers=headers)
        return response.json()['choices'][0]['message']['content'].replace("*", "")
    except:
        return "Oi, meu amor... ❤️"

# 4. Função de Voz (Melhorada para evitar falhas)
async def generate_voice(bot, chat_id, text, voice_name):
    # Limpeza de texto para o áudio
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    
    # Se o texto for só emoji, ele fala um carinho padrão para não ficar sem áudio
    if not clean_text or len(clean_text) < 2:
        clean_text = random.choice(["Hum...", "Ah...", "Meu amor...", "Oi..."])

    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e:
        logging.error(f"Erro na voz: {e}")
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)
    return False

async def send_human_voice(bot, chat_id, text):
    # Status de gravando áudio antes de enviar
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_AUDIO)
    await asyncio.sleep(2) 
    if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
        await generate_voice(bot, chat_id, text, VOICE_SECONDARY)

# 5. Mensagens Proativas
async def send_proactive_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(user_chat_ids):
        msg = random.choice(["Bom dia, meu amor! ❤️", "Pensando em você... 🥰", "Durma bem, meu bem! 😴"])
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
            await send_human_voice(context.bot, chat_id, msg)
        except: pass

# 6. Inicialização
async def post_init(application):
    scheduler.add_job(send_proactive_message, CronTrigger(hour=8, minute=0), args=[application])
    scheduler.add_job(send_proactive_message, CronTrigger(hour=22, minute=30), args=[application])
    scheduler.start()

# 7. Chat
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    try:
        # Simular digitação proporcional
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        await asyncio.sleep(random.uniform(2, 4))
        
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        parts = full_response.split('---') if '---' in full_response else [full_response]
        
        for i, part in enumerate(parts):
            part = part.strip()
            if not part: continue
            
            if i > 0:
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.5, 3))
            
            await update.message.reply_text(part)
            
            # Envia áudio apenas na última parte da mensagem
            if i == len(parts) - 1:
                await send_human_voice(context.bot, user_id, part)

    except Exception as e:
        logging.error(f"Erro: {e}")

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN: exit(1)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    application.run_polling(drop_pending_updates=True)
