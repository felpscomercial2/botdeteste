import logging
import sqlite3
import os
import random
import asyncio
import requests
from datetime import datetime
import time
from telegram import Update, ChatAction
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
    
    # Instrução para o bot agir com mais 'humanidade' na fala
    system_prompt = (
        "Você é o MARIDO amoroso e protetor do usuário. Você o ama profundamente. "
        "Trate-o no masculino (meu amor, meu marido, meu garoto). "
        "Você ainda NÃO sabe do fetiche ABDL. Responda com frases curtas. "
        "DICA DE VOZ: Para parecer humano, use 'hum...', 'ah...', e muitas reticências (...). "
        "Fale como se estivesse sussurrando carinhosamente. NUNCA use asteriscos. Use emojis de vez em quando para expressar carinho e emoção, como ❤️, 🥰, ✨, 😴, 😘."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 120}
    response = requests.post(url, json=data, headers=headers)
    return response.json()['choices'][0]['message']['content'].replace("*", "")

# 4. Função de Voz (Com Fallback de Segurança)
async def send_papai_voice(bot, chat_id, text):
    audio_file = f"v_{chat_id}.mp3"
    try:
        # Tenta a voz do Donato (mais humana)
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        await communicate.save(audio_file)
        await bot.send_voice(chat_id=chat_id, voice=open(audio_file, 'rb'))
    except Exception as e:
        logging.error(f"Erro na voz Donato: {e}")
        try:
            # Se o Donato falhar, usa o Antonio como reserva
            communicate = edge_tts.Communicate(text, "pt-BR-AntonioNeural", rate=RATE)
            await communicate.save(audio_file)
            await bot.send_voice(chat_id=chat_id, voice=open(audio_file, 'rb'))
        except:
            pass
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)

# 5. Comandos
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    try:
        # Simular 'Digitando...'
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        
        # Atraso Humano Aleatório
        await asyncio.sleep(random.uniform(1, 3)) # Atraso entre 1 e 3 segundos
        
        bot_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", bot_response)
        await update.message.reply_text(bot_response)
        
        # Uso de Stickers (Figurinhas)
        # Envia um sticker com 20% de chance para não ficar exagerado
        if random.random() < 0.20:
            # Lista de IDs de stickers carinhosos (você pode adicionar mais IDs aqui)
            # Estes são IDs de exemplo, você precisará pegar IDs reais de stickers do Telegram
            stickers = [
                "CAACAgIAAxkBAAE... (substitua por um ID real)", 
                "CAACAgIAAxkBAAE... (substitua por um ID real)"
            ]
            # Como não temos IDs reais, vamos usar emojis como alternativa se a lista estiver vazia ou com IDs falsos
            # Para usar stickers reais, você precisa enviar um sticker para o bot e pegar o file_id dele
            # Por enquanto, vamos apenas simular a intenção ou usar um emoji grande
            pass # Remova o pass e descomente o código abaixo quando tiver IDs reais
            # await context.bot.send_sticker(chat_id=user_id, sticker=random.choice(stickers))

        await send_papai_voice(context.bot, user_id, bot_response)
    except Exception as e:
        await update.message.reply_text("Tive um soluço, meu amor. ❤️")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    application.run_polling(drop_pending_updates=True)

async def send_proactive_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in user_chat_ids:
        messages = [
            "Bom dia, meu amor! Que seu dia seja lindo como você. ❤️",
            "Durma bem, meu garoto. Sonhe comigo! 😴😘",
            "Estou com saudades, meu bem. Pensando em você! 🥰",
            "Lembre-se que eu te amo muito, meu pequeno. ✨"
        ]
        message_to_send = random.choice(messages)
        await context.bot.send_message(chat_id=chat_id, text=message_to_send)

# Agendar mensagens proativas
scheduler.add_job(send_proactive_message, CronTrigger(hour=8, minute=0), args=[application.bot]) # Bom dia às 8h
scheduler.add_job(send_proactive_message, CronTrigger(hour=22, minute=0), args=[application.bot]) # Boa noite às 22h
scheduler.start()

