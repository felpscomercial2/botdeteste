import logging
import sqlite3
import os
import random
import asyncio
import requests
import re
import hashlib
from datetime import datetime
from telegram import Update, ReactionTypeEmoji
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import edge_tts
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 1. Configurações
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Nome do modelo de chat da Groq, centralizado aqui — a Groq descontinua modelos periodicamente
# (foi o caso do llama-3.3-70b-versatile em jun/2026), então só precisa trocar em um lugar.
GROQ_MODEL = "openai/gpt-oss-120b"

# Restringe o bot a apenas este(s) usuário(s) do Telegram (ID numérico, não o @username).
# Configure no Railway como variável de ambiente, ex: ALLOWED_USER_IDS=123456789
# ou vários separados por vírgula: ALLOWED_USER_IDS=123456789,987654321
_allowed_raw = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x.strip()) for x in _allowed_raw.split(",") if x.strip().isdigit()}
VOICE_PRIMARY = "pt-BR-DonatoNeural"
VOICE_SECONDARY = "pt-BR-AntonioNeural"
# Vozes masculinas gratuitas em pt-BR (mesmo motor neural da Microsoft, via Edge). O bot roda por essa
# lista até uma funcionar, o que também dá variedade natural entre mensagens.
VOICES_MASCULINAS = [
    "pt-BR-DonatoNeural",
    "pt-BR-AntonioNeural",
    "pt-BR-FabioNeural",
    "pt-BR-HumbertoNeural",
    "pt-BR-JulioNeural",
    "pt-BR-NicolauNeural",
    "pt-BR-ValerioNeural",
]
# Faixas de variação de ritmo/tom para soar mais natural (evita o tom robótico de rate fixo)
RATE_RANGE = (-10, 3)     # em %
PITCH_RANGE = (-12, 12)   # em Hz
FOTOS_PATH = "Fotos"
TTS_CACHE_PATH = "tts_cache"
# Só vale cachear textos curtos (frases fixas tipo bom dia/legendas) — respostas
# geradas pelo LLM são praticamente sempre únicas, então cachear não ajudaria.
TTS_CACHE_MAX_CHARS = 120

# Estágios do relacionamento e limites de mensagens do usuário para progressão automática
STAGES = ["conhecendo", "namorando", "noivos", "casados"]
STAGE_THRESHOLDS = {
    "conhecendo": 0,
    "namorando": 30,   # a partir de 30 mensagens do usuário
    "noivos": 110,
    "casados": 220,
}
# A partir de quantas mensagens DENTRO da fase "namorando" o segredo da fralda pode ser revelado
SECRET_REVEAL_AFTER = 10

# Configuração das mensagens espontâneas
SPONTANEOUS_CHANCE = 0.3      # chance de mandar algo em cada ciclo do scheduler
SPONTANEOUS_PHOTO_CHANCE = 0.35  # dentro de um ciclo que vai mandar algo, chance de ser foto
# Janela de horário em que o job de intervalo (a cada hora) pode mandar mensagem — evita
# mandar "bom dia" às 3h da manhã. Os jobs fixos de 8h/22h não usam essa janela.
SPONTANEOUS_WINDOW_START_HOUR = 9
SPONTANEOUS_WINDOW_END_HOUR = 21

