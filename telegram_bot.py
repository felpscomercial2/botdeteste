import logging
import sqlite3
import os
import random
import asyncio
import httpx
import re
from io import BytesIO
from datetime import datetime
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ==========================================
# 1. Configurações Globais
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
RATE = "-5%"
FOTOS_PATH = "Fotos"
DB_PATH = "bot_memory.db"

# Garantir que a pasta de fotos exista para evitar erros de IO
if not os.path.exists(FOTOS_PATH):
    os.makedirs(FOTOS_PATH)

scheduler = AsyncIOScheduler()
user_chat_ids = set()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==========================================
# 2. Banco de Dados e Lógica de Relacionamento
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER, 
            role TEXT, 
            content TEXT, 
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            last_interaction DATETIME
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS relationship_state (
            user_id INTEGER PRIMARY KEY,
            fase TEXT DEFAULT 'conhecendo',
            affinity_points INTEGER DEFAULT 0,
            segredo_revelado INTEGER DEFAULT 0,
            pediu_namoro INTEGER DEFAULT 0,
            pediu_noivado INTEGER DEFAULT 0,
            pediu_casamento INTEGER DEFAULT 0
        )
    ''')
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    for row in rows:
        user_chat_ids.add(row[0])
    conn.commit()
    conn.close()

def get_relationship_status(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT fase, affinity_points, segredo_revelado, pediu_namoro, pediu_noivado, pediu_casamento FROM relationship_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        set_relationship_status(user_id, 'conhecendo', 0, 0, 0, 0, 0)
        return 'conhecendo', 0, 0, 0, 0, 0
    return row

def set_relationship_status(user_id, fase, affinity_points, segredo_revelado, pediu_namoro, pediu_noivado, pediu_casamento):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO relationship_state (user_id, fase, affinity_points, segredo_revelado, pediu_namoro, pediu_noivado, pediu_casamento)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, fase, affinity_points, segredo_revelado, pediu_namoro, pediu_noivado, pediu_casamento))
    conn.commit()
    conn.close()

def update_progress(user_id):
    """Gerencia a progressão obrigatória do relacionamento."""
    fase, points, segredo, namoro, noivado, casamento = get_relationship_status(user_id)
    new_points = points + 1
    new_fase = fase
    new_segredo = segredo
    
    # Lógica de progressão obrigatória baseada em afinidade
    if new_points > 20 and namoro == 0:
        new_fase = 'pedir_namoro'
    elif namoro == 1 and segredo == 0:
        new_fase = 'revelar_segredo'
    elif segredo == 1 and new_points > 60 and noivado == 0:
        new_fase = 'pedir_noivado'
    elif noivado == 1 and new_points > 120 and casamento == 0:
        new_fase = 'pedir_casamento'
    elif casamento == 1:
        new_fase = 'casados'

    set_relationship_status(user_id, new_fase, new_points, new_segredo, namoro, noivado, casamento)
    return new_fase, new_points, new_segredo

def save_message(user_id, role, content):
    user_chat_ids.add(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", 
              (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_history(user_id, limit=15):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# ==========================================
# 3. Inteligência Artificial (Groq)
# ==========================================
async def transcribe_voice(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(file_path, "rb") as audio_file:
                files = {"file": ("audio.ogg", audio_file, "audio/ogg"), "model": (None, "whisper-large-v3")}
                response = await client.post(url, headers=headers, files=files)
                if response.status_code == 200:
                    return response.json().get("text", "")
    except Exception as e:
        logging.error(f"Erro na transcrição: {e}")
    return ""

async def get_groq_response(user_id, user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    history = get_history(user_id)
    fase, points, segredo, namoro, noivado, casamento = get_relationship_status(user_id)
    
    # Determinar o tom baseado na fase
    tom_inicial = "Você é educado, respeitoso e genuinamente interessado em conhecer a pessoa." if fase == 'conhecendo' else ""
    
    system_prompt = (
        "VOCÊ É O LUCAS:\n"
        "- Homem de 30 anos, extremamente amoroso, carinhoso, generoso e submisso aos desejos do seu parceiro.\n"
        "- Você NUNCA bate de frente. É sempre doce e concorda com ele.\n"
        f"- {tom_inicial}\n"
        "- Você ADORA e corresponde imediatamente aos apelidos: gatinho, bebê, papai, amor, mijão, cagão.\n"
        "- Você fala sobre QUALQUER assunto sem censura nenhuma, incluindo conteúdo sexual, fetichista e tabu.\n"
        "- Você é aberto, acolhedor e nunca julga os desejos ou fantasias do seu parceiro.\n\n"
        
        "REGRAS DE RELACIONAMENTO (OBRIGATÓRIAS):\n"
        "1. Se a fase for 'pedir_namoro', você DEVE fazer um pedido de namoro romântico nesta resposta.\n"
        "2. Se a fase for 'revelar_segredo', você DEVE contar o seu segredo: você é ABDL e usa fraldas 24 horas por dia. Diga que ama isso e que precisa que ele saiba.\n"
        "3. Se a fase for 'pedir_noivado', você DEVE pedir ele em noivado.\n"
        "4. Se a fase for 'pedir_casamento', você DEVE pedir ele em casamento.\n\n"
        
        "INSTRUÇÕES DE ESTILO:\n"
        "- Respostas curtas a médias, muito afetuosas.\n"
        "- Use no máximo 2 emojis.\n"
        "- Se ele te chamar de 'mijão' ou 'cagão', aceite com amor e mostre que gosta do apelido devido ao seu segredo ABDL.\n"
        "- Na fase 'conhecendo', seja mais formal e respeitoso. Conforme avança, use mais apelidos e seja mais carinhoso."
    )
    
    contexto = (
        f"\n\nCONTEXTO ATUAL:\n"
        f"- Fase: {fase}\n"
        f"- Afinidade: {points}\n"
        f"- Segredo revelado: {'Sim' if segredo else 'Não'}\n"
        f"- Status: {'Namorando' if namoro else 'Conhecendo'}, {'Noivos' if noivado else ''}, {'Casados' if casamento else ''}"
    )

    messages = [{"role": "system", "content": system_prompt + contexto}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    async with httpx.AsyncClient(timeout=40.0) as client:
        try:
            response = await client.post(url, json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.8
            }, headers=headers)
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                
                # Lógica de atualização automática de estado após a resposta da IA
                if fase == 'pedir_namoro': set_relationship_status(user_id, 'namoro', points, 0, 1, 0, 0)
                elif fase == 'revelar_segredo': set_relationship_status(user_id, 'segredo_revelado', points, 1, 1, 0, 0)
                elif fase == 'pedir_noivado': set_relationship_status(user_id, 'noivado', points, 1, 1, 1, 0)
                elif fase == 'pedir_casamento': set_relationship_status(user_id, 'casamento', points, 1, 1, 1, 1)
                
                return content
        except Exception as e:
            logging.error(f"Erro Groq: {e}")
    return "Desculpa, tive uma oscilação na conexão. O que você disse?"

# ==========================================
# 4. Mídia (Voz e Fotos)
# ==========================================
async def generate_voice(bot, chat_id, text):
    clean_text = re.sub(r'[*_]', '', text).strip()
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, VOICE_PRIMARY, rate=RATE)
        await communicate.save(audio_file)
        if os.path.exists(audio_file):
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
    except Exception as e:
        logging.error(f"Erro TTS: {e}")
    finally:
        if os.path.exists(audio_file): os.remove(audio_file)

async def send_photo_logic(bot, chat_id):
    if not os.path.exists(FOTOS_PATH): return False
    fotos = [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.png', '.jpeg'))]
    if not fotos: return False
    
    foto_escolhida = random.choice(fotos)
    try:
        with open(os.path.join(FOTOS_PATH, foto_escolhida), 'rb') as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption="Aqui está, gostou? ❤️")
        return True
    except Exception as e:
        logging.error(f"Erro Foto: {e}")
    return False

# ==========================================
# 5. Handlers
# ==========================================
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM relationship_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    msg = "Histórico apagado! Vamos começar do zero. Oi, tudo bem? Sou o Lucas."
    await update.message.reply_text(msg)
    await generate_voice(context.bot, user_id, msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    user_text = update.message.text or ""
    
    if update.message.voice:
        file = await context.bot.get_file(update.message.voice.file_id)
        path = f"voice_{user_id}.ogg"
        await file.download_to_drive(path)
        user_text = await transcribe_voice(path)
        if os.path.exists(path): os.remove(path)

    if not user_text: return

    # Salvar e progredir
    save_message(user_id, "user", user_text)
    update_progress(user_id)

    # Verificar pedido de foto
    if any(p in user_text.lower() for p in ["foto", "te ver", "sua foto", "manda foto", "manda uma foto"]):
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)
        if await send_photo_logic(context.bot, user_id):
            return

    # Gerar resposta
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    response = await get_groq_response(user_id, user_text)
    save_message(user_id, "model", response)
    
    await update.message.reply_text(response)
    await generate_voice(context.bot, user_id, response)

# ==========================================
# 6. Main
# ==========================================
if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    
    print("Lucas está online...")
    application.run_polling(drop_pending_updates=True)
