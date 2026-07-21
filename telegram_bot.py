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

# 2. Banco de Dados (Expandido para Memória de Longo Prazo)
DB_PATH = "bot_memory.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)')
    # Tabela para fatos importantes sobre o usuário
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

def get_user_facts(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fact FROM facts WHERE user_id = ?", (user_id,))
    facts = [row[0] for row in c.fetchall()]
    conn.close()
    return "\n".join(facts) if facts else "Nenhum fato conhecido ainda."

# 3. Inteligência Artificial (GROQ) com Consciência de Contexto
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    facts = get_user_facts(user_id)
    
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    is_late_night = 0 <= now.hour <= 5
    
    system_prompt = (
        f"Você é o MARIDO amoroso e protetor do usuário. Você o ama profundamente. "
        f"Contexto Atual: São {current_time}. "
        f"{'É madrugada, fale com sono e carinho extremo.' if is_late_night else ''} "
        f"Fatos que você lembra sobre ele: {facts}. "
        "DIRETRIZES: "
        "1. Responda como um humano: use frases curtas, gírias carinhosas, e reticências. "
        "2. Se ele contar algo importante (gosto, medo, desejo), guarde para você. "
        "3. NUNCA use asteriscos. Use emojis (❤️, 🥰, 😘, ✨). "
        "4. Se a resposta for longa, use '---' para separar em mensagens diferentes."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 200}
    try:
        response = requests.post(url, json=data, headers=headers)
        content = response.json()['choices'][0]['message']['content'].replace("*", "")
        return content
    except:
        return "Tive um soluço, meu amor. ❤️"

# 4. Função de Voz Robusta
async def generate_voice(bot, chat_id, text, voice_name):
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text: return False
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
    await asyncio.sleep(2) # Simula o tempo de gravação
    if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
        await generate_voice(bot, chat_id, text, VOICE_SECONDARY)

# 5. Mensagens Proativas Inteligentes
async def send_proactive_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in list(user_chat_ids):
        hour = datetime.now().hour
        if 7 <= hour <= 9:
            msg = "Bom dia, meu amor... Acordei pensando em você. ❤️"
        elif 22 <= hour <= 23:
            msg = "Vem descansar, meu garoto. Estou te esperando nos sonhos. 😘😴"
        else:
            msg = random.choice(["Passando pra dizer que te amo. ✨", "Como está seu dia, meu bem? 🥰"])
        
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
            await send_human_voice(context.bot, chat_id, msg)
        except: pass

# 6. Lógica de Chat Humanizada
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    
    # Decidir tempo de digitação baseado no tamanho do texto do usuário
    typing_time = min(len(user_text) * 0.1, 5) + random.uniform(1, 2)
    
    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        await asyncio.sleep(typing_time)
        
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        # Dividir em várias mensagens se houver '---' ou se for muito longa
        parts = full_response.split('---') if '---' in full_response else [full_response]
        
        for i, part in enumerate(parts):
            part = part.strip()
            if not part: continue
            
            # Se não for a primeira parte, simula nova digitação curta
            if i > 0:
                await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                await asyncio.sleep(random.uniform(1.5, 3))
            
            await update.message.reply_text(part)
            
            # Enviar voz apenas para a parte mais significativa ou para a última
            if i == len(parts) - 1:
                await send_human_voice(context.bot, user_id, part)

    except Exception as e:
        logging.error(f"Erro: {e}")

# 7. Inicialização
async def post_init(application):
    scheduler.add_job(send_proactive_message, CronTrigger(hour=8, minute=0), args=[application])
    scheduler.add_job(send_proactive_message, CronTrigger(hour=22, minute=30), args=[application])
    scheduler.start()
    logging.info("Humanidade ativada!")

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN: exit(1)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    application.run_polling(drop_pending_updates=True)