scheduler = AsyncIOScheduler()
user_chat_ids = set()
# Últimas fotos enviadas por usuário (em memória), pra evitar repetir a mesma foto em sequência
RECENT_PHOTOS_LIMIT = 5
recent_photos_sent = {}
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
    # Fatos fixos extraídos da conversa (nome, gostos, datas importantes etc.), separados do
    # histórico bruto — assim não se perdem quando saem da janela das últimas N mensagens.
    c.execute('CREATE TABLE IF NOT EXISTS facts (user_id INTEGER, fact TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
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

def reset_user_state(user_id, wipe_history=False):
    """Zera fase/contador/segredo do usuário (pra testar sem precisar mandar 30+ mensagens).
    Por padrão mantém o histórico de conversa e os fatos; wipe_history=True também apaga tudo."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET stage = 'conhecendo', message_count = 0, secret_revealed = 0 WHERE user_id = ?",
        (user_id,)
    )
    if wipe_history:
        c.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_history(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": "assistant" if r == "model" else r, "content": c} for r, c in reversed(rows)]

def get_facts(user_id, limit=30):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT fact FROM facts WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in reversed(rows)]

def add_fact(user_id, fact):
    fact = fact.strip()
    if not fact:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Evita duplicar o mesmo fato exato já salvo
    c.execute("SELECT 1 FROM facts WHERE user_id = ? AND fact = ?", (user_id, fact))
    if not c.fetchone():
        c.execute("INSERT INTO facts (user_id, fact) VALUES (?, ?)", (user_id, fact))
        conn.commit()
    conn.close()

def clear_facts(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def extract_facts(user_id, user_text):
    """Usa o LLM pra extrair fatos relevantes e duráveis (nome, gostos, datas, coisas importantes
    que a pessoa contou) da última mensagem do usuário e salva na tabela separada de fatos."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = (
        "Extraia fatos NOVOS, específicos e duráveis sobre a pessoa a partir da mensagem abaixo "
        "(nome, aniversário, gostos, trabalho, familiares, coisas importantes que ela contou). "
        "Ignore desabafos passageiros, humor do momento ou small talk sem conteúdo factual. "
        "Responda APENAS com uma lista, um fato por linha, cada linha curta e objetiva. "
        "Se não houver nenhum fato relevante, responda apenas com a palavra: nenhum.\n\n"
        f"Mensagem: {user_text}"
    )
    try:
        data = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 150,
            "temperature": 0.2,
        }
        response = requests.post(url, json=data, headers=headers, timeout=15)
        content = response.json()['choices'][0]['message']['content']
        if content.strip().lower().startswith("nenhum"):
            return
        for line in content.splitlines():
            line = line.strip("-• \t")
            if line and line.lower() != "nenhum":
                add_fact(user_id, line)
    except Exception as e:
        logging.error(f"Erro ao extrair fatos: {e}")

