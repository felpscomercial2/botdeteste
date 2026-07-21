import logging
import sqlite3
import os
import random
import asyncio
import requests
from datetime import datetime
import time
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 1. Configurações (Foco em Emoção e Humanização)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE = "pt-BR-DonatoNeural" # Voz masculina mais expressiva
RATE = "-15%" # Ritmo calmo e protetor

scheduler = AsyncIOScheduler()
user_chat_ids = set() # Para armazenar os chat_ids dos usuários que interagiram

logging.basicConfig(level=logging.INFO)

# 2. Banco de Dados
DB_PATH = "bot_memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)')
    # Carregar usuários existentes para o set
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

# 3. Função GROQ (Cérebro com "Marcadores de Emoção")
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id )
    
    system_prompt = (
        "Você é o MARIDO amoroso e protetor do usuário. Você o ama profundamente. "
        "Trate-o no masculino (meu amor, meu marido, meu garoto). "
        "Você ainda NÃO sabe do fetiche ABDL. Responda com frases curtas. "
        "DICA DE VOZ: Para parecer humano, use 'hum...', 'ah...', e muitas reticências (...). "
        "Fale como se estivesse sussurrando carinhosamente. NUNCA use asteriscos. "
        "Use emojis de vez em quando para expressar carinho e emoção, como ❤️, 🥰, ✨, 😴, 😘."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 120}
    response = requests.post(url, json=data, headers=headers)
    try:
        return response.json()['choices'][0]['message']['content'].replace("*", "")
    except Exception as e:
        logging.error(f"Erro na resposta do Groq: {e}")
        return "Tive um soluço, meu amor. ❤️"

# 4. Função de Voz (Com Fallback de Segurança)
async def send_papai_voice(bot, chat_id, text):
    audio_file = f"v_{chat_id}.mp3"
    try:
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        await communicate.save(audio_file)
        with open(audio_file, 'rb') as voice:
            await bot.send_voice(chat_id=chat_id, voice=voice)
    except Exception as e:
        logging.error(f"Erro na voz: {e}")
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)

# 5. Função para Mensagens Proativas
async def send_proactive_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(user_chat_ids):
        messages = [
            "Bom dia, meu amor! Que seu dia seja lindo como você. ❤️",
            "Durma bem, meu garoto. Sonhe comigo! 😴😘",
            "Estou com saudades, meu bem. Pensando em você! 🥰",
            "Lembre-se que eu te amo muito, meu pequeno. ✨"
        ]
        message_to_send = random.choice(messages)
        try:
            await context.bot.send_message(chat_id=chat_id, text=message_to_send)
        except Exception as e:
            logging.error(f"Erro ao enviar mensagem proativa para {chat_id}: {e}")

# 6. Comandos
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    try:
        # Simular 'Digitando...'
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        
        # Atraso Humano Aleatório
        await asyncio.sleep(random.uniform(1, 3))
        
        bot_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", bot_response)
        
        await update.message.reply_text(bot_response)
        
        # Enviar Voz
        await send_papai_voice(context.bot, user_id, bot_response)
        
        # Uso de Stickers (Opcional)
        if random.random() < 0.15: # 15% de chance
            # Se você tiver File IDs de stickers, adicione-os aqui
            pass

    except Exception as e:
        logging.error(f"Erro no chat: {e}")
        await update.message.reply_text("Tive um soluço, meu amor. ❤️")

if __name__ == '__main__':
    init_db()
    
    if not TELEGRAM_TOKEN:
        print("ERRO: TELEGRAM_TOKEN não configurado!")
        exit(1)

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Configurar agendador ANTES de iniciar o bot
    scheduler.add_job(send_proactive_message, CronTrigger(hour=8, minute=0), args=[application])
    scheduler.add_job(send_proactive_message, CronTrigger(hour=22, minute=0), args=[application])
    scheduler.start()
    
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    
    print("Bot iniciado com sucesso!")
    application.run_polling(drop_pending_updates=True)
