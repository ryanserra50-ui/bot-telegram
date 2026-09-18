import logging
import json
import os
import uuid
import asyncio
import aiohttp
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────────
#  CONFIGURAÇÕES  –  ÚNICA COISA QUE VOCÊ EDITA
# ─────────────────────────────────────────────
BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN não configurado no Back4app.")
SUPER_ID     = 7194320806
LINK_SUPORTE = "https://t.me/geovannapriv"
# ─────────────────────────────────────────────

DB_FILE = "banco.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ══════════════════════════════════════════════
#  PREÇOS - aceita 14,90 / 14.90 / R$ 14,90
# ══════════════════════════════════════════════
def preco_para_float(valor):
    s = str(valor).strip()
    s = s.replace("R$", "").replace(" ", "").replace("−", "-")
    # tolera valores antigos salvos como "- R$ 14.90"
    while s.startswith("-"):
        s = s[1:]

    if "," in s and "." in s:
        # Se a vírgula vier por último, assume formato brasileiro: 1.234,56
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            # Formato 1,234.56
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")

    return float(s)


def formatar_preco(valor):
    return f"{preco_para_float(valor):.2f}".replace(".", ",")


def normalizar_duracao(valor):
    s = str(valor).strip().lower()
    s_sem_acento = s.replace("í", "i").replace("ó", "o")
    if s_sem_acento in ("vitalicio", "vital", "permanente"):
        return None

    dias = int(s)
    if dias <= 0:
        raise ValueError("A duração deve ser maior que zero ou 'vitalicio'.")
    return dias


def texto_duracao(dias):
    if dias is None:
        return "Vitalício"
    return f"{int(dias)} dias"


def grupo_do_plano(plano, db):
    """Compatibilidade: planos antigos ainda podem usar o grupo global."""
    link = plano.get("link_grupo") or db.get("grupo", {}).get("link", "")
    grupo_id = plano.get("grupo_id")
    if grupo_id is None:
        grupo_id = db.get("grupo", {}).get("grupo_id")
    return link, grupo_id


