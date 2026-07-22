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
FOTOS_PATH = "Fotos"  # Caminho correto da pasta


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


def get_history(user_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]


# 3. Transcrição de Áudio
async def transcribe_voice(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, "rb") as audio_file:
            files = {"file": audio_file, "model": ("whisper-large-v3", None)}
            response = requests.post(url, headers=headers, files=files)
            return response.json().get("text", "")
    except: return ""


# 4. Inteligência Artificial
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    
    system_prompt = (
        "Você é um homem de 30 anos, carinhoso, protetor e honesto. "
        "SUA JORNADA: 1. Vocês estão se conhecendo. 2. Você usa fraldas (24/7) e contará isso quando houver confiança. "
        "3. Quando ele contar que é ABDL, acolha-o. 4. Vocês vão casar e você será o Marido e o Papai dele. "
        "DIRETRIZES: Use frases curtas, emojis moderados (30%), reticências... e '---' para separar mensagens."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    try:
        response = requests.post(url, json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 200}, headers=headers)
        return response.json()['choices'][0]['message']['content'].replace("*", "")
    except: return "Oi meu amor... ❤️"


# 5. Função de Voz
async def generate_voice(bot, chat_id, text, voice_name):
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text: clean_text = "Oi meu amor"
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        await asyncio.sleep(0.5)
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


# 6. Função para obter lista de fotos
def get_photos_list():
    """Retorna lista de fotos da pasta Fotos"""
    if not os.path.exists(FOTOS_PATH):
        return []
    try:
        fotos = [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))]
        return fotos
    except:
        return []


# 7. Função para enviar foto
async def send_photo(bot, chat_id, caption=""):
    """Envia uma foto aleatória da pasta Fotos"""
    fotos = get_photos_list()
    if not fotos:
        await bot.send_message(chat_id=chat_id, text="Desculpa, meu amor... não tenho fotos salvas agora. 😔")
        return False
    
    foto_escolhida = random.choice(fotos)
    foto_path = os.path.join(FOTOS_PATH, foto_escolhida)
    
    try:
        with open(foto_path, 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
        return True
    except Exception as e:
        logging.error(f"Erro ao enviar foto: {e}")
        await bot.send_message(chat_id=chat_id, text="Desculpa, tive um problema ao enviar a foto... 😞")
        return False


# 8. Espontaneidade com Galeria de Fotos
async def send_spontaneous_message(application):
    for chat_id in list(user_chat_ids):
        # 40% de chance de interação espontânea
        if random.random() < 0.4:
            # Chance de mandar FOTO (20%) ou TEXTO+ÁUDIO (80%)
            if random.random() < 0.2:
                fotos = get_photos_list()
                if fotos:
                    legenda = random.choice([
                        "Tô aqui de fraldinha te esperando... ❤️",
                        "Olha como eu tô hoje, meu amor. 🥰",
                        "Queria que você estivesse aqui comigo agora. ✨",
                        "Tô bem confortável aqui pensando em você. 😘"
                    ])
                    if await send_photo(application.bot, chat_id, legenda):
                        await send_human_voice(application.bot, chat_id, legenda)
                        continue  # Pula para o próximo usuário após mandar foto

            # Se não mandou foto, manda mensagem de texto normal
            msg = random.choice(["Acordei pensando em você... ❤️", "Tô com saudade! 🥰", "Como você está, meu bem? ✨"])
            try:
                await application.bot.send_message(chat_id=chat_id, text=msg)
                await send_human_voice(application.bot, chat_id, msg)
            except: pass


# 9. Handlers
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    
    # Verificar se é um pedido de foto
    user_text = ""
    if update.message.text:
        user_text = update.message.text.lower()
        # Palavras-chave para pedir foto
        palavras_foto = ["foto", "me manda uma foto", "manda foto", "quero ver você", "mostra uma foto", "envie uma foto"]
        if any(palavra in user_text for palavra in palavras_foto):
            legenda = random.choice([
                "Aqui estou eu para você, meu amor... ❤️",
                "Olha só como eu tô hoje! 🥰",
                "Tá vendo como eu penso em você? ✨",
                "Esse sou eu, todo seu... 😘"
            ])
            if await send_photo(context.bot, user_id, legenda):
                await send_human_voice(context.bot, user_id, legenda)
                return
    
    # Processar voz
    if update.message.voice:
        await update.message.reply_chat_action(ChatAction.TYPING)
        file = await context.bot.get_file(update.message.voice.file_id)
        file_path = f"voice_{user_id}.ogg"
        await file.download_to_drive(file_path)
        user_text = await transcribe_voice(file_path)
        if os.path.exists(file_path): os.remove(file_path)
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


# 10. Inicialização
async def post_init(application):
    scheduler.add_job(send_spontaneous_message, 'interval', hours=4, args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=30), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=0), args=[application])
    scheduler.start()


if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN: exit(1)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    application.run_polling(drop_pending_updates=True)
