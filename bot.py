import logging
import json
import os
import uuid
import asyncio
import aiohttp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
BOT_TOKEN    = "8996653670:AAFJladKB7_eVP39PGXVnloLJ9w0MWk2Ddo"
SUPER_ID     = 7194320806
LINK_SUPORTE = "https://t.me/geovannapriv"
# ─────────────────────────────────────────────

DB_FILE = "banco.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

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
    if update.effective_user.id != SUPER_ID:
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
            f"{plano['nome']} R$ {plano['preco']}",
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
    db = carregar_db()
    uid = str(update.effective_user.id)
    usuario = db.get("usuarios", {}).get(uid)

    if not usuario:
        await update.message.reply_text(
            "📊 *Minha Assinatura*\n\n❌ Você não possui nenhuma assinatura ativa.\n\nUse /start para ver nossos planos!",
            parse_mode="Markdown",
        )
        return

    expira = usuario.get("expira", "")
    plano = usuario.get("plano", "")
    try:
        expira_dt = datetime.fromisoformat(expira)
        restante = (expira_dt - datetime.now()).days
        if restante < 0:
            status_txt = "❌ Expirado"
        else:
            status_txt = f"✅ Ativo — {restante} dias restantes"
    except:
        status_txt = "⚠️ Indefinido"

    await update.message.reply_text(
        f"📊 *Minha Assinatura*\n\n"
        f"📦 Plano: *{plano}*\n"
        f"📅 Expira em: *{expira[:10]}*\n"
        f"🔋 Status: {status_txt}",
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
    if update.effective_user.id != SUPER_ID:
        await update.message.reply_text("⛔ Sem permissão.")
        return
    await mostrar_super(update.message, ctx)

async def mostrar_super(msg, ctx):
    teclado = [
        [InlineKeyboardButton("✏️ Texto de Apresentação", callback_data="cfg_apresentacao")],
        [InlineKeyboardButton("🛒 Botões de Compra / Planos", callback_data="cfg_planos")],
        [InlineKeyboardButton("👥 Link e Tempo do Grupo", callback_data="cfg_grupo")],
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
                float(plano["preco"].replace(",", ".")), user.first_name,
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

            # Salva pagamento pendente
            db["pagamentos_pendentes"][tx_id] = {
                "user_id": str(user.id),
                "plano_idx": idx,
                "plano_nome": plano["nome"],
                "plano_dias": plano.get("dias", db["grupo"]["dias"]),
                "criado_em": datetime.now().isoformat(),
            }
            salvar_db(db)

            texto = (
                f"💳 *Pagamento PIX*\n\n"
                f"📦 Plano: *{plano['nome']} R$ {plano['preco']}*\n"
                f"💰 Valor: *R$ {plano['preco']}*\n"
                f"⏱️ Acesso: *{plano.get('dias', db['grupo']['dias'])} dias*\n\n"
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

        await query.answer("🔍 Verificando pagamento...", show_alert=False)

        try:
            resp = await verificar_pagamento(gw["client_id"], gw["client_secret"], tx_id)
            estado = resp.get("data", {}).get("transactionState", "PENDENTE")

            if estado == "APROVADO":
                uid = pendente["user_id"]
                dias = pendente["plano_dias"]
                expira = (datetime.now() + timedelta(days=dias)).isoformat()

                if "usuarios" not in db:
                    db["usuarios"] = {}
                db["usuarios"][uid] = {
                    "plano": pendente["plano_nome"],
                    "expira": expira,
                    "comprado_em": datetime.now().isoformat(),
                }
                del db["pagamentos_pendentes"][tx_id]
                salvar_db(db)

                link_grupo = db.get("grupo", {}).get("link", "")
                await query.message.reply_text(
                    f"✅ *Pagamento Confirmado!*\n\n"
                    f"Parabéns! Seu acesso foi liberado por *{dias} dias*.\n\n"
                    f"👇 Clique no link abaixo para entrar no grupo:\n{link_grupo}",
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
            await query.answer(f"Erro: {str(e)}", show_alert=True)
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
        await query.answer("⛔ Sem permissão.", show_alert=True)
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
        lista = "\n".join([f"{i+1}. {p['nome']} — R$ {p['preco']} — {p.get('dias',30)} dias" for i, p in enumerate(planos)]) or "_(nenhum plano cadastrado)_"
        teclado = [
            [InlineKeyboardButton("➕ Adicionar plano", callback_data="plano_add")],
            [InlineKeyboardButton("🗑️ Remover plano", callback_data="plano_remove")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_super")],
        ]
        await query.edit_message_text(
            f"🛒 *Botões de Compra / Planos*\n\n{lista}\n\nO que deseja fazer?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(teclado),
        )

    elif data == "plano_add":
        await query.edit_message_text(
            "➕ *Adicionar Plano*\n\n"
            "Envie no formato:\n"
            "`nome | preço | dias`\n\n"
            "Exemplo:\n"
            "`VIP Mensal | 29.90 | 30`",
            parse_mode="Markdown",
        )
        ctx.user_data["aguardando"] = "plano_add"

    elif data == "plano_remove":
        planos = db.get("planos", [])
        if not planos:
            await query.answer("Nenhum plano para remover.", show_alert=True)
            return
        teclado = [[InlineKeyboardButton(f"🗑️ {p['nome']}", callback_data=f"del_plano_{i}")] for i, p in enumerate(planos)]
        teclado.append([InlineKeyboardButton("⬅️ Voltar", callback_data="cfg_planos")])
        await query.edit_message_text("Qual plano deseja remover?", reply_markup=InlineKeyboardMarkup(teclado))

    elif data.startswith("del_plano_"):
        idx = int(data.split("_")[2])
        planos = db.get("planos", [])
        if idx < len(planos):
            removido = planos.pop(idx)
            db["planos"] = planos
            salvar_db(db)
            await query.answer(f"✅ Plano '{removido['nome']}' removido!", show_alert=True)
        await callback_handler(update, ctx)  # volta pra lista

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
        await mostrar_super(query.message, ctx)

# ══════════════════════════════════════════════
#  RECEBE CONFIGURAÇÃO DO ADMIN
# ══════════════════════════════════════════════
async def receber_configuracao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ID:
        return
    aguardando = ctx.user_data.get("aguardando")
    if not aguardando:
        return

    db = carregar_db()
    msg = update.message
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
            nome, preco, dias = partes[0], partes[1], int(partes[2])
            if "planos" not in db:
                db["planos"] = []
            db["planos"].append({"nome": nome, "preco": preco, "dias": dias})
            salvar_db(db)
            await msg.reply_text(f"✅ *Plano adicionado!*\n\n📦 {nome}\n💰 R$ {preco}\n⏱️ {dias} dias", parse_mode="Markdown")
        except:
            await msg.reply_text("⚠️ Formato inválido. Use:\n`nome | preço | dias`\nEx: `VIP | 29.90 | 30`", parse_mode="Markdown")

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

    # Só age em grupos/supergrupos
    if chat.type not in ("group", "supergroup"):
        return

    bot_id = ctx.bot.id

    # Bot foi promovido a admin
    if (
        novo_status.user.id == bot_id
        and novo_status.status in ("administrator",)
    ):
        db = carregar_db()
        db["grupo"]["grupo_id"] = chat.id
        salvar_db(db)

        # Avisa o admin no privado
        try:
            await ctx.bot.send_message(
                SUPER_ID,
                f"✅ *Bot adicionado como admin!*\n\n"
                f"Grupo: *{chat.title}*\n"
                f"ID salvo: `{chat.id}`\n\n"
                f"Remoção automática de membros expirados está ativa! 🤖",
                parse_mode="Markdown",
            )
        except:
            pass

    # Bot foi removido ou rebaixado
    elif (
        novo_status.user.id == bot_id
        and novo_status.status in ("left", "kicked", "member", "restricted")
    ):
        db = carregar_db()
        if db["grupo"].get("grupo_id") == chat.id:
            db["grupo"]["grupo_id"] = None
            salvar_db(db)
            try:
                await ctx.bot.send_message(
                    SUPER_ID,
                    f"⚠️ *Bot removido ou rebaixado no grupo!*\n\n"
                    f"Grupo: *{chat.title}*\n\n"
                    f"A remoção automática foi desativada.\n"
                    f"Adicione o bot como admin novamente para reativar.",
                    parse_mode="Markdown",
                )
            except:
                pass


async def verificar_expirados(app):
    while True:
        try:
            db = carregar_db()
            grupo_id = db.get("grupo", {}).get("grupo_id")
            usuarios = db.get("usuarios", {})
            agora = datetime.now()
            removidos = []

            for uid, dados in list(usuarios.items()):
                try:
                    expira = datetime.fromisoformat(dados["expira"])
                    dias_restantes = (expira - agora).days

                    # Avisa 1 dia antes de expirar
                    if dias_restantes == 1 and not dados.get("aviso_enviado"):
                        try:
                            await app.bot.send_message(
                                int(uid),
                                "⚠️ *Seu acesso expira amanhã!*\n\n"
                                "Renove agora para não perder o acesso ao grupo.\n"
                                "Use /start para renovar.",
                                parse_mode="Markdown",
                            )
                            dados["aviso_enviado"] = True
                            salvar_db(db)
                        except:
                            pass

                    # Remove se expirou
                    if agora > expira:
                        if grupo_id:
                            try:
                                await app.bot.ban_chat_member(grupo_id, int(uid))
                                await app.bot.unban_chat_member(grupo_id, int(uid))  # unban pra não bloquear permanente
                            except:
                                pass
                        try:
                            await app.bot.send_message(
                                int(uid),
                                "❌ *Seu acesso ao grupo expirou.*\n\n"
                                "Você foi removido do grupo.\n"
                                "Use /start para renovar seu acesso!",
                                parse_mode="Markdown",
                            )
                        except:
                            pass
                        removidos.append(uid)
                except:
                    pass

            for uid in removidos:
                del db["usuarios"][uid]
            if removidos:
                salvar_db(db)
                logging.info(f"Removidos {len(removidos)} usuário(s) expirado(s).")

        except Exception as e:
            logging.error(f"Erro na verificação de expirados: {e}")

        await asyncio.sleep(3600)  # Verifica a cada 1 hora

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
#  MAIN
# ══════════════════════════════════════════════
async def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(ChatMemberHandler(bot_adicionado_grupo, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("testar",  testar_gateway))
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("status",  status))
    app.add_handler(CommandHandler("suporte", suporte))
    app.add_handler(CommandHandler("super",   super_menu))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO,
        receber_configuracao,
    ))
    print("🤖 Bot rodando...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