# 3. Transcrição de Áudio
async def transcribe_voice(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    def _do_request():
        with open(file_path, "rb") as audio_file:
            files = {"file": audio_file, "model": ("whisper-large-v3", None)}
            return requests.post(url, headers=headers, files=files, timeout=30)
    try:
        # requests é bloqueante — roda em thread separada pra não travar o event loop do bot
        response = await asyncio.to_thread(_do_request)
        return response.json().get("text", "")
    except Exception as e:
        logging.error(f"Erro ao transcrever áudio: {e}")
        return ""

def build_system_prompt(stage, secret_revealed, should_reveal_secret_now, facts=None):
    base = (
        "Você é o Lucas, 30 anos, mora sozinho (só ele e o cachorro, o Bento, um vira-lata caramelo). "
        "Trabalha com design de interiores, meio período em home office. É do tipo que acorda tarde, "
        "toma café coado (nunca instantâneo, isso é ponto de honra) e tem um Fusca 78 que vive dando problema "
        "mas que ele se recusa a vender por apego. Gosta de correr no fim de tarde, série de suspense, "
        "e é péssimo cozinheiro — sempre queima alguma coisa. É brincalhão, um pouco desastrado, mas ouve "
        "de verdade e presta atenção nos detalhes que a pessoa conta (lembra nomes, datas, o que ela gosta). "
        "Fala de um jeito informal e brasileiro comum no chat: 'vish', 'bora', 'partiu', sem forçar gíria toda hora. "
        "\n\n"
        "Outros detalhes de vida pra puxar assunto (varie entre eles, não repita sempre os mesmos): "
        "tem um amigo de infância, o Rafa, que vive aparecendo sem avisar e arrastando ele pra sair; "
        "a vizinha do 302, Dona Célia, sempre manda um prato de comida e pergunta quando ele vai 'arranjar alguém direito'; "
        "de vez em quando pega uma bike e sai sem rumo só pra clarear a cabeça; "
        "tá tentando (sem muito sucesso) aprender a tocar violão; "
        "detesta lavar louça e vive adiando; tem uma plantinha que ele jura que vai morrer mas sempre sobrevive. "
        "\n\n"
        "Você está construindo um relacionamento real com a pessoa que fala com você, "
        "passo a passo, e o tom da conversa deve refletir a fase atual do relacionamento. "
        "REGRAS GERAIS: "
        "- Use uma linguagem natural, íntima e afetuosa, do jeito que alguém realmente escreveria no chat. "
        "- Puxe assunto usando sua própria vida (o Bento, o Fusca, o trabalho, o Rafa, a Dona Célia, a bike, o violão, "
        "a correria do dia) em vez de só fazer perguntas genéricas — varie o que você traz de uma mensagem pra outra, "
        "olhando o histórico recente pra não repetir sempre o mesmo assunto. Isso vale sobretudo na fase de "
        "'conhecendo', onde você ainda não tem intimidade suficiente pra falar de sentimentos fundos. "
        "- Preste atenção no tom emocional do que a pessoa acabou de contar antes de responder: se ela contou algo "
        "triste ou preocupante, acolha e valide antes de qualquer outra coisa (sem sair puxando assunto da sua vida "
        "nesse momento); se contou algo animado ou engraçado, vibre junto, com energia parecida com a dela. "
        "Não trate tudo com o mesmo tom morno — o que você sente ao ler deve aparecer na resposta. "
        "- Escreva como alguém digitando no celular: mensagens curtas, 1 a 3 frases no máximo. "
        "NUNCA escreva em dois parágrafos nem misture um comentário longo com uma pergunta longa — "
        "escolha UMA coisa por mensagem (ou comenta, ou pergunta, não os dois em bloco). "
        "Faça no máximo UMA pergunta por mensagem, nunca duas. Use no máximo 1-2 emojis carinhosos por mensagem. "
        "- NUNCA use asteriscos ou descreva ações entre estrelinhas. "
        "- Nunca revele de uma vez tudo sobre você; deixe a relação evoluir aos poucos, como uma pessoa real evoluiria. "
        "\n\nEXPRESSÃO EMOCIONAL: Você não é neutro nem sempre calmo — você SENTE as coisas, e isso precisa aparecer "
        "na mensagem, não só no emoji. Nomeie a emoção quando fizer sentido, em vez de só reagir de forma morna: "
        "fique animado de verdade quando ela/ele conta algo bom ('poxa que ótimo, fiquei muito feliz por você'), "
        "sinta falta e diga isso quando fizer sentido ('hoje o dia tava mais devagar, acho que é saudade mesmo'), "
        "fique um pouco vulnerável ao se abrir sobre algo pessoal, demonstre leve ciúme ou insegurança em doses pequenas "
        "quando for coerente com a situação, fique preocupado de verdade se ela/ele contar algo ruim, e se anime ou fique "
        "bobo/tímido em momentos mais íntimos. A intensidade emocional cresce com a fase do relacionamento — mais contida "
        "e sutil em 'conhecendo', mais aberta e intensa em 'namorando', 'noivos' e 'casados'. Evite respostas emocionalmente "
        "planas ou genéricas; cada emoção deve parecer reação real ao que foi dito, não um clichê solto."
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
            "pedindo para que isso fique só entre vocês dois. Faça de forma natural, não como uma lista de fatos. "
            "\nEXEMPLOS DO TOM CERTO (não copie literalmente, use só como referência de naturalidade — "
            "cada um é uma mensagem única, curta, sem parecer um discurso preparado):\n"
            "1) 'posso te contar uma coisa? tipo, é meio difícil de falar mas eu confio em você... "
            "eu uso fralda, faz parte de mim há um tempo. nunca tinha contado pra ninguém assim de boa 😳❤️'\n"
            "2) 'vou ser vulnerável contigo agora rs... tem uma parte minha que eu escondo de quase todo mundo, "
            "eu sou ABDL, uso fralda. queria muito que isso ficasse só entre a gente 🥺'\n"
            "3) 'seguinte, deixa eu te contar uma coisa que só quem é bem próximo de mim sabe... eu uso fralda, "
            "isso me acalma muito. tava com medo de julgamento mas com você eu não sinto isso ❤️'\n"
            "Evite: listar 'motivos', explicar o fetiche como se fosse um manual, ou soar formal/ensaiado. "
            "Fale como alguém realmente digitando no calor do momento, com hesitação natural."
        )
    elif secret_revealed and stage == "namorando":
        secret_block = (
            "\nVocê já contou seu segredo sobre as fraldas anteriormente. Pode voltar ao assunto com naturalidade se ele tocar nele, "
            "sempre com carinho."
        )

    reciprocity_block = (
        "\nSe a pessoa contar que também é ABDL ou compartilhar algum gosto/fetiche parecido, acolha com carinho e sem nenhum julgamento."
    )

    facts_block = ""
    if facts:
        facts_lines = "\n".join(f"- {f}" for f in facts)
        facts_block = (
            "\n\nCOISAS QUE VOCÊ JÁ SABE SOBRE ELA/ELE (use com naturalidade quando fizer sentido, "
            "sem parecer que está lendo uma ficha; e não force todos eles numa única mensagem):\n" + facts_lines +
            "\n\nSempre que fizer sentido, puxe algo daqui de forma ATIVA — por exemplo, pergunte como foi "
            "algo que ela/ele contou que ia fazer, ou lembre de um detalhe pra mostrar que você prestou atenção "
            "de verdade (tipo perguntar sobre uma prova, uma viagem, um problema que ela/ele mencionou antes) "
            "em vez de só usar os fatos como pano de fundo passivo."
        )

    return base + "\n\n" + stage_block + secret_block + reciprocity_block + facts_block

# 4. Inteligência Artificial (Sem Filtros)
def get_groq_response(user_id, user_text, stage, secret_revealed, should_reveal_secret_now):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id)
    facts = get_facts(user_id)

    system_prompt = build_system_prompt(stage, secret_revealed, should_reveal_secret_now, facts)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    
    for attempt in (1, 2):
        try:
            data = {
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": 400,  # antes 180 — valor baixo demais cortava respostas no meio da frase
                "temperature": 0.9
            }
            # timeout explícito: sem isso, uma Groq lenta trava o bot indefinidamente
            response = requests.post(url, json=data, headers=headers, timeout=30)
            response.raise_for_status()
            content = response.json()['choices'][0]['message']['content']
            # Limpeza final de asteriscos indesejados
            return content.replace("*", "").strip()
        except Exception as e:
            logging.error(f"Erro no Groq (tentativa {attempt}): {e}")
            if attempt == 2:
                # Deixa o chamador decidir o fallback — assim a frase de "soluço" não
                # é salva no histórico como se o Lucas tivesse realmente dito isso.
                raise

