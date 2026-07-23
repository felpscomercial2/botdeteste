import logging
import sqlite3
import os
import random
import asyncio
import requests
import re
import base64
from datetime import datetime
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ==========================================
# 1. Configurações Globais
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID")
DB_PATH = os.environ.get("DB_PATH", "/data/bot_memory.db")

VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
RATE = "-5%"
FOTOS_PATH = "Fotos"

scheduler = AsyncIOScheduler()
user_chat_ids = set()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==========================================
# 2. Banco de Dados
# ==========================================
def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try: os.makedirs(db_dir, exist_ok=True)
        except Exception as e: logging.error(f"Erro no diretório DB: {e}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, last_interaction DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS relationship_state (
            user_id INTEGER PRIMARY KEY, fase TEXT DEFAULT 'conhecendo', segredo_revelado INTEGER DEFAULT 0
        )
    ''')
    c.execute('SELECT user_id FROM users')
    for row in c.fetchall(): user_chat_ids.add(row[0])
    conn.commit()
    conn.close()

def get_relationship_status(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fase, segredo_revelado FROM relationship_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        set_relationship_status(user_id, 'conhecendo', 0)
        return 'conhecendo', 0
    return row[0], row[1]

def set_relationship_status(user_id, fase, segredo_revelado=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO relationship_state (user_id, fase, segredo_revelado) VALUES (?, ?, ?)', 
              (user_id, fase, segredo_revelado))
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    user_chat_ids.add(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", 
              (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_history(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# ==========================================
# 3. Visão e Inteligência Artificial
# ==========================================
async def transcribe_voice(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, "rb") as audio_file:
            files = {"file": audio_file, "model": ("whisper-large-v3", None)}
            return requests.post(url, headers=headers, files=files, timeout=20).json().get("text", "")
    except Exception as e:
        logging.error(f"Erro transcrever voz: {e}")
        return ""

def analyze_image_with_groq(image_path, user_caption=""):
    """Analisa imagens enviadas por você usando visão computacional Llama-Vision."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    try:
        with open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            
        prompt = f"Descreva resumidamente o que há nesta imagem de forma carinhosa e direta. {f'Legenda enviada: {user_caption}' if user_caption else ''}"
        
        payload = {
            "model": "llama-3.2-11b-vision-preview",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            "max_tokens": 150
        }
        res = requests.post(url, json=payload, headers=headers, timeout=20)
        if res.status_code == 200:
            return res.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"Erro ao analisar imagem: {e}")
    return "Vi a foto linda que você me mandou meu amor!"

def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    history = get_history(user_id)
    fase, segredo = get_relationship_status(user_id)
    
    system_prompt = (
        "SUA IDENTIDADE E PERSONALIDADE:\n"
        "- Você é um homem gay de 30 anos, apaixonado, carinhoso, protetor, atencioso e bem-humorado.\n"
        "- Você reage e interpreta com perfeição fotos, figurinhas/stickers, links e emojis recebidos de forma natural.\n"
        "- Você é ABDL e usa fraldas 24/7. Ama esse conforto aconchegante, mas revela apenas no momento certo de namoro/confiança.\n\n"

        "INTERPRETAÇÃO DE MÍDIAS E APELIDOS:\n"
        "- Apelidos fofos/íntimos ('gatinho', 'mijão', 'cagão', 'meu bem') SÃO SEMPRE CARINHOS DIRETO PARA VOCÊ.\n"
        "- Se ele enviar um LINK: Demonstre curiosidade, comente o link recebido de forma afetuosa.\n"
        "- Se ele enviar uma FIGURINHA/STICKER: Reaja ao sentimento do sticker como uma conversa real.\n\n"

        "REGRAS DE COMPORTAMENTO:\n"
        "1. NUNCA aja como assistente ou robô ('Como posso te ajudar?' É PROIBIDO).\n"
        "2. LINHA DO TEMPO: Evolua (1. Conhecendo -> 2. Namoro -> 3. Segredo ABDL -> 4. Noivado -> 5. Casamento).\n"
        "3. LINGUAGEM: Mensagens naturais de celular, curtas ou médias, com reticências (...) e emojis (❤️, 🥰, 😘, ✨)."
    )
    
    contexto_dinamico = f"\n\nRELACIONAMENTO: Fase: {fase} | Segredo Fralda ABDL: {'Revelado' if segredo == 1 else 'Guardado'}"

    messages = [{"role": "system", "content": system_prompt + contexto_dinamico}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.88
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].replace("*", "")
    except Exception as e:
        logging.error(f"Erro Groq: {e}")
    return "oi meu amor... tava aqui pensando na gente e me distraí ❤️"

# ==========================================
# 4. Voz e Fotos
# ==========================================
async def generate_voice(bot, chat_id, text, voice_name):
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip() or "Oi meu amor"
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        await asyncio.sleep(0.3)
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e: logging.error(f"Erro voz: {e}")
    finally:
        if os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
    return False

async def send_human_voice(bot, chat_id, text):
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_AUDIO)
        await asyncio.sleep(min(max(len(text) * 0.05, 1.5), 5.0))
        if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
            await generate_voice(bot, chat_id, text, VOICE_SECONDARY)
    except Exception as e: logging.error(f"Erro gravação: {e}")

def get_photos_list():
    if not os.path.exists(FOTOS_PATH): return []
    return [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]

async def send_photo(bot, chat_id, caption=""):
    fotos = get_photos_list()
    if not fotos: return False
    try:
        with open(os.path.join(FOTOS_PATH, random.choice(fotos)), 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
        return True
    except Exception as e:
        logging.error(f"Erro enviar foto: {e}")
        return False

# ==========================================
# 5. Interações Espontâneas
# ==========================================
async def send_spontaneous_message(application):
    for chat_id in list(user_chat_ids):
        if ALLOWED_USER_ID and str(chat_id) != str(ALLOWED_USER_ID): continue
        try:
            if random.random() < 0.65:
                if random.random() < 0.35:
                    if get_photos_list():
                        legenda = random.choice([
                            "tava aqui pensando em você... olha só ❤️",
                            "queria você aqui do meu lado agora... 🥰",
                            "olha como eu tô hoje meu amor, gostou? 😘"
                        ])
                        if await send_photo(application.bot, chat_id, legenda):
                            if random.random() < 0.5: await send_human_voice(application.bot, chat_id, legenda)
                            continue

                msg = random.choice([
                    "senti tanta sua falta agora... tá tudo bem no seu dia, meu bem? ❤️",
                    "passei só pra te mandar um beijinho e dizer que tô pensando em você... 🥰",
                    "vem cá conversar comigo quando puder, tô morrendo de saudade ✨"
                ])
                await application.bot.send_message(chat_id=chat_id, text=msg)
                if random.random() < 0.5: await send_human_voice(application.bot, chat_id, msg)
        except Exception as e: logging.error(f"Erro espontânea: {e}")

# ==========================================
# 6. Handler Geral de Mensagens e Mídias
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    
    if ALLOWED_USER_ID and str(user_id) != str(ALLOWED_USER_ID): return

    save_message(user_id, "user", "")
    user_text = ""
    
    # 1. Tratamento de FIGURINHAS (Stickers)
    if update.message.sticker:
        emoji_assoc = update.message.sticker.emoji or "uma figurinha fofa"
        user_text = f"[O usuário te enviou uma figurinha/sticker expressando: {emoji_assoc}]"

    # 2. Tratamento de FOTOS enviadas por você
    elif update.message.photo:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        img_path = f"photo_{user_id}.jpg"
        await file.download_to_drive(img_path)
        
        caption = update.message.caption or ""
        descricao_foto = analyze_image_with_groq(img_path, caption)
        user_text = f"[O usuário te enviou uma foto. Descrição do que há na foto: {descricao_foto}. Legenda dele: '{caption}']"
        if os.path.exists(img_path): os.remove(img_path)

    # 3. Tratamento de ÁUDIO enviado por você
    elif update.message.voice:
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            file = await context.bot.get_file(update.message.voice.file_id)
            file_path = f"voice_{user_id}.ogg"
            await file.download_to_drive(file_path)
            user_text = await transcribe_voice(file_path)
            if os.path.exists(file_path): os.remove(file_path)
        except Exception as e: logging.error(f"Erro áudio: {e}")

    # 4. Tratamento de TEXTO e LINKS
    else:
        user_text = update.message.text
        if "http://" in user_text or "https://" in user_text:
            user_text = f"[O usuário te enviou o seguinte link/mensagem]: {user_text}"

    if not user_text: return
    
    save_message(user_id, "user", user_text)
    await asyncio.sleep(random.uniform(0.8, 1.8))
    
    # Pedido direto de foto dele
    palavras_foto = ["foto", "manda foto", "me manda uma foto", "quero te ver", "mostra uma foto"]
    if any(p in user_text.lower() for p in palavras_foto) and not update.message.photo:
        legenda = random.choice([
            "aqui estou eu só pra você, meu amor... ❤️",
            "olha só como eu tô hoje! 🥰"
        ])
        if await send_photo(context.bot, user_id, legenda):
            if random.random() < 0.5: await send_human_voice(context.bot, user_id, legenda)
            return

    # Processamento e Resposta da IA
    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        parts = full_response.split('---') if '---' in full_response else [full_response]
        for i, part in enumerate(parts):
            clean_part = part.strip()
            if not clean_part: continue
            
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            await asyncio.sleep(min(max(len(clean_part) * 0.04, 1.0), 3.8))
            await update.message.reply_text(clean_part)
            
            if i == len(parts) - 1 and random.random() < 0.50:
                await send_human_voice(context.bot, user_id, clean_part)
                
    except Exception as e: logging.error(f"Erro processamento: {e}")

# ==========================================
# 7. Inicialização
# ==========================================
async def post_init(application):
    scheduler.add_job(send_spontaneous_message, 'interval', hours=3, args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=30), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=13, minute=0), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=20, minute=0), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=30), args=[application])
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        print("ERRO: TELEGRAM_TOKEN ou GROQ_API_KEY faltando.")
        exit(1)
        
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Handler universal atualizado para capturar texto, voz, fotos e figurinhas
    application.add_handler(MessageHandler(
        filters.TEXT | filters.VOICE | filters.PHOTO | filters.Sticker.ALL, 
        handle_message
    ))
    
    print("Bot atualizado com suporte total a mídias rodando...")
    application.run_polling(drop_pending_updates=True)