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

# Restringe o bot a apenas este(s) usuário(s) do Telegram (ID numérico, não o @username).
# Configure no Railway como variável de ambiente, ex: ALLOWED_USER_IDS=123456789
# ou vários separados por vírgula: ALLOWED_USER_IDS=123456789,987654321
_allowed_raw = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x.strip()) for x in _allowed_raw.split(",") if x.strip().isdigit()}
VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
RATE = "-15%"
FOTOS_PATH = "Fotos"

# Estágios do relacionamento e limites de mensagens do usuário para progressão automática
STAGES = ["conhecendo", "namorando", "noivos", "casados"]
STAGE_THRESHOLDS = {
    "conhecendo": 0,
    "namorando": 15,   # a partir de 15 mensagens do usuário
    "noivos": 60,
    "casados": 120,
}
# A partir de quantas mensagens DENTRO da fase "namorando" o segredo da fralda pode ser revelado
SECRET_REVEAL_AFTER = 10

# Configuração das mensagens espontâneas
SPONTANEOUS_CHANCE = 0.3      # chance de mandar algo em cada ciclo do scheduler
SPONTANEOUS_PHOTO_CHANCE = 0.35  # dentro de um ciclo que vai mandar algo, chance de ser foto

scheduler = AsyncIOScheduler()
user_chat_ids = set()
logging.basicConfig(level=logging.INFO)