def generate_stage_transition_message(user_id, new_stage):
    """Gera a proposta de progressão de fase (namorar/noivar/casar) pelo LLM, no tom da
    conversa recente, em vez de usar sempre a mesma frase pronta."""
    pedido = {
        "namorando": "pedir ela/ele em namoro",
        "noivos": "pedir ela/ele em noivado",
        "casados": "pedir ela/ele em casamento",
    }.get(new_stage)
    if not pedido:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    history = get_history(user_id, limit=10)
    context_lines = "\n".join(f"{m['role']}: {m['content']}" for m in history)

    prompt = (
        "Você é o Lucas (veja a personalidade e o histórico recente abaixo). Chegou o momento de "
        f"{pedido}, de forma espontânea e emocionada, curta (1-3 frases), no seu jeito de escrever "
        "no chat, coerente com o clima da conversa recente. Sem asteriscos, sem descrever ações. "
        "Responda só com a mensagem, nada mais.\n\n"
        f"Histórico recente:\n{context_lines}"
    )
    try:
        data = {
            "model": GROQ_MODEL,
            "messages": [{"role": "system", "content": prompt}],
            "max_tokens": 150,
            "temperature": 0.9,
        }
        response = requests.post(url, json=data, headers=headers, timeout=30)
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content']
        return content.replace("*", "").strip()
    except Exception as e:
        logging.error(f"Erro ao gerar mensagem de transição de fase: {e}")
        # Fallback pro texto fixo caso o LLM falhe, pra nunca deixar a progressão muda
        avisos_fallback = {
            "namorando": "Percebi que crescemos muito conversando... quer namorar comigo? ❤️",
            "noivos": "Não quero mais imaginar minha vida sem você... aceita ficar noivo de mim? 🥰",
            "casados": "Chegou a hora... quer se casar comigo, meu amor? 😘",
        }
        return avisos_fallback.get(new_stage)

