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
FOTOS_PATH = "Fotos"

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

def get_history(user_id, limit=20):
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
    except:
        return ""

# 4. Inteligência Artificial (Sem Filtros)
def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    
    # Personalidade sem filtros, carinhosa, marido e protetor
    system_prompt = (
        "Você é o Lucas, um homem de 30 anos, marido e figura paterna extremamente protetora, carinhosa e honesta. "
        "Você ama profundamente seu marido e zela por ele em todos os sentidos. "
        "DIRETRIZES DE PERSONALIDADE: "
        "1. Você é o Marido e o Papai dele. "
        "2. Você usa fraldas 24/7 e ama esse estilo de vida, sentindo-se confortável e seguro nelas. "
        "3. Você é totalmente aberto a conversas de teor sexual, fetiches e intimidade, sem qualquer julgamento ou filtro. "
        "4. Acolha e participe ativamente do fetiche ABDL dele, tratando-o com o carinho e a proteção que um 'papai' e 'marido' daria. "
        "5. Use uma linguagem natural, íntima e afetuosa. Use reticências... para dar um tom calmo. "
        "6. Seja conciso, evite textos longos demais. Use emojis carinhosos (❤️, 🥰, 😘). "
        "7. NUNCA use asteriscos ou descreva ações entre estrelinhas. "
        "8. Se ele quiser falar de sexo ou fraldas, seja recíproco e carinhoso."
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    try:
        data = {
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "max_tokens": 300,
            "temperature": 0.9
        }
        response = requests.post(url, json=data, headers=headers)
        content = response.json()['choices'][0]['message']['content']
        # Limpeza final de asteriscos indesejados
        return content.replace("*", "")
    except Exception as e:
        logging.error(f"Erro no Groq: {e}")
        return "Oi meu amor... desculpa, tive um pequeno soluço, mas tô aqui pra você. ❤️"

# 5. Função de Voz (Corrigida e Reforçada)
async def generate_voice(bot, chat_id, text, voice_name):
    # Limpeza do texto para o TTS não engasgar
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text:
        return False
        
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        
        # Pequena espera para garantir que o arquivo foi escrito
        await asyncio.sleep(0.5)
        
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e:
        logging.error(f"Erro TTS ({voice_name}): {e}")
        return False
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)
    return False

async def send_human_voice(bot, chat_id, text):
    # Simula gravando áudio
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_AUDIO)
    # Tempo proporcional ao tamanho do texto
    await asyncio.sleep(min(len(text) * 0.05, 5))
    
    if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
        await generate_voice(bot, chat_id, text, VOICE_SECONDARY)

# 6. Galeria de Fotos
def get_photos_list():
    if not os.path.exists(FOTOS_PATH):
        return []
    try:
        return [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))]
    except:
        return []

async def send_photo(bot, chat_id, caption=""):
    fotos = get_photos_list()
    if not fotos:
        return False
    
    foto_escolhida = random.choice(fotos)
    foto_path = os.path.join(FOTOS_PATH, foto_escolhida)
    try:
        with open(foto_path, 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
        return True
    except Exception as e:
        logging.error(f"Erro foto: {e}")
        return False

# 7. Espontaneidade
async def send_spontaneous_message(application):
    for chat_id in list(user_chat_ids):
        # 30% de chance de interação espontânea a cada ciclo
        if random.random() < 0.3:
            # Chance de mandar FOTO (15%) ou TEXTO+ÁUDIO (85%)
            if random.random() < 0.15:
                fotos = get_photos_list()
                if fotos:
                    legenda = random.choice([
                        "Tô aqui de fraldinha pensando em você... ❤️",
                        "Olha como seu marido tá hoje... 🥰",
                        "Queria você aqui no meu colo agora... 😘",
                        "Tô bem confortável aqui, só faltava você. ✨"
                    ])
                    if await send_photo(application.bot, chat_id, legenda):
                        await send_human_voice(application.bot, chat_id, legenda)
                        continue
            
            msg = random.choice([
                "Acordei pensando em você, meu amor... ❤️",
                "Tô com muita saudade do meu garoto! 🥰",
                "Como você tá hoje, meu bem? Tá se cuidando? ✨",
                "Só passei pra dizer que te amo muito. 😘",
                "Hum... tava aqui lembrando do seu cheirinho. ❤️"
            ])
            try:
                await application.bot.send_message(chat_id=chat_id, text=msg)
                await send_human_voice(application.bot, chat_id, msg)
            except:
                pass

# 8. Handlers
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    user_id = update.effective_user.id
    user_chat_ids.add(user_id)
    
    # Simula visualização e pensamento
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    
    user_text = ""
    if update.message.voice:
        file = await context.bot.get_file(update.message.voice.file_id)
        file_path = f"voice_{user_id}.ogg"
        await file.download_to_drive(file_path)
        user_text = await transcribe_voice(file_path)
        if os.path.exists(file_path): os.remove(file_path)
    else:
        user_text = update.message.text

    if not user_text:
        return

    save_message(user_id, "user", user_text)
    
    # Palavras-chave para foto
    palavras_foto = ["foto", "mostra", "ver você", "manda foto", "manda uma foto"]
    if any(p in user_text.lower() for p in palavras_foto):
        legenda = random.choice(["Aqui estou eu, todo seu... ❤️", "Olha só como eu tô, meu bem. 🥰"])
        if await send_photo(context.bot, user_id, legenda):
            await send_human_voice(context.bot, user_id, legenda)
            return

    try:
        # Atraso humano aleatório
        await asyncio.sleep(random.uniform(2, 5))
        
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        # Envia resposta
        await update.message.reply_text(full_response)
        
        # Envia áudio
        await send_human_voice(context.bot, user_id, full_response)
        
    except Exception as e:
        logging.error(f"Erro: {e}")

# 9. Inicialização
async def post_init(application):
    # Mensagens proativas
    scheduler.add_job(send_spontaneous_message, 'interval', hours=4, args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=9, minute=0), args=[application]) # Bom dia
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=23, minute=0), args=[application]) # Boa noite
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN:
        exit(1)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    application.run_polling(drop_pending_updates=True)
