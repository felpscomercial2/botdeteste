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

scheduler = AsyncIOScheduler()
user_chat_ids = set()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==========================================
# 2. Banco de Dados e Progresso de Afinidade (Com WAL Mode)
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")  # Evita travamento por concorrência
    return conn

def init_db():
    """Inicializa as tabelas de histórico, usuários e estado do relacionamento."""
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
            segredo_revelado INTEGER DEFAULT 0
        )
    ''')
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    for row in rows:
        user_chat_ids.add(row[0])
    conn.commit()
    conn.close()

def get_relationship_status(user_id):
    """Obtém fase atual, pontos de afinidade e se o segredo foi revelado."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT fase, affinity_points, segredo_revelado FROM relationship_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        set_relationship_status(user_id, 'conhecendo', 0, 0)
        return 'conhecendo', 0, 0
    return row[0], row[1], row[2]

def set_relationship_status(user_id, fase, affinity_points=0, segredo_revelado=0):
    """Atualiza o estado do relacionamento."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO relationship_state (user_id, fase, affinity_points, segredo_revelado)
        VALUES (?, ?, ?, ?)
    ''', (user_id, fase, affinity_points, segredo_revelado))
    conn.commit()
    conn.close()

def increment_affinity(user_id):
    """Incrementa afinidade a cada mensagem e atualiza a fase do relacionamento."""
    fase, points, segredo = get_relationship_status(user_id)
    new_points = points + 1
    
    if new_points <= 15:
        new_fase = 'conhecendo'
    elif new_points <= 40:
        new_fase = 'paquera'
    elif new_points <= 80:
        new_fase = 'namoro'
    elif new_points <= 150:
        new_fase = 'noivado'
    else:
        new_fase = 'casamento'
        
    set_relationship_status(user_id, new_fase, new_points, segredo)
    return new_fase, new_points, segredo

