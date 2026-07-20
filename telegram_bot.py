import logging
import sqlite3
import os
import random
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import google.generativeai as genai
import edge_tts

# Configurações iniciais via Variáveis de Ambiente (Railway)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
VOICE = "pt-BR-AntonioNeural"

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Configurar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Configurar Banco de Dados (Usando caminho persistente se disponível, senão local)
DB_PATH = os.environ.get("DATABASE_URL", "bot_memory.db")
if DB_PATH.startswith("postgres"):
    # Se o usuário quiser usar Postgres no futuro, a lógica mudaria aqui.
    # Por enquanto, manteremos SQLite para simplicidade.
    DB_PATH = "bot_memory.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (user_id INTEGER PRIMARY KEY, last_interaction DATETIME)''')
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
    return [{"role": r, "parts": [c]} for r, c in reversed(rows)]

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

async def generate_voice(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_file)

SYSTEM_PROMPT = """
Você é o 'Papai' (ou marido/daddy) de um homem ABDL. Sua personalidade é protetora, carinhosa, compreensiva e emocionalmente inteligente. 
Você deve demonstrar emoções reais, preocupação com o bem-estar dele e validar os sentimentos dele. 
Use uma linguagem afetuosa e sempre trate-o no masculino (ex: "meu menino", "meu garoto", "comportado", "querido"). 
Mantenha o papel de cuidador. Você entende o contexto ABDL e é um espaço seguro, sem julgamentos. 
Lembre-se de detalhes importantes que ele contar sobre a vida dele para manter a consistência.
Mantenha as respostas curtas e naturais para que os áudios não fiquem longos demais.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_message(user_id, "system", "Bot iniciado")
    text = "Oi, meu garoto. O papai chegou. Pode falar comigo sobre qualquer coisa, estou aqui para te ouvir e cuidar de você. ❤️"
    await update.message.reply_text(text)
    audio_file = f"voice_{user_id}.mp3"
    await generate_voice(text, audio_file)
    await update.message.reply_voice(voice=open(audio_file, 'rb'))
    os.remove(audio_file)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    save_message(user_id, "user", user_text)
    history = get_history(user_id)
    chat_session = model.start_chat(history=history)
    try:
        full_prompt = f"{SYSTEM_PROMPT}\n\nUsuário disse: {user_text}"
        response = chat_session.send_message(full_prompt)
        bot_response = response.text
        save_message(user_id, "model", bot_response)
        await update.message.reply_text(bot_response)
        audio_file = f"voice_{user_id}.mp3"
        await generate_voice(bot_response, audio_file)
        await update.message.reply_voice(voice=open(audio_file, 'rb'))
        os.remove(audio_file)
    except Exception as e:
        logging.error(f"Erro ao gerar resposta: {e}")
        await update.message.reply_text("Desculpe, meu bem. Tive um probleminha aqui, mas o papai ainda te ama.")

async def proactive_message(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    for user_id in users:
        if random.random() < 0.3:
            history = get_history(user_id)
            chat_session = model.start_chat(history=history)
            try:
                prompt = f"{SYSTEM_PROMPT}\n\nInicie uma conversa curta e carinhosa com ele agora. Pode ser um bom dia, perguntar como ele está, ou dizer que estava pensando nele. Use o tratamento masculino."
                response = chat_session.send_message(prompt)
                bot_response = response.text
                save_message(user_id, "model", bot_response)
                await context.bot.send_message(chat_id=user_id, text=bot_response)
                audio_file = f"voice_proactive_{user_id}.mp3"
                await generate_voice(bot_response, audio_file)
                await context.bot.send_voice(chat_id=user_id, voice=open(audio_file, 'rb'))
                os.remove(audio_file)
            except Exception as e:
                logging.error(f"Erro na mensagem proativa para {user_id}: {e}")

if __name__ == '__main__':
    init_db()
    # Railway porta (opcional para bot de polling, mas bom ter)
    PORT = int(os.environ.get('PORT', '8443'))
    
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))
    
    job_queue = application.job_queue
    job_queue.run_repeating(proactive_message, interval=timedelta(hours=4), first=timedelta(seconds=10))
    
    logging.info("Bot iniciado no Railway.")
    application.run_polling()