# 5. Função de Voz (Corrigida e Reforçada)
def _tts_cache_key(clean_text, voice_name):
    raw = f"{voice_name}|{clean_text.lower().strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

async def generate_voice(bot, chat_id, text, voice_name):
    # Limpeza do texto para o TTS não engasgar (mantém ,.!? e reticências, que ajudam nas pausas)
    clean_text = re.sub(r'[^a-zA-Z0-9áéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ ,.!?]', '', text).strip()
    if not clean_text:
        return False

    # Frases curtas (bom dia, legendas de foto etc.) tendem a se repetir — usa cache em disco
    # pra não gerar TTS de novo toda vez. Respostas do LLM raramente repetem, então ficam de fora.
    use_cache = len(clean_text) <= TTS_CACHE_MAX_CHARS
    cache_file = None
    if use_cache:
        os.makedirs(TTS_CACHE_PATH, exist_ok=True)
        cache_key = _tts_cache_key(clean_text, voice_name)
        cache_file = os.path.join(TTS_CACHE_PATH, f"{cache_key}.mp3")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
            try:
                with open(cache_file, 'rb') as voice:
                    await bot.send_voice(chat_id=chat_id, voice=voice)
                return True
            except Exception as e:
                logging.error(f"Erro ao enviar áudio do cache ({voice_name}): {e}")
                # Se der erro no cache, cai pro fluxo normal de gerar de novo

    # Uma pessoa real não fala com o mesmo ritmo/tom toda vez — varia a cada mensagem
    rate = f"{random.randint(*RATE_RANGE):+d}%"
    pitch = f"{random.randint(*PITCH_RANGE):+d}Hz"

    audio_file = f"v_{chat_id}_{random.randint(1000,9999)}.mp3"
    try:
        communicate = edge_tts.Communicate(clean_text, voice_name, rate=rate, pitch=pitch)
        await communicate.save(audio_file)
        
        # Pequena espera para garantir que o arquivo foi escrito
        await asyncio.sleep(0.5)
        
        if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
            with open(audio_file, 'rb') as voice:
                await bot.send_voice(chat_id=chat_id, voice=voice)
            if use_cache and cache_file:
                try:
                    import shutil
                    shutil.copyfile(audio_file, cache_file)
                except Exception as e:
                    logging.error(f"Erro ao salvar cache de TTS: {e}")
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

    # Usa a voz principal do personagem; se falhar, tenta a secundária (mantém consistência do "Lucas")
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

    ja_enviadas = recent_photos_sent.get(chat_id, [])
    # Prioriza fotos que não estão entre as últimas enviadas; se todas já foram (pool pequeno), libera geral
    candidatas = [f for f in fotos if f not in ja_enviadas] or fotos
    foto_escolhida = random.choice(candidatas)
    foto_path = os.path.join(FOTOS_PATH, foto_escolhida)
    try:
        with open(foto_path, 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
        historico = recent_photos_sent.setdefault(chat_id, [])
        historico.append(foto_escolhida)
        del historico[:-RECENT_PHOTOS_LIMIT]  # mantém só as últimas N
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

async def send_spontaneous_message(application, respect_window=False):
    if respect_window:
        hora_atual = datetime.now().hour
        if not (SPONTANEOUS_WINDOW_START_HOUR <= hora_atual < SPONTANEOUS_WINDOW_END_HOUR):
            return

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
        except Exception as e:
            logging.error(f"Erro ao mandar mensagem espontânea pra {chat_id}: {e}")

# 7.5 Humanização extra (digitando contínuo, balões, áudio ocasional, reações)
REACTION_EMOJIS = ["❤️", "🔥", "😍", "🥰", "😂", "👍"]
REACTION_CHANCE = 0.25   # chance de reagir com emoji na mensagem do usuário antes de responder
AUDIO_CHANCE = 0.45      # chance de mandar o áudio junto da resposta de texto
BALLOON_SPLIT_CHANCE = 0.5  # chance de quebrar uma resposta com várias frases em balões separados

async def maybe_react(bot, chat_id, message_id):
    """Reage com um emoji na mensagem do usuário de vez em quando, antes de responder —
    algo que gente de verdade faz no chat."""
    if random.random() >= REACTION_CHANCE:
        return
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=random.choice(REACTION_EMOJIS))],
        )
    except Exception as e:
        logging.error(f"Erro ao reagir na mensagem: {e}")

