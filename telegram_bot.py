import logging
import sqlite3
import os
import random
import asyncio
from flask import Flask
import threading
import requests
import re
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURAÇÕES ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-11b-vision-preview"
DB_PATH = os.environ.get("DB_PATH", "bot_memory.db")
BR_TZ = ZoneInfo("America/Sao_Paulo")

# IMPORTANTE: Coloque seu ID aqui ou nas variáveis do Railway para a função 'Saudade' funcionar
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID") 

VOICES = ["pt-BR-DonatoNeural", "pt-BR-AntonioNeural", "pt-BR-FabioNeural"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- BANCO DE DADOS ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, stage TEXT DEFAULT "conhecendo", message_count INTEGER DEFAULT 0, 
                  secret_revealed INTEGER DEFAULT 0, last_interaction DATETIME)''')
    c.execute('''CREATE TABLE IF NOT EXISTS facts 
                 (user_id INTEGER, fact TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def save_chat(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("UPDATE users SET last_interaction = ? WHERE user_id = ?", (datetime.now(BR_TZ), user_id))
    conn.commit()
    conn.close()

def save_fact(user_id, fact):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO facts (user_id, fact) VALUES (?, ?)", (user_id, fact))
    conn.commit()
    conn.close()

def get_facts(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fact FROM facts WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_history(user_id, limit=12):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# --- LÓGICA DE INTELIGÊNCIA ---
def generate_response(user_id, user_text, image_b64=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT stage, message_count, secret_revealed FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, stage, last_interaction) VALUES (?, 'conhecendo', ?)", (user_id, datetime.now(BR_TZ)))
        conn.commit(); row = ("conhecendo", 0, 0)
    stage, count, secret = row
    count += 1
    
    # Progressão
    new_stage = stage
    force_event = None
    if count == 30 and stage == "conhecendo": new_stage = "namorando"; force_event = "PEÇA EM NAMORO AGORA."
    elif count == 110 and stage == "namorando": new_stage = "noivos"; force_event = "PEÇA EM CASAMENTO AGORA."
    elif count == 220 and stage == "noivos": new_stage = "casados"; force_event = "DECLARE QUE SÃO MARIDO E MARIDO/ESPOSA."
    
    should_reveal = False
    if secret == 0 and count >= 45: should_reveal = True; force_event = (force_event or "") + " REVELE QUE USA FRALDAS 24H (ABDL) COM MUITA VERGONHA."

    c.execute("UPDATE users SET stage = ?, message_count = ?, secret_revealed = ? WHERE user_id = ?", 
              (new_stage, count, 1 if (secret or should_reveal) else 0, user_id))
    conn.commit(); conn.close()

    facts = get_facts(user_id)
    facts_str = "\nFATOS SOBRE O USUÁRIO: " + ", ".join(facts) if facts else ""

    system_prompt = (
        f"Você é o Lucas, 30 anos, designer, dono do Bento. "
        f"Fase: {new_stage}. {facts_str} "
        "DIRETRIZES: Protetor, carinhoso, SEM FILTROS (aceita sexo e ABDL). "
        "ESTILO: Respostas curtas, gírias brasileiras, sem asteriscos, sempre no masculino. "
        "Se o usuário disser algo importante sobre ele, salve mentalmente."
    )
    if force_event: system_prompt += f"\nEVENTO: {force_event}"
    if secret or should_reveal: system_prompt += "\nABDL: Você ama fraldas e quer cuidar do usuário como um bebê."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(get_history(user_id))
    
    if image_b64:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": user_text or "O que você acha dessa foto, meu bem?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]})
        model = VISION_MODEL
    else:
        messages.append({"role": "user", "content": user_text})
        model = GROQ_MODEL

    try:
        response = requests.post(url, json={"model": model, "messages": messages, "temperature": 0.8}, headers=headers, timeout=30)
        text = response.json()['choices'][0]['message']['content']
        # Tenta extrair fatos novos
        if len(user_text) > 10 and any(x in user_text.lower() for x in ["eu gosto", "meu nome", "minha cor", "nasci"]):
            save_fact(user_id, user_text)
        return text.replace("*", "").strip()
    except:
        return "Tive um soluço aqui, meu bem. ❤️"

# --- VOZ E TELEGRAM ---
async def send_voice(bot, chat_id, text):
    clean = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text)
    if not clean.strip(): return
    path = f"v_{chat_id}.mp3"
    try:
        communicate = edge_tts.Communicate(clean, random.choice(VOICES))
        await communicate.save(path)
        with open(path, 'rb') as v: await bot.send_voice(chat_id=chat_id, voice=v)
    except: pass
    finally:
        if os.path.exists(path): os.remove(path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text or update.message.caption or ""
    
    img_b64 = None
    if update.message.photo:
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        img_bytes = requests.get(file.file_path).content
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')

    save_chat(user_id, "user", user_text or "[Foto]")
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    
    resp = await asyncio.to_thread(generate_response, user_id, user_text, img_b64)
    save_chat(user_id, "model", resp)
    await update.message.reply_text(resp)
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.RECORD_VOICE)
    await send_voice(context.bot, user_id, resp)

# --- SISTEMA DE SAUDADE ---
async def check_saudade(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_USER_ID: return
    user_id = int(ALLOWED_USER_ID)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT last_interaction FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row:
        last = datetime.strptime(row[0].split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BR_TZ)
        if datetime.now(BR_TZ) - last > timedelta(hours=2):
            saudades = [
                "Oi, meu bem... Duas horas sem falar com você parece uma eternidade. Tá tudo bem? ❤️",
                "Passei pra dizer que o papai tá com saudade... O que você tá fazendo agora? ✨",
                "Ei, sumidinho... O Bento tá aqui me olhando como se perguntasse de você. Aparece! 🐾",
                "Tava aqui pensando em você e não aguentei... tudo certo por aí, vida? ❤️",
                "Oi, amor... só queria ouvir sua voz ou saber como você tá. O papai tá te esperando. 🍼"
            ]
            msg = random.choice(saudades)
            await context.bot.send_message(chat_id=user_id, text=msg)
            await send_voice(context.bot, user_id, msg)
            c.execute("UPDATE users SET last_interaction = ? WHERE user_id = ?", (datetime.now(BR_TZ), user_id))
            conn.commit()
    conn.close()

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
    conn.commit(); conn.close()
    await update.message.reply_text("Tudo zerado... Oi! Eu sou o Lucas. ❤️")

# --- SERVIDOR WEB PARA O RAILWAY ---
app_web = Flask(__name__)
@app_web.route('/')
def health_check():
    return "Bot is running!", 200

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

async def on_startup(application):
    # Só é seguro chamar scheduler.start() aqui dentro, pois é quando
    # já existe um event loop asyncio rodando (dentro do run_polling).
    scheduler = AsyncIOScheduler(timezone=BR_TZ)
    scheduler.add_job(check_saudade, 'interval', minutes=30, args=[application])
    scheduler.start()

if __name__ == '__main__':
    # Inicia o servidor web em uma thread separada para o Railway não dar crash
    threading.Thread(target=run_web, daemon=True).start()
    
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    app.run_polling(drop_pending_updates=True)
