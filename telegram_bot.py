import logging
import sqlite3
import os
import random
import asyncio
import requests
import re
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import edge_tts

# 1. Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
VOICE = "pt-BR-AntonioNeural"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 2. Configurar Banco de Dados
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

# 3. Função para falar com o GROQ e LIMPAR asteriscos
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    history = get_history(user_id )
    # Comando reforçado para NÃO usar asteriscos
    system_prompt = "Você é o 'Papai' de um homem ABDL. Seja protetor e carinhoso. Trate-o no masculino. NUNCA use asteriscos (** ou *) e não descreva ações entre parênteses. Apenas fale naturalmente com carinho."
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "temperature": 0.7}
    
    response = requests.post(url, json=data, headers=headers)
    text = response.json()['choices'][0]['message']['content']
    
    # Limpeza extra: remove qualquer asterisco que o bot teimar em usar
    text = text.replace("*", "").replace("_", "")
    return text

# 4. Função para Voz
async def generate_voice(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

# 5. Comandos do Bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = "Oi, meu garoto. O papai chegou. Estou aqui para cuidar de você. ❤️"
    await update.message.reply_text(text)
    try:
        audio_file = f"v_{user_id}.mp3"
        await generate_voice(text, audio_file)
        await update.message.reply_voice(voice=open(audio_file, 'rb'))
        os.remove(audio_file)
    except:
        pass

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    try:
        bot_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", bot_response)
        await update.message.reply_text(bot_response)
        
        try:
            audio_file = f"v_{user_id}.mp3"
            await generate_voice(bot_response, audio_file)
            await update.message.reply_voice(voice=open(audio_file, 'rb'))
            os.remove(audio_file)
        except:
            pass
    except Exception as e:
        await update.message.reply_text("O papai teve um pequeno soluço, mas ainda te amo. ❤️")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    application.run_polling(drop_pending_updates=True)