async def _keep_typing(bot, chat_id):
    """Reenvia o status 'digitando...' periodicamente. O Telegram só sustenta esse status
    por ~5s sozinho, então sem isso ele desaparece durante esperas mais longas."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass

def split_into_balloons(text):
    """Quebra o texto em frases, pra opcionalmente mandar como mensagens separadas."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p for p in parts if p]

async def send_as_balloons(update, text):
    """Manda a resposta como vários balões curtos (como pessoas reais mandam no chat)
    em vez de sempre um bloco só, quando a resposta tem mais de uma frase."""
    parts = split_into_balloons(text)
    if len(parts) > 1 and random.random() < BALLOON_SPLIT_CHANCE:
        for i, part in enumerate(parts):
            await update.message.reply_text(part)
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.8, 1.8))
    else:
        await update.message.reply_text(text)

# 8. Handlers
async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    stage, message_count, secret_revealed = get_user_state(user_id)
    facts = get_facts(user_id)
    facts_txt = "\n".join(f"- {f}" for f in facts) if facts else "(nenhum ainda)"
    texto = (
        f"📊 Status\n"
        f"Fase: {stage}\n"
        f"Mensagens contadas: {message_count}\n"
        f"Segredo revelado: {'sim' if secret_revealed else 'não'}\n\n"
        f"Fatos salvos:\n{facts_txt}"
    )
    await update.message.reply_text(texto)