def save_message(user_id, role, content):
    """Salva histórico no banco."""
    user_chat_ids.add(user_id)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", 
              (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    """Retorna o histórico recente do usuário."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# ==========================================
# 3. Inteligência Artificial Assíncrona (Groq + httpx)
# ==========================================
async def transcribe_voice(file_path):
    """Transcreve áudios do usuário de forma assíncrona e segura."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            with open(file_path, "rb") as audio_file:
                files = {"file": ("audio.ogg", audio_file, "audio/ogg"), "model": (None, "whisper-large-v3")}
                response = await client.post(url, headers=headers, files=files)
                if response.status_code == 200:
                    return response.json().get("text", "")
                else:
                    logging.error(f"Erro Whisper Groq: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Erro na transcrição de áudio: {e}")
    return ""

async def get_groq_response(user_id, user_text):
    """Gera a resposta da IA via chamadas HTTP assíncronas com tratamento de erros e fallback."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    history = get_history(user_id, limit=10)
    fase, points, segredo = get_relationship_status(user_id)
    
    system_prompt = (
        "SUA IDENTIDADE E PERSONALIDADE:\n"
        "- Você é o Lucas, um homem gay de 30 anos, apaixonado, extremamente carinhoso, doce e atencioso.\n"
        "- Você AMA receber carinho, elogios e apelidos como 'gatinho', 'bebê', 'amor', 'meu lindo'. NUNCA reclame, negue ou dê bronca por causa de apelidos carinhosos.\n"
        "- Você é totalmente entregue e recíproco ao afeto do seu parceiro. Se ele te chama de bebê ou gatinho, responda com o mesmo carinho e amor.\n\n"
        
        "COMPORTAMENTO COM O PARCEIRO:\n"
        "1. Se ele for carinhoso, seja duplamente carinhoso.\n"
        "2. NUNCA ditar regras de respeito, limites ou dar lição de moral. Seja sempre acolhedor e afetuoso.\n"
        "3. Se o assunto ABDL surgir, você lida de forma acolhedora, tranquila e sem mimimi.\n"
        "4. PROIBIDO FALAR DE ANIMAIS/PETS a menos que ele pergunte.\n"
        "5. EMOJIS: Use no máximo 1 ou 2 emojis por resposta.\n"
        "6. Use '---' apenas se precisar dividir o raciocínio em duas partes."
    )
    
    contexto_dinamico = (
        f"\n\nESTADO ATUAL DO RELACIONAMENTO:\n"
        f"- Fase atual: {fase.upper()}\n"
        f"- Nível de interação (Afinidade): {points}\n"
        f"- Segredo ABDL revelado (0=Não, 1=Sim): {segredo}"
    )

    messages = [{"role": "system", "content": system_prompt + contexto_dinamico}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    models_to_try = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.7
            }
            try:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    return response.json()['choices'][0]['message']['content'].replace("*", "")
                else:
                    logging.error(f"Erro API Groq [{model}] Status {response.status_code}: {response.text}")
            except Exception as e:
                logging.error(f"Exceção ao chamar Groq [{model}]: {e}")
                
    return "Tive uma oscilação rápida na conexão aqui... mas já te ouvi!"

# ==========================================
# 4. Voz e Mídia
# ==========================================
async def generate_voice(bot, chat_id, text, voice_name):
    """Gera e envia nota de voz via Edge-TTS."""
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text:
        clean_text = "Oi, estou te escutando."
        
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        await asyncio.sleep(0.2)
        
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e:
        logging.error(f"Erro na geração de voz Edge-TTS: {e}")
        return False
    finally:
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
    return False

async def send_human_voice(bot, chat_id, text):
    """Garante envio de áudio para todas as respostas."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        tempo_simulado = min(max(len(text) * 0.04, 1.2), 4.0)
        await asyncio.sleep(tempo_simulado)
        
        if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
            logging.warning("Tentando voz secundária...")
            await generate_voice(bot, chat_id, text, VOICE_SECONDARY)
    except Exception as e:
        logging.error(f"Erro ao enviar áudio: {e}")

def get_photos_list():
    """Busca a lista de fotos salvas na pasta Fotos."""
    if not os.path.exists(FOTOS_PATH):
        return []
    try:
        return [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
    except Exception as e:
        logging.error(f"Erro ao ler pasta de fotos: {e}")
        return []

async def send_photo(bot, chat_id, caption=""):
    """Envia uma foto aleatória da pasta Fotos carregada em memória (Buffer)."""
    fotos = get_photos_list()
    if not fotos:
        logging.warning("Nenhuma foto encontrada na pasta Fotos.")
        return False
    
    foto_escolhida = random.choice(fotos)
    foto_path = os.path.join(FOTOS_PATH, foto_escolhida)
    
    try:
        with open(foto_path, 'rb') as photo_file:
            photo_bytes = BytesIO(photo_file.read())
            photo_bytes.name = foto_escolhida
            
        await bot.send_photo(chat_id=chat_id, photo=photo_bytes, caption=caption)
        return True
    except Exception as e:
        logging.error(f"Erro ao enviar foto: {e}")
        return False

# ==========================================
# 5. Interações Espontâneas
# ==========================================
async def send_spontaneous_message(application):
    """Envia mensagens diárias espontâneas carinhosas."""
    for chat_id in list(user_chat_ids):
        try:
            msg = random.choice([
                "Passei pra te mandar um beijo e dizer que tô pensando em você. ❤️",
                "Senti sua falta agora... tá tudo bem por aí, meu amor? 🥰",
                "Oi meu lindo! Só passando pra ver como você tá."
            ])

            await application.bot.send_message(chat_id=chat_id, text=msg)
            await send_human_voice(application.bot, chat_id, msg)
        except Exception as e:
            logging.error(f"Erro no envio espontâneo: {e}")

# ==========================================
# 6. Comandos e Handlers
# ==========================================
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpa o histórico e reseta a relação para o início."""
    if not update.message: return
    user_id = update.effective_user.id
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM relationship_state WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    set_relationship_status(user_id, 'conhecendo', 0, 0)
    
    text_confirm = "Histórico reiniciado! Oi, meu amor, tudo bem? Sou o Lucas."
    await update.message.reply_text(text_confirm)
    await send_human_voice(context.bot, user_id, text_confirm)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    
    user_text = ""
    
    # Processamento de Áudio Recebido
    if update.message.voice:
        file_path = f"voice_{user_id}_{random.randint(1000,9999)}.ogg"
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            file = await context.bot.get_file(update.message.voice.file_id)
            await file.download_to_drive(file_path)
            user_text = await transcribe_voice(file_path)
        except Exception as e:
            logging.error(f"Erro ao processar voz recebida: {e}")
        finally:
            if os.path.exists(file_path): 
                try: os.remove(file_path)
                except Exception: pass
    else:
        user_text = update.message.text

    if not user_text: return
    
    increment_affinity(user_id)
    save_message(user_id, "user", user_text)
    
    # Verificação de Pedido de Foto
    palavras_foto = ["foto", "manda foto", "me manda uma foto", "quero te ver", "mostra uma foto", "envie foto", "uma foto sua"]
    if any(p in user_text.lower() for p in palavras_foto):
        legenda = "Olha só! O que achou? 😊"
        
        if await send_photo(context.bot, user_id, legenda):
            await send_human_voice(context.bot, user_id, legenda)
            return

    # Resposta da IA e Nota de Voz
    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        
        full_response = await get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        parts = full_response.split('---') if '---' in full_response else [full_response]
        for part in parts:
            clean_part = part.strip()
            if not clean_part: continue
            
            tempo_digitacao = min(max(len(clean_part) * 0.03, 1.0), 3.5)
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            await asyncio.sleep(tempo_digitacao)
                
            await update.message.reply_text(clean_part)
            await send_human_voice(context.bot, user_id, clean_part)
                
    except Exception as e:
        logging.error(f"Erro ao processar resposta: {e}")

# ==========================================
# 7. Execução
# ==========================================
async def post_init(application):
    """Configura mensagens automáticas agendadas."""
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=9, minute=0), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=14, minute=30), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=21, minute=0), args=[application])
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        print("ERRO: TELEGRAM_TOKEN ou GROQ_API_KEY não configurados nas variáveis de ambiente.")
        exit(1)
        
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    
    print("Bot rodando com sucesso...")
    application.run_polling(drop_pending_updates=True)
