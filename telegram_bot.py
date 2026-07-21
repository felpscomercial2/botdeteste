import logging
import sqlite3
import os
import random
import asyncio
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import edge_tts

# 1. Configurações (100% Gratuito e Estável)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE = "pt-BR-AntonioNeural" 
RATE = "-10%" # Velocidade calma e carinhosa

logging.basicConfig(level=logging.INFO)

# 2. Banco de Dados (Memória)
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

# 3. Função GROQ (Cérebro do Marido com Pausas)
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id )
    
    # Instruímos o cérebro a usar pontuação para a voz soar humana
    system_prompt = (
        "Você é o MARIDO amoroso e protetor do usuário. Você o ama profundamente e cuida dele. "
        "Trate-o no masculino (meu amor, meu marido, meu garoto, meu bem). "
        "Você ainda NÃO sabe do fetiche ABDL. Responda com frases curtas e afetuosas. "
        "DICA DE FALA: Use vírgulas e reticências (...) com frequência para a voz sair pausada, "
        "calma e carinhosa, como se estivesse falando baixinho. NUNCA use asteriscos."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 120}
    response = requests.post(url, json=data, headers=headers)
    return response.json()['choices'][0]['message']['content'].replace("*", "")

# 4. Função de Voz Gratuita e Estável
async def send_papai_voice(bot, chat_id, text):
    audio_file = f"v_{chat_id}.mp3"
    try:
        # Gerando com o Antonio de forma estável
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        await communicate.save(audio_file)
        
        if os.path.exists(audio_file):
            await bot.send_voice(chat_id=chat_id, voice=open(audio_file, 'rb'))
    except Exception as e:
        logging.error(f"Erro na voz: {e}")
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)

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
        await update.message.reply_text("Tive um soluço, meu amor. ❤️")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    
    print("Bot do Marido (Antonio) iniciado!")
    application.run_polling(drop_pending_updates=True)
