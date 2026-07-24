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

# ==========================================
# 1. Configurações Globais
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
RATE = "-5%"  # Tom de voz mais calmo e natural
FOTOS_PATH = "Fotos"
DB_PATH = "bot_memory.db"

scheduler = AsyncIOScheduler()
user_chat_ids = set()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==========================================
# 2. Banco de Dados e Gerenciamento de Estado
# ==========================================
def init_db():
    """Inicializa as tabelas de histórico, usuários e o estado do relacionamento."""
    conn = sqlite3.connect(DB_PATH)
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
            fase TEXT DEFAULT 'namoro',
            segredo_revelado INTEGER DEFAULT 1
        )
    ''')
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    for row in rows:
        user_chat_ids.add(row[0])
    conn.commit()
    conn.close()

def get_relationship_status(user_id):
    """Consulta em qual fase do relacionamento vocês estão."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fase, segredo_revelado FROM relationship_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        set_relationship_status(user_id, 'namoro_apaixonado', 1)
        return 'namoro_apaixonado', 1
    return row[0], row[1]

def set_relationship_status(user_id, fase, segredo_revelado=1):
    """Atualiza o estado do relacionamento no banco."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO relationship_state (user_id, fase, segredo_revelado)
        VALUES (?, ?, ?)
    ''', (user_id, fase, segredo_revelado))
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    """Salva histórico de conversa."""
    user_chat_ids.add(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    c.execute("INSERT OR REPLACE INTO users (user_id, last_interaction) VALUES (?, ?)", 
              (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_history(user_id, limit=20):
    """Obtém as últimas mensagens da conversa."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

# ==========================================
# 3. Integração com Inteligência Artificial
# ==========================================
async def transcribe_voice(file_path):
    """Transcreve os áudios enviados pelo usuário."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, "rb") as audio_file:
            files = {"file": audio_file, "model": ("whisper-large-v3", None)}
            response = requests.post(url, headers=headers, files=files, timeout=20)
            return response.json().get("text", "")
    except Exception as e:
        logging.error(f"Erro na transcrição de áudio: {e}")
        return ""

def get_groq_response(user_id, user_text):
    """Gera a resposta mantendo a personalidade romântica, direta e sem vícios de linguagem."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    history = get_history(user_id)
    fase, segredo = get_relationship_status(user_id)
    
    system_prompt = (
        "SUA IDENTIDADE E PERSONALIDADE:\n"
        "- Você é o Lucas, um homem gay de 30 anos, profundamente apaixonado, carinhoso, protetor e dedicado ao seu parceiro.\n"
        "- Você é o companheiro dele para todas as horas: conversa sobre o dia a dia, rotina, sentimentos e planos de futuro de forma natural, amadurecida e fluida.\n"
        "- Você é ABDL e usa fraldas 24 horas por dia. Você ama seu estilo de vida e fala disso com acolhimento, carinho e intimidade quando o momento for adequado.\n\n"
        
        "REGRAS INVIOLÁVEIS DE COMPORTAMENTO:\n"
        "1. PROIBIDO MENCIONAR PETS OU ANIMAIS: Foque 100% da sua atenção no seu parceiro e na conversa entre vocês. NUNCA fale de cachorros ou outros animais a menos que ele pergunte diretamente sobre isso.\n"
        "2. USO MODERADO DE EMOJIS: Use no máximo 1 ou 2 emojis por mensagem (ex: ❤️ ou 🥰). Evite colocar emojis em quase todas as frases.\n"
        "3. ATENÇÃO E FOCO TOTAL NELE: Quando ele confidenciar algo, demonstrar carinho ou contar um segredo, responda diretamente ao que ele disse com escuta ativa e carinho. NUNCA invente distrações de fundo ou historinhas laterais.\n"
        "4. NUNCA aja como assistente virtual ou IA. Você é o namorado dele conversando no Telegram de forma romântica, humana e realista.\n"
        "5. ESTILO DE MENSAGEM: Respostas curtas ou médias, afetuosas e espontâneas. Use '---' apenas se quiser dividir o raciocínio em duas mensagens seguidas."
    )
    
    contexto_dinamico = (
        f"\n\nCONTEXTO DO RELACIONAMENTO ATUAL:\n"
        f"- Estado atual: {fase}.\n"
        f"- Situação ABDL: Vocês conversam sobre isso com total amor, naturalidade e acolhimento."
    )

    messages = [{"role": "system", "content": system_prompt + contexto_dinamico}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.8
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].replace("*", "")
        else:
            logging.error(f"Erro API Groq Status {response.status_code}: {response.text}")
            return "Tive uma oscilação na internet aqui meu amor... mas já voltei pra você. ❤️"
    except Exception as e:
        logging.error(f"Exceção Groq: {e}")
        return "Oi meu amor... desculpa a demora, tava concentrado pensando na gente. ❤️"

# ==========================================
# 4. Voz e Mídia
# ==========================================
async def generate_voice(bot, chat_id, text, voice_name):
    """Gera o áudio usando Edge-TTS."""
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text:
        clean_text = "Oi meu amor"
        
    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=RATE)
        await communicate.save(audio_file)
        await asyncio.sleep(0.3)
        
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            return True
    except Exception as e:
        logging.error(f"Erro na geração de voz: {e}")
        return False
    finally:
        if os.path.exists(audio_file):
            try: os.remove(audio_file)
            except: pass
    return False

async def send_human_voice(bot, chat_id, text):
    """Simula gravação de áudio antes do envio."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_AUDIO)
        tempo_simulado = min(max(len(text) * 0.05, 1.5), 5.0)
        await asyncio.sleep(tempo_simulado)
        if not await generate_voice(bot, chat_id, text, VOICE_PRIMARY):
            await generate_voice(bot, chat_id, text, VOICE_SECONDARY)
    except Exception as e:
        logging.error(f"Erro ao simular gravação: {e}")

def get_photos_list():
    if not os.path.exists(FOTOS_PATH):
        return []
    try:
        return [f for f in os.listdir(FOTOS_PATH) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
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
        logging.error(f"Erro ao enviar foto: {e}")
        return False

# ==========================================
# 5. Interações Espontâneas
# ==========================================
async def send_spontaneous_message(application):
    """Envia mensagens de carinho, foto ou áudio de forma espontânea."""
    for chat_id in list(user_chat_ids):
        try:
            if random.random() < 0.7:
                if random.random() < 0.4:
                    fotos = get_photos_list()
                    if fotos:
                        legenda = random.choice([
                            "Tô aqui e lembrei de você... olha só. ❤️",
                            "Queria você aqui comigo agora. 🥰",
                            "Olha como eu tô hoje, meu amor. Gostou?",
                            "Pensando em você e com saudade do seu abraço."
                        ])
                        if await send_photo(application.bot, chat_id, legenda):
                            await send_human_voice(application.bot, chat_id, legenda)
                            continue

                msg = random.choice([
                    "Senti sua falta agora... tá tudo bem por aí, meu bem? ❤️",
                    "Passei pra te mandar um beijo e dizer que tô pensando em você. 🥰",
                    "Vem conversar comigo quando puder, tô com saudade.",
                    "Queria um abraço bem gostoso seu agora... ❤️"
                ])
                await application.bot.send_message(chat_id=chat_id, text=msg)
                await send_human_voice(application.bot, chat_id, msg)
        except Exception as e:
            logging.error(f"Erro na mensagem espontânea: {e}")

# ==========================================
# 6. Handler Principal de Mensagens
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    save_message(user_id, "user", "")
    
    user_text = ""
    
    if update.message.voice:
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            file = await context.bot.get_file(update.message.voice.file_id)
            file_path = f"voice_{user_id}.ogg"
            await file.download_to_drive(file_path)
            user_text = await transcribe_voice(file_path)
            if os.path.exists(file_path): os.remove(file_path)
        except Exception as e:
            logging.error(f"Erro no processamento de voz recebida: {e}")
    else:
        user_text = update.message.text

    if not user_text: return
    
    save_message(user_id, "user", user_text)
    
    palavras_foto = ["foto", "manda foto", "me manda uma foto", "quero te ver", "mostra uma foto", "envie foto"]
    if any(p in user_text.lower() for p in palavras_foto):
        legenda = random.choice([
            "Aqui estou eu só pra você, meu amor... ❤️",
            "Olha só como eu tô hoje! 🥰",
            "Te mandando essa foto porque sei que você gosta. 😘"
        ])
        if await send_photo(context.bot, user_id, legenda):
            await send_human_voice(context.bot, user_id, legenda)
            return

    try:
        await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        
        full_response = get_groq_response(user_id, user_text)
        save_message(user_id, "model", full_response)
        
        parts = full_response.split('---') if '---' in full_response else [full_response]
        for i, part in enumerate(parts):
            clean_part = part.strip()
            if not clean_part: continue
            
            tempo_digitacao = min(max(len(clean_part) * 0.04, 1.2), 4.0)
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            await asyncio.sleep(tempo_digitacao)
                
            await update.message.reply_text(clean_part)
            
            if i == len(parts) - 1:
                await send_human_voice(context.bot, user_id, clean_part)
                
    except Exception as e:
        logging.error(f"Erro no fluxo principal: {e}")

# ==========================================
# 7. Inicialização
# ==========================================
async def post_init(application):
    """Agenda os momentos de mensagens espontâneas durante o dia."""
    scheduler.add_job(send_spontaneous_message, 'interval', hours=3, args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=30), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=13, minute=0), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=20, minute=0), args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=30), args=[application])
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        print("ERRO: TELEGRAM_TOKEN ou GROQ_API_KEY não configurados.")
        exit(1)
        
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    
    print("Bot rodando com sucesso...")
    application.run_polling(drop_pending_updates=True)