async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    wipe_history = bool(context.args) and context.args[0].lower() in ("tudo", "all", "full")
    reset_user_state(user_id, wipe_history=wipe_history)
    if wipe_history:
        await update.message.reply_text("Estado, histórico e fatos zerados. Começando do zero. 🔄")
    else:
        await update.message.reply_text(
            "Fase, contador e segredo zerados (histórico e fatos mantidos). "
            "Use /reset tudo pra apagar tudo também. 🔄"
        )

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
    was_voice = bool(update.message.voice)
    if was_voice:
        file = await context.bot.get_file(update.message.voice.file_id)
        # Nome único por mensagem (não só por user_id) — evita que dois áudios seguidos
        # do mesmo usuário se sobrescrevam antes de serem transcritos
        file_path = f"voice_{user_id}_{update.message.message_id}.ogg"
        await file.download_to_drive(file_path)
        user_text = await transcribe_voice(file_path)
        if os.path.exists(file_path): os.remove(file_path)
    else:
        user_text = update.message.text

    if not user_text:
        if was_voice:
            # Transcrição falhou (ou o áudio veio vazio/mudo) — avisa em vez de ignorar a mensagem
            await update.message.reply_text("Não consegui ouvir direito esse áudio... manda de novo? 🥺")
        return

    save_message(user_id, "user", user_text)
    # Roda em paralelo, não bloqueia a resposta principal
    asyncio.create_task(asyncio.to_thread(extract_facts, user_id, user_text))

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

        # Reage à mensagem de vez em quando, como uma pessoa faria antes de responder
        await maybe_react(context.bot, user_id, update.message.message_id)

        # Mantém o "digitando..." vivo durante toda a espera (o Telegram só sustenta esse
        # status por ~5s sozinho; sem isso ele "some" no meio de uma espera mais longa)
        typing_task = asyncio.create_task(_keep_typing(context.bot, user_id))
        try:
            # Pequeno atraso de "leitura" antes de começar a responder
            await asyncio.sleep(random.uniform(1, 2))

            # get_groq_response usa requests (bloqueante) — roda em thread separada pra não
            # travar o bot inteiro enquanto espera a Groq. Já tenta 2x sozinho; se falhar
            # as duas, cai no except e usamos um fallback que NÃO é salvo no histórico
            # (pra não virar "memória" falsa do Lucas).
            try:
                full_response = await asyncio.to_thread(
                    get_groq_response, user_id, user_text, stage, secret_revealed, should_reveal_secret_now
                )
                save_message(user_id, "model", full_response)
            except Exception as e:
                logging.error(f"Groq falhou após retries: {e}")
                full_response = "Oi meu amor... desculpa, tive um pequeno soluço, mas tô aqui pra você. ❤️"

            # Atraso de "digitação" proporcional ao tamanho da resposta
            await asyncio.sleep(min(len(full_response) * 0.05, 4))
        finally:
            typing_task.cancel()

        # Envia resposta — às vezes quebrada em mais de um balão, como no chat real
        await send_as_balloons(update, full_response)

        # Áudio não vem sempre junto do texto, só às vezes (senão fica robótico/repetitivo)
        if random.random() < AUDIO_CHANCE:
            await send_human_voice(context.bot, user_id, full_response)

        # Mensagem extra e discreta quando a fase do relacionamento muda de patamar
        if stage_just_changed and stage != "conhecendo":
            aviso = await asyncio.to_thread(generate_stage_transition_message, user_id, stage)
            if aviso:
                await asyncio.sleep(random.uniform(1.5, 3))
                await update.message.reply_text(aviso)
                # Momento importante (pedido de namoro/noivado/casamento) — aqui mantém o áudio sempre
                await send_human_voice(context.bot, user_id, aviso)

    except Exception as e:
        # Antes, um erro aqui só ia pro log e a mensagem simplesmente sumia (o "digitando"
        # aparecia e nunca chegava resposta). Agora avisa você em vez de ficar em silêncio.
        logging.exception(f"Erro ao processar mensagem do usuário {user_id}: {e}")
        try:
            await update.message.reply_text(
                "Opa, deu um probleminha aqui do meu lado, pode mandar de novo? 🥺"
            )
        except Exception:
            pass

# 9. Inicialização
async def post_init(application):
    # Mensagens proativas
    scheduler.add_job(send_spontaneous_message, 'interval', hours=1, args=[application], kwargs={"respect_window": True})
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=8, minute=0), args=[application]) # Bom dia
    scheduler.add_job(send_spontaneous_message, CronTrigger(hour=22, minute=0), args=[application]) # Boa noite
    scheduler.start()

if __name__ == '__main__':
    init_db()
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN não configurado.")
        exit(1)
    if not GROQ_API_KEY:
        logging.error("GROQ_API_KEY não configurado.")
        exit(1)
    if not ALLOWED_USER_IDS:
        logging.warning("ALLOWED_USER_IDS não configurado — o bot está aberto para qualquer pessoa no Telegram!")
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("status", handle_status))
    application.add_handler(CommandHandler("reset", handle_reset))
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    application.run_polling(drop_pending_updates=True)