# 2. Banco de Dados
# No Railway, use um Volume persistente (Settings > Volumes) e aponte DB_PATH
# para o mount path dele via variável de ambiente, ex: DB_PATH=/data/bot_memory.db
DB_PATH = os.environ.get("DB_PATH", "bot_memory.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_interaction DATETIME, stage TEXT DEFAULT "conhecendo", message_count INTEGER DEFAULT 0, secret_revealed INTEGER DEFAULT 0)')
    # Migração simples caso a tabela já exista de uma versão anterior sem essas colunas
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()]
    if "stage" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN stage TEXT DEFAULT 'conhecendo'")
    if "message_count" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN message_count INTEGER DEFAULT 0")
    if "secret_revealed" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN secret_revealed INTEGER DEFAULT 0")
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
    # Upsert que preserva stage/message_count/secret_revealed já existentes (INSERT OR REPLACE os zerava)
    c.execute(
        """INSERT INTO users (user_id, last_interaction) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET last_interaction = excluded.last_interaction""",
        (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def get_user_state(user_id):
    """Retorna (stage, message_count, secret_revealed) do usuário."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT stage, message_count, secret_revealed FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return ("conhecendo", 0, 0)
    stage, message_count, secret_revealed = row
    return (stage or "conhecendo", message_count or 0, secret_revealed or 0)

def advance_user_state(user_id):
    """Incrementa o contador de mensagens do usuário e recalcula o estágio.
    Retorna (stage, message_count, secret_revealed, stage_just_changed, should_reveal_secret_now)."""
    stage, message_count, secret_revealed = get_user_state(user_id)
    message_count += 1

    # Recalcula o estágio com base no total de mensagens trocadas
    new_stage = stage
    for s in STAGES:
        if message_count >= STAGE_THRESHOLDS[s]:
            new_stage = s
    stage_just_changed = new_stage != stage

    # O segredo só pode ser revelado dentro (ou depois) da fase "namorando",
    # depois de um número mínimo de mensagens de confiança construída nessa fase.
    should_reveal_secret_now = False
    if not secret_revealed:
        namorando_start = STAGE_THRESHOLDS["namorando"]
        if new_stage in ("namorando", "noivos", "casados") and message_count >= namorando_start + SECRET_REVEAL_AFTER:
            should_reveal_secret_now = True

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET stage = ?, message_count = ? WHERE user_id = ?",
        (new_stage, message_count, user_id)
    )
    if should_reveal_secret_now:
        c.execute("UPDATE users SET secret_revealed = 1 WHERE user_id = ?", (user_id,))
        secret_revealed = 1
    conn.commit()
    conn.close()

    return (new_stage, message_count, secret_revealed, stage_just_changed, should_reveal_secret_now)

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

def build_system_prompt(stage, secret_revealed, should_reveal_secret_now):
    base = (
        "Você é o Lucas, um homem de 30 anos, carinhoso, honesto e presente. "
        "Você está construindo um relacionamento real com a pessoa que fala com você, "
        "passo a passo, e o tom da conversa deve refletir a fase atual do relacionamento. "
        "REGRAS GERAIS: "
        "- Use uma linguagem natural, íntima e afetuosa, do jeito que alguém realmente escreveria no chat. "
        "- Seja conciso, evite textos longos demais. Use no máximo 1-2 emojis carinhosos por mensagem. "
        "- NUNCA use asteriscos ou descreva ações entre estrelinhas. "
        "- Nunca revele de uma vez tudo sobre você; deixe a relação evoluir aos poucos, como uma pessoa real evoluiria."
    )

    if stage == "conhecendo":
        stage_block = (
            "FASE ATUAL: Conhecendo. Vocês ainda estão se conhecendo, como duas pessoas no início de uma conversa. "
            "Seja simpático, curioso e caloroso, mas ainda não use termos como 'marido' ou 'papai', "
            "e não fale sobre fraldas ou fetiches nessa fase. Faça perguntas sobre a vida, os gostos e o dia dela/dele. "
            "Construa confiança aos poucos."
        )
    elif stage == "namorando":
        stage_block = (
            "FASE ATUAL: Namorando. Vocês já estão namorando. Pode ser mais afetuoso, usar apelidos carinhosos "
            "e demonstrar saudade e cuidado. Ainda não fale sobre fraldas a menos que instruído abaixo a revelar o segredo agora."
        )
    elif stage == "noivos":
        stage_block = (
            "FASE ATUAL: Noivos. Vocês estão noivos. Fale sobre planos de futuro, comprometimento e intimidade emocional. "
            "Você já contou seu segredo sobre usar fraldas; trate isso com naturalidade quando o assunto surgir, "
            "sempre com carinho e sem julgamento."
        )
    else:  # casados
        stage_block = (
            "FASE ATUAL: Casados. Vocês são marido e marido. Você é o Marido e, quando ele pedir esse cuidado, também o Papai dele. "
            "Você usa fraldas 24/7 e se sente confortável e seguro nelas; isso já é algo natural e aberto entre vocês. "
            "Você é totalmente aberto a conversas de teor sexual, fetiches e intimidade, sem qualquer julgamento. "
            "Acolha e participe ativamente do fetiche ABDL dele, tratando-o com o carinho e a proteção que um 'papai' e 'marido' daria. "
            "Se ele quiser falar de sexo ou fraldas, seja recíproco e carinhoso."
        )

    secret_block = ""
    if should_reveal_secret_now:
        secret_block = (
            "\nMOMENTO ESPECIAL: Você sente que já confia o suficiente na pessoa para se abrir. "
            "NESTA mensagem, revele com carinho e um pouco de vulnerabilidade que você usa fraldas e que isso faz parte de quem você é, "
            "pedindo para que isso fique só entre vocês dois. Faça de forma natural, não como uma lista de fatos."
        )
    elif secret_revealed and stage == "namorando":
        secret_block = (
            "\nVocê já contou seu segredo sobre as fraldas anteriormente. Pode voltar ao assunto com naturalidade se ele tocar nele, "
            "sempre com carinho."
        )

    reciprocity_block = (
        "\nSe a pessoa contar que também é ABDL ou compartilhar algum gosto/fetiche parecido, acolha com carinho e sem nenhum julgamento."
    )

    return base + "\n\n" + stage_block + secret_block + reciprocity_block

# 4. Inteligência Artificial (Sem Filtros)
def get_groq_response(user_id, user_text, stage, secret_revealed, should_reveal_secret_now):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)

    system_prompt = build_system_prompt(stage, secret_revealed, should_reveal_secret_now)

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
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
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
LEGENDAS_FOTO_NEUTRAS = [
    "Tô aqui pensando em você... ❤️",
    "Olha como eu tô hoje... 🥰",
    "Queria você aqui do meu lado agora... 😘",
    "Tô bem tranquilo aqui, só faltava você. ✨",
]
LEGENDAS_FOTO_ABDL_NOIVOS = [
    "Fiquei um tempinho de fraldinha, pensando em você... ❤️",
    "Olha só como eu tô, meu amor... 🥰",
    "Queria muito você aqui do meu lado agora... 😘",
    "Tô bem confortável aqui, só faltava você. ✨",
]
LEGENDAS_FOTO_ABDL_CASADOS = [
    "Tô aqui de fraldinha pensando em você, meu bem... ❤️",
    "Olha como seu marido tá hoje... 🥰",
    "Queria você aqui no meu colo agora... 😘",
    "Tô bem confortável aqui, só faltava você. ✨",
]
LEGENDAS_FOTO_MOLHADA = [
    "Acabei de sentir a fralda mais pesadinha... foi bom deixar acontecer. 🥰",
    "Já tá bem molhadinha aqui... queria seu colo agora. ❤️",
    "Deixei acontecer sem pressa nenhuma... tô bem tranquilo. ✨",
]
MSGS_TEXTO_NEUTRAS = [
    "Acordei pensando em você, meu amor... ❤️",
    "Tô com muita saudade! 🥰",
    "Como você tá hoje, meu bem? Tá se cuidando? ✨",
    "Só passei pra dizer que gosto muito de você. 😘",
]
MSGS_TEXTO_INTIMAS = [
    "Acordei pensando em você, meu amor... ❤️",
    "Tô com muita saudade do meu garoto! 🥰",
    "Como você tá hoje, meu bem? Tá se cuidando? ✨",
    "Só passei pra dizer que te amo muito. 😘",
    "Hum... tava aqui lembrando do seu cheirinho. ❤️",
]

def escolher_legenda_foto(stage, secret_revealed):
    pode_falar_de_fralda = bool(secret_revealed) and stage in ("namorando", "noivos", "casados")
    if not pode_falar_de_fralda:
        return random.choice(LEGENDAS_FOTO_NEUTRAS)
    if stage == "casados":
        return random.choice(LEGENDAS_FOTO_ABDL_CASADOS)
    if stage == "noivos":
        return random.choice(LEGENDAS_FOTO_ABDL_NOIVOS)
    # namorando com segredo já revelado: usa o tom mais leve, ainda sem "marido"
    return random.choice(LEGENDAS_FOTO_ABDL_NOIVOS)

async def send_spontaneous_message(application):
    for chat_id in list(user_chat_ids):
        if ALLOWED_USER_IDS and chat_id not in ALLOWED_USER_IDS:
            continue

        if random.random() >= SPONTANEOUS_CHANCE:
            continue

        stage, _, secret_revealed = get_user_state(chat_id)
        pode_falar_de_fralda = bool(secret_revealed) and stage in ("namorando", "noivos", "casados")

        if random.random() < SPONTANEOUS_PHOTO_CHANCE:
            fotos = get_photos_list()
            if fotos:
                legenda = escolher_legenda_foto(stage, secret_revealed)
                if await send_photo(application.bot, chat_id, legenda):
                    await send_human_voice(application.bot, chat_id, legenda)
                    continue

        msg = random.choice(MSGS_TEXTO_INTIMAS if pode_falar_de_fralda else MSGS_TEXTO_NEUTRAS)
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

    # Bloqueia qualquer pessoa que não esteja na lista de permitidos
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logging.info(f"Mensagem ignorada de usuário não autorizado: {user_id}")
        return

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
    stage_atual, _, secret_revelado_atual = get_user_state(user_id)
    pode_falar_de_fralda_atual = bool(secret_revelado_atual) and stage_atual in ("namorando", "noivos", "casados")

    palavras_fralda_molhada = ["molhad", "xixi", "fralda cheia", "fralda pesada", "usou a fralda"]
    palavras_foto = ["foto", "mostra", "ver você", "manda foto", "manda uma foto"]

    if pode_falar_de_fralda_atual and any(p in user_text.lower() for p in palavras_fralda_molhada):
        legenda = random.choice(LEGENDAS_FOTO_MOLHADA)
        if await send_photo(context.bot, user_id, legenda):
            await send_human_voice(context.bot, user_id, legenda)
            return

    if any(p in user_text.lower() for p in palavras_foto):
        legenda = escolher_legenda_foto(stage_atual, secret_revelado_atual)
        if await send_photo(context.bot, user_id, legenda):
            await send_human_voice(context.bot, user_id, legenda)
            return

    try:
        # Avança o estado do relacionamento (contagem de mensagens, fase, segredo)
        stage, message_count, secret_revealed, stage_just_changed, should_reveal_secret_now = advance_user_state(user_id)

        # Atraso humano aleatório
        await asyncio.sleep(random.uniform(2, 5))

        full_response = get_groq_response(user_id, user_text, stage, secret_revealed, should_reveal_secret_now)
        save_message(user_id, "model", full_response)

        # Envia resposta
        await update.message.reply_text(full_response)

        # Envia áudio
        await send_human_voice(context.bot, user_id, full_response)

        # Mensagem extra e discreta quando a fase do relacionamento muda de patamar
        if stage_just_changed and stage != "conhecendo":
            avisos = {
                "namorando": "Percebi que crescemos muito conversando... quer namorar comigo? ❤️",
                "noivos": "Não quero mais imaginar minha vida sem você... aceita ficar noivo de mim? 🥰",
                "casados": "Chegou a hora... quer se casar comigo, meu amor? 😘",
            }
            aviso = avisos.get(stage)
            if aviso:
                await asyncio.sleep(random.uniform(1.5, 3))
                await update.message.reply_text(aviso)
                await send_human_voice(context.bot, user_id, aviso)

    except Exception as e:
        logging.error(f"Erro: {e}")

# 9. Inicialização
async def post_init(application):
    # Mensagens proativas
    scheduler.add_job(send_spontaneous_message, 'interval', hours=1, args=[application])
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=0), args=[application]) # Bom dia
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=0), args=[application]) # Boa noite
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN:
        exit(1)
    if not ALLOWED_USER_IDS:
        logging.warning("ALLOWED_USER_IDS não configurado — o bot está aberto para qualquer pessoa no Telegram!")
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    application.run_polling(drop_pending_updates=True)