# ══════════════════════════════════════════════
#  BANCO DE DADOS
# ══════════════════════════════════════════════
def carregar_db():
    if not os.path.exists(DB_FILE):
        return {
            "apresentacao": {
                "tipo": "texto",
                "texto": "👋 Olá! Bem-vindo!\n\nConfigure sua mensagem no painel /super.",
                "file_id": None,
            },
            "planos": [],
            "gateway": {
                "client_id": "",
                "client_secret": "",
            },
            "grupo": {
                "link": "",
                "dias": 30,
                "grupo_id": None,
            },
            "pagamentos_pendentes": {},
            "usuarios": {},
        }
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def salvar_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════
#  MISTICPAY  –  Gerar cobrança PIX
# ══════════════════════════════════════════════
async def criar_cobranca_pix(client_id, client_secret, amount, payer_name, descricao):
    url = "https://api.misticpay.com/api/transactions/create"
    headers = {
        "ci": client_id,
        "cs": client_secret,
        "Content-Type": "application/json",
    }
    payload = {
        "amount": float(amount),
        "payerName": payer_name,
        "payerDocument": "00000000000",
        "transactionId": str(uuid.uuid4()).replace("-", "")[:20],
        "description": descricao,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            resultado = await resp.json()
            logging.info(f"MisticPay resposta: {resultado}")
            return resultado

# ══════════════════════════════════════════════
#  MISTICPAY  –  Verificar pagamento
# ══════════════════════════════════════════════
async def verificar_pagamento(client_id, client_secret, transaction_id):
    url = f"https://api.misticpay.com/api/transactions/{transaction_id}"
    headers = {"ci": client_id, "cs": client_secret}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            return await resp.json()

# ══════════════════════════════════════════════
#  /testar  –  Testa as credenciais da MisticPay
# ══════════════════════════════════════════════
async def testar_gateway(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or user.id != SUPER_ID:
        return
    db = carregar_db()
    gw = db.get("gateway", {})
    ci = gw.get("client_id", "")
    cs = gw.get("client_secret", "")

    if not ci or not cs:
        await update.message.reply_text("❌ Gateway não configurado. Use /super → Gateway MisticPay.")
        return

    await update.message.reply_text("🔍 Testando conexão com MisticPay...")

    try:
        url = "https://api.misticpay.com/api/users/balance"
        headers = {"ci": ci, "cs": cs}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                resultado = await resp.json()
                status_code = resp.status

        if status_code == 200:
            saldo = resultado.get("data", {}).get("balance", "?")
            await update.message.reply_text(
                f"✅ *Conexão OK!*\n\n"
                f"Saldo disponível: R$ {saldo}\n\n"
                f"As credenciais estão corretas! 🎉",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"❌ *Erro na conexão!*\n\n"
                f"Status: `{status_code}`\n"
                f"Resposta: `{resultado}`\n\n"
                f"Verifique suas credenciais no /super → Gateway MisticPay.",
                parse_mode="Markdown",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: `{str(e)}`", parse_mode="Markdown")

# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = carregar_db()
    ap = db["apresentacao"]
    planos = db.get("planos", [])

    # Monta botões de compra se tiver planos
    teclado = []
    for i, plano in enumerate(planos):
        teclado.append([InlineKeyboardButton(
            f"{plano['nome']} - R$ {formatar_preco(plano['preco'])}",
            callback_data=f"comprar_{i}"
        )])

    markup = InlineKeyboardMarkup(teclado) if teclado else None

    if ap["tipo"] == "texto":
        await update.message.reply_text(ap["texto"], reply_markup=markup)
    elif ap["tipo"] == "foto":
        await update.message.reply_photo(photo=ap["file_id"], caption=ap.get("texto", ""), reply_markup=markup)
    elif ap["tipo"] == "video":
        await update.message.reply_video(video=ap["file_id"], caption=ap.get("texto", ""), reply_markup=markup)

# ══════════════════════════════════════════════
#  /status
# ══════════════════════════════════════════════
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    db = carregar_db()
    uid = str(user.id)
    usuario = db.get("usuarios", {}).get(uid)

    if not usuario:
        await update.message.reply_text(
            "📊 *Minhas Assinaturas*\n\n❌ Você não possui nenhuma assinatura ativa.\n\nUse /start para ver nossos planos!",
            parse_mode="Markdown",
        )
        return

    assinaturas = usuario.get("assinaturas", {})

    if not assinaturas and usuario.get("plano"):
        expira = usuario.get("expira")
        plano = usuario.get("plano", "")
        if expira:
            try:
                expira_dt = datetime.fromisoformat(expira)
                restante = max(0, (expira_dt - datetime.now()).days)
                status_txt = f"✅ Ativo — {restante} dias restantes"
                data_txt = expira[:10]
            except Exception:
                status_txt = "⚠️ Indefinido"
                data_txt = "indefinida"
        else:
            status_txt = "✅ Ativo — Vitalício"
            data_txt = "Vitalício"

        await update.message.reply_text(
            f"📊 *Minha Assinatura*\n\n"
            f"📦 Plano: *{plano}*\n"
            f"📅 Expira em: *{data_txt}*\n"
            f"🔋 Status: {status_txt}",
            parse_mode="Markdown",
        )
        return

    if not assinaturas:
        await update.message.reply_text(
            "📊 *Minhas Assinaturas*\n\n❌ Você não possui nenhuma assinatura ativa.",
            parse_mode="Markdown",
        )
        return

    blocos = []
    agora = datetime.now()

    for assinatura in assinaturas.values():
        plano = assinatura.get("plano", "Plano")
        expira = assinatura.get("expira")

        if expira is None:
            status_txt = "✅ Ativo — Vitalício"
            expira_txt = "Vitalício"
        else:
            try:
                expira_dt = datetime.fromisoformat(expira)
                restante = (expira_dt - agora).days
                if restante < 0:
                    status_txt = "❌ Expirado"
                else:
                    status_txt = f"✅ Ativo — {restante} dias restantes"
                expira_txt = expira[:10]
            except Exception:
                status_txt = "⚠️ Indefinido"
                expira_txt = "indefinida"

        blocos.append(
            f"📦 Plano: *{plano}*\n"
            f"📅 Expira em: *{expira_txt}*\n"
            f"🔋 Status: {status_txt}"
        )

    await update.message.reply_text(
        "📊 *Minhas Assinaturas*\n\n" + "\n\n────────────\n\n".join(blocos),
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════
#  /suporte
# ══════════════════════════════════════════════
async def suporte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📞 *Suporte*\n\nPara entrar em contato com nosso suporte, clique no link abaixo:\n\n"
        f"👉 {LINK_SUPORTE}\n\nEstamos disponíveis para ajudar você!",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════
#  /super  –  painel admin
# ══════════════════════════════════════════════
async def super_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    if user.id != SUPER_ID:
        await msg.reply_text("⛔ Sem permissão.")
        return
    await mostrar_super(update.message, ctx)

async def mostrar_super(msg, ctx):
    teclado = [
        [InlineKeyboardButton("✏️ Texto de Apresentação", callback_data="cfg_apresentacao")],
        [InlineKeyboardButton("🛒 Botões de Compra / Planos", callback_data="cfg_planos")],
        [InlineKeyboardButton("💳 Gateway MisticPay", callback_data="cfg_gateway")],
    ]
    await msg.reply_text(
        "🛠️ *Menu Super Admin*\n\nO que deseja configurar?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(teclado),
    )

# ══════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data

    # ── Compra de plano (usuário) ──────────────
    if data.startswith("comprar_"):
        idx = int(data.split("_")[1])
        db = carregar_db()
        planos = db.get("planos", [])
        if idx >= len(planos):
            await query.message.reply_text("❌ Plano não encontrado.")
            return
        plano = planos[idx]
        gw = db.get("gateway", {})
        if not gw.get("client_id") or not gw.get("client_secret"):
            await query.message.reply_text("⚠️ Pagamento ainda não configurado pelo admin.")
            return

        # Cancela qualquer PIX pendente anterior desse usuário
        uid = str(query.from_user.id)
        pendentes = db.get("pagamentos_pendentes", {})
        for tx_id_antigo in list(pendentes.keys()):
            if pendentes[tx_id_antigo].get("user_id") == uid:
                del db["pagamentos_pendentes"][tx_id_antigo]
        salvar_db(db)

        # Manda mensagem nova de "aguarde"
        aguarde_msg = await query.message.reply_text("⏳ Gerando seu PIX, aguarde...")

        try:
            user = query.from_user
            resp = await criar_cobranca_pix(
                gw["client_id"], gw["client_secret"],
                preco_para_float(plano["preco"]), user.first_name,
                f"Plano: {plano['nome']}"
            )
            tx_data = resp.get("data", {})
            tx_id = tx_data.get("transactionId", "")
            copy_paste = tx_data.get("copyPaste", "")
            qr_url = tx_data.get("qrcodeUrl", "")

            logging.info(f"=== MISTICPAY RESPOSTA COMPLETA ===")
            logging.info(f"tx_id: {tx_id}")
            logging.info(f"copy_paste: {copy_paste[:50] if copy_paste else 'VAZIO'}")
            logging.info(f"qr_url: {qr_url}")
            logging.info(f"resp completo: {resp}")
            logging.info(f"=====================================")

            # Cada plano tem seu próprio grupo e duração
            link_grupo, grupo_id = grupo_do_plano(plano, db)
            dias = plano.get("dias")
            if "dias" not in plano:
                dias = db.get("grupo", {}).get("dias", 30)

            if not link_grupo or not grupo_id:
                raise RuntimeError(
                    "Este plano ainda não tem link/ID de grupo configurado. "
                    "Remova o plano e cadastre novamente pelo /super."
                )

            # Salva pagamento pendente junto com o grupo correto
            db["pagamentos_pendentes"][tx_id] = {
                "user_id": str(user.id),
                "plano_idx": idx,
                "plano_nome": plano["nome"],
                "plano_dias": dias,
                "plano_link": link_grupo,
                "plano_grupo_id": int(grupo_id),
                "criado_em": datetime.now().isoformat(),
            }
            salvar_db(db)

            texto = (
                f"💳 *Pagamento PIX*\n\n"
                f"📦 Plano: *{plano['nome']} - R$ {formatar_preco(plano['preco'])}*\n"
                f"💰 Valor: *R$ {formatar_preco(plano['preco'])}*\n"
                f"⏱️ Acesso: *{texto_duracao(dias)}*\n\n"
                f"─────────────────\n"
                f"📋 *Copia e Cola PIX:*\n`{copy_paste}`\n\n"
                f"Após pagar, clique em ✅ Já Paguei para verificar!"
            )
            teclado = [
                [InlineKeyboardButton("✅ Já Paguei!", callback_data=f"verificar_{tx_id}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
            ]

            await aguarde_msg.delete()

            # Baixa o QR code e envia como bytes (evita cache do Telegram)
            if qr_url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(qr_url) as r:
                        qr_bytes = await r.read()
                from io import BytesIO
                await query.message.reply_photo(
                    photo=BytesIO(qr_bytes),
                    caption=texto,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(teclado),
                )
            else:
                await query.message.reply_text(
                    texto,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(teclado),
                )

        except Exception as e:
            await aguarde_msg.edit_text(f"❌ Erro ao gerar PIX: {str(e)}")
        return

    # ── Verificar pagamento ────────────────────
    if data.startswith("verificar_"):
        tx_id = data.split("_", 1)[1]
        db = carregar_db()
        gw = db.get("gateway", {})
        pendente = db.get("pagamentos_pendentes", {}).get(tx_id)

        if not pendente:
            await query.message.reply_text(
                "❌ Este PIX expirou ou foi cancelado.\n\nUse /start para gerar um novo! 😊"
            )
            return

        try:
            resp = await verificar_pagamento(gw["client_id"], gw["client_secret"], tx_id)
            estado = resp.get("data", {}).get("transactionState", "PENDENTE")

            if estado == "APROVADO":
                uid = pendente["user_id"]
                dias = pendente.get("plano_dias")
                link_grupo = pendente.get("plano_link", "")
                grupo_id = pendente.get("plano_grupo_id")

                if not link_grupo or not grupo_id:
                    await query.message.reply_text(
                        "⚠️ O pagamento foi confirmado, mas este plano está sem grupo configurado. "
                        "Entre em contato com o suporte."
                    )
                    return

                expira = None
                if dias is not None:
                    expira = (datetime.now() + timedelta(days=int(dias))).isoformat()

                if "usuarios" not in db:
                    db["usuarios"] = {}

                usuario = db["usuarios"].setdefault(uid, {})
                assinaturas = usuario.setdefault("assinaturas", {})

                assinaturas[str(grupo_id)] = {
                    "plano": pendente["plano_nome"],
                    "expira": expira,
                    "comprado_em": datetime.now().isoformat(),
                    "grupo_id": int(grupo_id),
                    "link_grupo": link_grupo,
                    "aviso_enviado": False,
                }

                del db["pagamentos_pendentes"][tx_id]
                salvar_db(db)

                tempo_txt = texto_duracao(dias)
                await query.message.reply_text(
                    f"✅ *Pagamento Confirmado!*\n\n"
                    f"📦 Plano: *{pendente['plano_nome']}*\n"
                    f"⏱️ Acesso: *{tempo_txt}*\n\n"
                    f"👇 Entre no grupo correto do seu plano:\n{link_grupo}",
                    parse_mode="Markdown",
                )
            else:
                teclado = [
                    [InlineKeyboardButton("🔄 Verificar novamente", callback_data=f"verificar_{tx_id}")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
                ]
                await query.message.reply_text(
                    "⏳ *Pagamento ainda não identificado.*\n\nAguarde alguns segundos e tente novamente.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(teclado),
                )
        except Exception as e:
            await query.message.reply_text(f"❌ Erro ao verificar pagamento: {str(e)}")
        return

    if data == "cancelar":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text("❌ Pedido cancelado.")
        return

    # ── Admin: proteção ────────────────────────
    if query.from_user.id != SUPER_ID:
        await query.message.reply_text("⛔ Sem permissão.")
        return

    db = carregar_db()

    # ── Texto de Apresentação ──────────────────
    if data == "cfg_apresentacao":
        ap = db["apresentacao"]
        await query.edit_message_text(
            f"✏️ *Texto de Apresentação*\n\n"
            f"Tipo atual: `{ap['tipo'].upper()}`\n\n"
            f"Envie agora:\n"
            f"• Só texto → digita e manda\n"
            f"• Imagem → manda a foto (pode colocar legenda)\n"
            f"• Vídeo → manda o vídeo (pode colocar legenda)",
            parse_mode="Markdown",
        )
        ctx.user_data["aguardando"] = "apresentacao"

    # ── Planos ─────────────────────────────────
    elif data == "cfg_planos":
        planos = db.get("planos", [])
        linhas = []
        for i, p in enumerate(planos):
            link, gid = grupo_do_plano(p, db)
            duracao = texto_duracao(p.get("dias", 30))
            grupo_txt = link or "sem grupo"
            linhas.append(
                f"{i+1}. {p['nome']} - R$ {formatar_preco(p['preco'])} - {duracao}\n"
                f"   👥 {grupo_txt}"
            )

        lista = "\n".join(linhas) or "_(nenhum plano cadastrado)_"
        teclado = [
            [InlineKeyboardButton("➕ Adicionar plano", callback_data="plano_add")],
            [InlineKeyboardButton("🗑️ Remover plano", callback_data="plano_remove")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_super")],
        ]
        await query.edit_message_text(
            f"🛒 *Botões de Compra / Planos*\n\n{lista}\n\n"
            f"Cada plano possui seu próprio grupo.\n\nO que deseja fazer?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado),
        )

    elif data == "plano_add":
        await query.edit_message_text(
            "➕ *Adicionar Plano*\n\n"
            "Envie no formato:\n"
            "`nome | preço | dias/vitalicio | link_do_grupo | id_do_grupo`\n\n"
            "Exemplo Mensal:\n"
            "`Mensal 🌸 | 14,90 | 30 | https://t.me/grupo_mensal | -1001234567890`\n\n"
            "Exemplo Vitalício:\n"
            "`Vitalício 🌸 | 19,90 | vitalicio | https://t.me/grupo_vitalicio | -1009876543210`\n\n"
            "⚠️ O bot precisa ser administrador em cada grupo.",
            parse_mode="Markdown",
        )
        ctx.user_data["aguardando"] = "plano_add"

    elif data == "plano_remove":
        planos = db.get("planos", [])
        if not planos:
            teclado = [[InlineKeyboardButton("⬅️ Voltar", callback_data="cfg_planos")]]
            await query.edit_message_text(
                "Nenhum plano para remover.",
                reply_markup=InlineKeyboardMarkup(teclado),
            )
            return

        teclado = [
            [InlineKeyboardButton(f"🗑️ {p['nome']}", callback_data=f"del_plano_{i}")]
            for i, p in enumerate(planos)
        ]
        teclado.append([InlineKeyboardButton("⬅️ Voltar", callback_data="cfg_planos")])
        await query.edit_message_text(
            "Qual plano deseja remover?",
            reply_markup=InlineKeyboardMarkup(teclado),
        )

    elif data.startswith("del_plano_"):
        idx = int(data.split("_")[2])
        planos = db.get("planos", [])

        if idx >= len(planos):
            await query.edit_message_text(
                "⚠️ Esse plano não existe mais.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Voltar", callback_data="cfg_planos")]
                ]),
            )
            return

        removido = planos.pop(idx)
        db["planos"] = planos
        salvar_db(db)

        linhas = []
        for i, p in enumerate(planos):
            link, gid = grupo_do_plano(p, db)
            linhas.append(
                f"{i+1}. {p['nome']} - R$ {formatar_preco(p['preco'])} - "
                f"{texto_duracao(p.get('dias', 30))}\n"
                f"   👥 {link or 'sem grupo'}"
            )
        lista = "\n".join(linhas) or "_(nenhum plano cadastrado)_"

        teclado = [
            [InlineKeyboardButton("➕ Adicionar plano", callback_data="plano_add")],
            [InlineKeyboardButton("🗑️ Remover plano", callback_data="plano_remove")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_super")],
        ]

        await query.edit_message_text(
            f"✅ Plano *{removido['nome']}* removido!\n\n"
            f"🛒 *Planos atuais:*\n{lista}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado),
        )
        return

    # ── Grupo ──────────────────────────────────
    elif data == "cfg_grupo":
        grupo = db.get("grupo", {})
        await query.edit_message_text(
            f"👥 *Configuração do Grupo*\n\n"
            f"Link atual: {grupo.get('link') or '_(não definido)_'}\n"
            f"ID do grupo: `{grupo.get('grupo_id') or '_(não definido)_'}`\n"
            f"Dias de acesso padrão: *{grupo.get('dias', 30)} dias*\n\n"
            f"Envie no formato:\n`link | dias | id_do_grupo`\n\n"
            f"Exemplo:\n`https://t.me/+abc123 | 30 | -1001234567890`\n\n"
            f"📌 *Como pegar o ID do grupo:*\n"
            f"Adicione @userinfobot no grupo e envie /start lá dentro.",
            parse_mode="Markdown",
        )
        ctx.user_data["aguardando"] = "grupo"

    # ── Gateway ────────────────────────────────
    elif data == "cfg_gateway":
        gw = db.get("gateway", {})
        ci = gw.get("client_id", "")
        await query.edit_message_text(
            f"💳 *Gateway MisticPay*\n\n"
            f"Client ID atual: `{ci[:10] + '...' if ci else '_(não definido)_'}`\n\n"
            f"Envie suas credenciais no formato:\n"
            f"`client_id | client_secret`\n\n"
            f"Você encontra isso no painel da MisticPay em:\n"
            f"misticpay.com → API → Credenciais",
            parse_mode="Markdown",
        )
        ctx.user_data["aguardando"] = "gateway"

    elif data == "voltar_super":
        ctx.user_data["aguardando"] = None
        teclado = [
            [InlineKeyboardButton("✏️ Texto de Apresentação", callback_data="cfg_apresentacao")],
            [InlineKeyboardButton("🛒 Botões de Compra / Planos", callback_data="cfg_planos")],
            [InlineKeyboardButton("👥 Link e Tempo do Grupo", callback_data="cfg_grupo")],
            [InlineKeyboardButton("💳 Gateway MisticPay", callback_data="cfg_gateway")],
        ]
        await query.edit_message_text(
            "🛠️ *Menu Super Admin*\n\nO que deseja configurar?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado),
        )
        return

# ══════════════════════════════════════════════
#  RECEBE CONFIGURAÇÃO DO ADMIN
# ══════════════════════════════════════════════
async def receber_configuracao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message

    # Alguns tipos de update do Telegram não possuem effective_user.
    # Ignoramos esses updates em vez de derrubar o handler.
    if user is None or msg is None or user.id != SUPER_ID:
        return

    aguardando = ctx.user_data.get("aguardando")
    if not aguardando:
        return

    db = carregar_db()
    ctx.user_data["aguardando"] = None

    # ── Apresentação ───────────────────────────
    if aguardando == "apresentacao":
        if msg.text:
            db["apresentacao"] = {"tipo": "texto", "texto": msg.text, "file_id": None}
            salvar_db(db)
            await msg.reply_text("✅ *Texto salvo!*", parse_mode="Markdown")
        elif msg.photo:
            db["apresentacao"] = {"tipo": "foto", "texto": msg.caption or "", "file_id": msg.photo[-1].file_id}
            salvar_db(db)
            await msg.reply_text("✅ *Imagem salva!*", parse_mode="Markdown")
        elif msg.video:
            db["apresentacao"] = {"tipo": "video", "texto": msg.caption or "", "file_id": msg.video.file_id}
            salvar_db(db)
            await msg.reply_text("✅ *Vídeo salvo!*", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Envie texto, foto ou vídeo.")
            return

    # ── Plano ──────────────────────────────────
    elif aguardando == "plano_add":
        try:
            partes = [p.strip() for p in msg.text.split("|")]
            if len(partes) != 5:
                raise ValueError("São necessários 5 campos.")

            nome = partes[0]
            preco = formatar_preco(partes[1])
            dias = normalizar_duracao(partes[2])
            link_grupo = partes[3]
            grupo_id = int(partes[4])

            if not link_grupo.startswith(("https://t.me/", "http://t.me/", "t.me/")):
                raise ValueError("Link do grupo inválido.")

            if "planos" not in db:
                db["planos"] = []

            db["planos"].append({
                "nome": nome,
                "preco": preco,
                "dias": dias,
                "link_grupo": link_grupo,
                "grupo_id": grupo_id,
            })
            salvar_db(db)

            await msg.reply_text(
                f"✅ *Plano adicionado!*\n\n"
                f"📦 {nome}\n"
                f"💰 R$ {preco}\n"
                f"⏱️ {texto_duracao(dias)}\n"
                f"👥 Grupo: {link_grupo}\n"
                f"🆔 ID: `{grupo_id}`",
                parse_mode="Markdown",
            )
        except Exception:
            await msg.reply_text(
                "⚠️ Formato inválido. Use exatamente:\n"
                "`nome | preço | dias/vitalicio | link_do_grupo | id_do_grupo`\n\n"
                "Mensal:\n"
                "`Mensal 🌸 | 14,90 | 30 | https://t.me/grupo_mensal | -1001234567890`\n\n"
                "Vitalício:\n"
                "`Vitalício 🌸 | 19,90 | vitalicio | https://t.me/grupo_vitalicio | -1009876543210`",
                parse_mode="Markdown",
            )

    # ── Grupo ──────────────────────────────────
    elif aguardando == "grupo":
        try:
            partes = [p.strip() for p in msg.text.split("|")]
            link, dias, grupo_id = partes[0], int(partes[1]), int(partes[2])
            db["grupo"] = {"link": link, "dias": dias, "grupo_id": grupo_id}
            salvar_db(db)
            await msg.reply_text(
                f"✅ *Grupo configurado!*\n\nLink: {link}\nDias: {dias}\nID: `{grupo_id}`\n\n"
                f"⚠️ Certifique-se que o bot está adicionado como *admin* no grupo!",
                parse_mode="Markdown",
            )
        except:
            await msg.reply_text("⚠️ Formato inválido. Use:\n`link | dias | id_do_grupo`\nEx: `https://t.me/+abc | 30 | -1001234567890`", parse_mode="Markdown")

    # ── Gateway ────────────────────────────────
    elif aguardando == "gateway":
        try:
            partes = [p.strip() for p in msg.text.split("|")]
            ci, cs = partes[0], partes[1]
            db["gateway"] = {"client_id": ci, "client_secret": cs}
            salvar_db(db)
            await msg.reply_text("✅ *Gateway MisticPay configurado!*\n\nAgora os clientes já podem pagar via PIX! 💳", parse_mode="Markdown")
        except:
            await msg.reply_text("⚠️ Formato inválido. Use:\n`client_id | client_secret`", parse_mode="Markdown")

    await mostrar_super(msg, ctx)

# ══════════════════════════════════════════════
#  DETECTA BOT ADICIONADO COMO ADMIN NO GRUPO
# ══════════════════════════════════════════════
async def bot_adicionado_grupo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    resultado = update.my_chat_member
    if not resultado:
        return

    chat = resultado.chat
    novo_status = resultado.new_chat_member

    if chat.type not in ("group", "supergroup"):
        return

    bot_id = ctx.bot.id

    if (
        novo_status.user.id == bot_id
        and novo_status.status == "administrator"
    ):
        try:
            await ctx.bot.send_message(
                SUPER_ID,
                f"✅ *Bot adicionado como admin!*\n\n"
                f"Grupo: *{chat.title}*\n"
                f"ID do grupo: `{chat.id}`\n\n"
                f"Use esse ID ao cadastrar o plano no /super.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif (
        novo_status.user.id == bot_id
        and novo_status.status in ("left", "kicked", "member", "restricted")
    ):
        try:
            await ctx.bot.send_message(
                SUPER_ID,
                f"⚠️ *Bot removido/rebaixado de um grupo*\n\n"
                f"Grupo: *{chat.title}*\n"
                f"ID: `{chat.id}`\n\n"
                f"Planos ligados a esse grupo não conseguirão remover membros expirados.",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def verificar_expirados(app):
    while True:
        try:
            db = carregar_db()
            usuarios = db.get("usuarios", {})
            agora = datetime.now()
            usuarios_para_apagar = []

            for uid, dados_usuario in list(usuarios.items()):
                assinaturas = dados_usuario.get("assinaturas", {})

                if assinaturas:
                    assinaturas_para_apagar = []

                    for chave, assinatura in list(assinaturas.items()):
                        expira = assinatura.get("expira")

                        if expira is None:
                            continue

                        try:
                            expira_dt = datetime.fromisoformat(expira)
                        except Exception:
                            continue

                        dias_restantes = (expira_dt - agora).days

                        if dias_restantes == 1 and not assinatura.get("aviso_enviado"):
                            try:
                                await app.bot.send_message(
                                    int(uid),
                                    f"⚠️ *Seu acesso expira amanhã!*\n\n"
                                    f"Plano: *{assinatura.get('plano', 'Plano')}*\n"
                                    f"Renove para não perder o acesso ao grupo.",
                                    parse_mode="Markdown",
                                )
                                assinatura["aviso_enviado"] = True
                                salvar_db(db)
                            except Exception:
                                pass

                        if agora > expira_dt:
                            grupo_id = assinatura.get("grupo_id")

                            if grupo_id:
                                try:
                                    await app.bot.ban_chat_member(int(grupo_id), int(uid))
                                    await app.bot.unban_chat_member(int(grupo_id), int(uid))
                                except Exception as e:
                                    logging.error(
                                        f"Erro ao remover {uid} do grupo {grupo_id}: {e}"
                                    )

                            try:
                                await app.bot.send_message(
                                    int(uid),
                                    f"❌ *Seu acesso expirou.*\n\n"
                                    f"Plano: *{assinatura.get('plano', 'Plano')}*\n"
                                    f"Você foi removido do grupo correspondente.\n"
                                    f"Use /start para renovar.",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass

                            assinaturas_para_apagar.append(chave)

                    for chave in assinaturas_para_apagar:
                        assinaturas.pop(chave, None)

                    if not assinaturas:
                        usuarios_para_apagar.append(uid)

                elif dados_usuario.get("expira"):
                    try:
                        expira_dt = datetime.fromisoformat(dados_usuario["expira"])
                    except Exception:
                        continue

                    if agora > expira_dt:
                        grupo_id = db.get("grupo", {}).get("grupo_id")
                        if grupo_id:
                            try:
                                await app.bot.ban_chat_member(int(grupo_id), int(uid))
                                await app.bot.unban_chat_member(int(grupo_id), int(uid))
                            except Exception:
                                pass
                        usuarios_para_apagar.append(uid)

            for uid in usuarios_para_apagar:
                usuarios.pop(uid, None)

            if usuarios_para_apagar:
                salvar_db(db)

        except Exception as e:
            logging.error(f"Erro na verificação de expirados: {e}")

        await asyncio.sleep(3600)


# ══════════════════════════════════════════════
#  Menu do bot
# ══════════════════════════════════════════════
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",   "🚀 Iniciar o bot"),
        BotCommand("status",  "📊 Ver minha assinatura"),
        BotCommand("suporte", "💬 Falar com suporte"),
    ])
    # Inicia a tarefa de remoção automática
    asyncio.create_task(verificar_expirados(app))


# ══════════════════════════════════════════════
#  HEALTH CHECK PARA HOSPEDAGEM
# ══════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot online")

    def log_message(self, format, *args):
        pass


def iniciar_health_server():
    porta = int(os.getenv("PORT", "8080"))
    servidor = HTTPServer(("0.0.0.0", porta), HealthHandler)
    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    logging.info(f"Health check ativo na porta {porta}")


# ══════════════════════════════════════════════
#  TRATAMENTO DE ERROS DO TELEGRAM
# ══════════════════════════════════════════════
async def telegram_error_handler(update, context):
    erro = context.error

    # Erros 502/Bad Gateway, timeout e falhas temporárias de rede
    # não devem encerrar o bot. O polling tentará novamente.
    if isinstance(erro, (NetworkError, TimedOut)):
        logging.warning(f"Falha temporária de rede/Telegram: {erro}. Tentando novamente...")
        return

    logging.exception("Erro não tratado no bot:", exc_info=erro)


def criar_app():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .post_init(post_init)
        .build()
    )

    app.add_handler(ChatMemberHandler(bot_adicionado_grupo, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("testar",  testar_gateway))
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("status",  status))
    app.add_handler(CommandHandler("suporte", suporte))
    app.add_handler(CommandHandler("super",   super_menu))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        ((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.VIDEO)
        & filters.ChatType.PRIVATE,
        receber_configuracao,
    ))
    app.add_error_handler(telegram_error_handler)

    return app


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
def main():
    app = criar_app()
    print("🤖 Bot rodando 24h com reconexão automática...")

    # run_polling gerencia inicialização, polling, reconexões e encerramento.
    # Não descartamos mensagens recebidas durante uma queda temporária.
    app.run_polling(
        poll_interval=0.0,
        timeout=20,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    iniciar_health_server()
    main()
