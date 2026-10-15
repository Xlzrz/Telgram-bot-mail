"""
Bot de Telegram -> CORREOS promocionales, con menu de botones, pago por transferencia
(concepto unico) y verificacion de membresia a canales/grupo.

Misma estructura que el bot de SMS: suscripciones Premium, menus con botones que editan
el mensaje, mesa de control del owner, staff, pagos pendientes, etc.
Lo unico que cambia es el envio: en vez de Twilio/SMS, ahora se manda por SMTP.

Requisitos:  pip install "python-telegram-bot==21.*"

Variables de entorno:
  BOT_TOKEN, ADMIN_ID, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL
  (opcional) FROM_NAME   -> nombre que ve el destinatario

Ejemplos de SMTP:
  Gmail:    SMTP_HOST=smtp.gmail.com   SMTP_PORT=587  (usa "contraseña de aplicación")
  Outlook:  SMTP_HOST=smtp.office365.com SMTP_PORT=587
  Brevo / Mailgun / SendGrid / Amazon SES: usa los datos SMTP que te den ellos
  (recomendado para volumen: una cuenta personal de Gmail/Outlook te limita por dia).

CONFIGURA ABAJO (seccion CONFIG):
  - REQUIRED_CHATS: el grupo + 2 canales obligatorios (el bot debe ser ADMIN en los 3
    para poder verificar membresia).
  - BANK_INFO: tus datos de transferencia (CLABE/banco/titular).
  - PLANS: nombre, dias, precio.

Ejecutar: python bot_email.py
"""

#Este bot es propiedad de @xlz_rz en Telegram y Xlzrz en GITHUB cualquier uso indebido o malicioso de este bot esta prohibido y no me hago responsable
import os, re, ssl, smtplib, sqlite3, asyncio, logging, random, string
from datetime import datetime, timedelta, date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, MessageHandler,
                          ConversationHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
FROM_EMAIL = os.environ["FROM_EMAIL"]
FROM_NAME = os.environ.get("FROM_NAME", "")

# ================= CONFIG (editar aqui) =================
DAILY_LIMIT = 10000                 # correos por usuario por dia (sin limite mensual)
                                    # OJO: tu proveedor SMTP tiene su propio limite diario
MAX_REPEAT_SAME_ADDRESS = 1000       # tope de envios repetidos AL MISMO correo por campaña
                                    # (enviar muchas veces lo mismo a una sola persona se
                                    # parece a acoso/spam y hace que el proveedor bloquee
                                    # tu cuenta o que te manden a la carpeta de spam)
SECONDS_BETWEEN_EMAILS = 1.0
MAX_SUBJECT = 120
MAX_BODY = 3000
FOOTER = "\n\n--\nSi no deseas recibir más correos, responde con la palabra BAJA."
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# nombre interno -> (dias, precio MXN, etiqueta boton)
PLANS = {
    "M": {"dias": 30, "precio": 300.00, "nombre": "Mensual"},
    "T": {"dias": 90, "precio": 750.00, "nombre": "Trimestral (90 días)"},
}

BANK_INFO = (
    "Banco: TU_BANCO\n"
    "CLABE: 0000 0000 0000 0000 00\n"
    "Titular: TU_NOMBRE"
)

# El grupo + 2 canales obligatorios. El bot debe ser ADMINISTRADOR en los 3.
# "id" acepta @usuario_publico o el id numerico (-100...) si son privados.
REQUIRED_CHATS = [
    {"id": "@tu_grupo_aqui",    "nombre": "Grupo oficial",  "link": "https://t.me/tu_grupo_aqui"},
    {"id": "@tu_canal1_aqui",   "nombre": "Canal 1",        "link": "https://t.me/tu_canal1_aqui"},
    {"id": "@tu_canal2_aqui",   "nombre": "Canal 2",        "link": "https://t.me/tu_canal2_aqui"},
]
# ==========================================================

ASK_EMAIL, ASK_SUBJECT, ASK_TEXT, ASK_MANUAL_UID, ASK_STAFF_UID = range(5)
QTY_OPTIONS = [1, 2, 5, 10]
STAFF_LABELS = {"coowner": "Co-owner", "developer": "Developer"}

db = sqlite3.connect("bot_email.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, accepted INTEGER DEFAULT 0, expires TEXT,
  used_today INTEGER DEFAULT 0, day TEXT);
CREATE TABLE IF NOT EXISTS pending_payments(
  concept TEXT PRIMARY KEY, user_id INTEGER, plan TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, plan TEXT,
  amount REAL, concept TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS staff(
  user_id INTEGER PRIMARY KEY, role TEXT, username TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS suppressed(
  email TEXT PRIMARY KEY, added TEXT);
""")

def get_user(uid):
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    row = db.execute("SELECT accepted,expires,used_today,day FROM users WHERE user_id=?",
                     (uid,)).fetchone()
    u = dict(zip(["accepted", "expires", "used_today", "day"], row))
    today = date.today().isoformat()
    if u["day"] != today:
        db.execute("UPDATE users SET used_today=0, day=? WHERE user_id=?", (today, uid))
        u["used_today"], u["day"] = 0, today
    db.commit()
    return u

def is_premium(u):
    return bool(u["expires"]) and datetime.fromisoformat(u["expires"]) > datetime.now()

def is_owner(uid):
    """Owner original (ADMIN_ID) o cualquier co-owner agregado: mismos permisos."""
    if uid == ADMIN_ID:
        return True
    row = db.execute("SELECT 1 FROM staff WHERE user_id=? AND role='coowner'", (uid,)).fetchone()
    return row is not None

def is_suppressed(email):
    return db.execute("SELECT 1 FROM suppressed WHERE email=?", (email.lower(),)).fetchone() is not None

def extend_subscription(uid, days):
    u = get_user(uid)
    base = datetime.now()
    if is_premium(u):
        base = datetime.fromisoformat(u["expires"])
    exp = (base + timedelta(days=days)).isoformat()
    db.execute("UPDATE users SET expires=? WHERE user_id=?", (exp, uid)); db.commit()
    return exp

def gen_concept(uid, plan_code):
    suf = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    concept = f"MAIL-{plan_code}-{uid}-{suf}"
    db.execute("INSERT INTO pending_payments VALUES(?,?,?,?)",
              (concept, uid, plan_code, datetime.now().isoformat()))
    db.commit()
    return concept

async def missing_chats(bot, uid):
    faltan = []
    for chat in REQUIRED_CHATS:
        try:
            m = await bot.get_chat_member(chat_id=chat["id"], user_id=uid)
            if m.status in ("left", "kicked"):
                faltan.append(chat)
        except Exception as e:
            logging.warning("No pude verificar %s: %s (¿el bot es admin ahí?)", chat["id"], e)
            faltan.append(chat)
    return faltan

# ================= Envio de correo (SMTP) =================
def send_email_sync(to_addr, subject, body):
    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL)) if FROM_NAME else FROM_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

# ================= Menu principal =================
def main_menu(uid):
    rows = [
        [InlineKeyboardButton("💎 Premium", callback_data="menu:premium")],
        [InlineKeyboardButton("🆘 Soporte", callback_data="menu:soporte")],
        [InlineKeyboardButton("📧 Mandar correo", callback_data="menu:enviar")],
        [InlineKeyboardButton("👥 Staff", callback_data="menu:staff")],
    ]
    if is_owner(uid):
        rows.append([InlineKeyboardButton("⚙️ Mesa de control", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await update.message.reply_text("Bienvenido. Elige una opción:",
                                    reply_markup=main_menu(update.effective_user.id))

async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu:premium" or data == "menu:back_premium":
        u = get_user(uid)
        if is_premium(u):
            txt = f"Tu suscripción está activa hasta el {u['expires'][:10]}."
        else:
            txt = "No tienes una suscripción activa. Elige un plan:"
        kb = [[InlineKeyboardButton(f"{p['nombre']} - ${p['precio']:.2f} MXN",
                                    callback_data=f"plan:{code}")] for code, p in PLANS.items()]
        kb.append([InlineKeyboardButton("⬅️ Regresar", callback_data="menu:home")])
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("plan:"):
        code = data.split(":")[1]
        plan = PLANS[code]
        concept = gen_concept(uid, code)
        await q.edit_message_text(
            f"Plan {plan['nombre']} - ${plan['precio']:.2f} MXN\n\n"
            f"Transfiere a:\n{BANK_INFO}\n\n"
            f"⚠️ Usa EXACTAMENTE este concepto de pago:\n`{concept}`\n\n"
            "En cuanto se confirme tu pago con ese concepto, tu suscripción se "
            "activa automáticamente. Esto puede tardar si la transferencia no es "
            "instantánea.",
            parse_mode="Markdown")

    elif data == "menu:soporte":
        await q.edit_message_text(
            "🆘 Soporte\n\nEscríbele directamente al administrador si tienes algún "
            "problema con tu suscripción o tus envíos.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Escribir a @xlz_rz", url="https://t.me/xlz_rz")],
                [InlineKeyboardButton("⬅️ Regresar", callback_data="menu:home")],
            ]))

    elif data == "menu:staff":
        try:
            owner_chat = await ctx.bot.get_chat(ADMIN_ID)
            owner_display = f"@{owner_chat.username}" if owner_chat.username else f"ID {ADMIN_ID}"
        except Exception:
            owner_display = f"ID {ADMIN_ID}"
        coowners = db.execute("SELECT user_id, username FROM staff WHERE role='coowner'").fetchall()
        devs = db.execute("SELECT user_id, username FROM staff WHERE role='developer'").fetchall()
        fmt = lambda rows: "\n".join(f"@{un}" if un else f"ID {i}" for i, un in rows) or "Ninguno"
        texto = (f"👥 Staff\n\n👑 Owner:\n{owner_display}\n\n"
                f"🧑‍💼 Co-owners:\n{fmt(coowners)}\n\n"
                f"💻 Developers:\n{fmt(devs)}")
        kb = []
        if is_owner(uid):
            kb.append([InlineKeyboardButton("➕ Agregar co-owner", callback_data="staff:add:coowner")])
            kb.append([InlineKeyboardButton("➕ Agregar developer", callback_data="staff:add:developer")])
            if coowners or devs:
                kb.append([InlineKeyboardButton("🗑️ Quitar staff", callback_data="staff:removelist")])
        kb.append([InlineKeyboardButton("⬅️ Regresar", callback_data="menu:home")])
        await q.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "staff:removelist":
        if not is_owner(uid):
            return
        rows = db.execute("SELECT user_id, role, username FROM staff").fetchall()
        kb = [[InlineKeyboardButton(f"🗑️ {STAFF_LABELS[r]}: {('@'+un) if un else i}",
                                    callback_data=f"staff:remove:{i}")] for i, r, un in rows]
        kb.append([InlineKeyboardButton("⬅️ Regresar", callback_data="menu:staff")])
        await q.edit_message_text("Toca para quitar a alguien del staff:",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("staff:remove:"):
        if not is_owner(uid):
            return
        target = int(data.split(":")[2])
        db.execute("DELETE FROM staff WHERE user_id=?", (target,)); db.commit()
        await q.edit_message_text(f"Usuario {target} quitado del staff.",
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("⬅️ Regresar", callback_data="menu:staff")]]))

    elif data == "menu:home":
        await q.edit_message_text("Elige una opción:", reply_markup=main_menu(uid))

    elif data in ("menu:enviar", "join:recheck"):
        # IMPORTANTE: devolver el estado para que el ConversationHandler lo registre
        return await intentar_enviar(update, ctx, via_callback=True)

# ================= Flujo "Mandar correo" =================
async def intentar_enviar(update, ctx, via_callback=False):
    uid = update.effective_user.id
    send = update.callback_query.edit_message_text if via_callback else update.message.reply_text
    u = get_user(uid)

    if not u["accepted"]:
        kb = [[InlineKeyboardButton("✅ Acepto", callback_data="acepto:si")]]
        await send("Antes de continuar: confirmas que solo enviarás correos a direcciones "
                  "que aceptaron recibir tus promociones y que respetarás las bajas (BAJA).",
                  reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END

    if not is_premium(u):
        kb = [[InlineKeyboardButton("💎 Ver planes", callback_data="menu:premium")]]
        await send("Necesitas una suscripción Premium activa para enviar correos.",
                  reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END

    faltan = await missing_chats(ctx.bot, uid)
    if faltan:
        kb = [[InlineKeyboardButton(f"➡️ {c['nombre']}", url=c["link"])] for c in faltan]
        kb.append([InlineKeyboardButton("✅ Ya me uní", callback_data="join:recheck")])
        await send("Para usar el bot debes unirte a estos canales/grupo:",
                  reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END

    restante = DAILY_LIMIT - u["used_today"]
    if restante <= 0:
        await send(f"Llegaste al límite diario de {DAILY_LIMIT} correos. Se reinicia mañana.")
        return ConversationHandler.END

    await send(f"Escribe el correo de destino (ej. cliente@ejemplo.com).\n"
              f"Puedes cancelar con /cancelar. Te quedan {restante} correos hoy.")
    return ASK_EMAIL

async def acepto_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    db.execute("UPDATE users SET accepted=1 WHERE user_id=?", (q.from_user.id,)); db.commit()
    await q.edit_message_text("Confirmado. Pulsa /menu y elige 'Mandar correo' para continuar.")

async def ask_email(update, ctx):
    addr = update.message.text.strip().lower()
    if not EMAIL_RE.match(addr):
        await update.message.reply_text("Correo inválido. Usa el formato: cliente@ejemplo.com")
        return ASK_EMAIL
    if is_suppressed(addr):
        await update.message.reply_text(
            "Esa dirección pidió no recibir más correos y no se puede usar. "
            "Escribe otra o usa /cancelar.")
        return ASK_EMAIL
    ctx.user_data["email"] = addr
    await update.message.reply_text(f"Escribe el ASUNTO del correo (máx. {MAX_SUBJECT} caracteres):")
    return ASK_SUBJECT

async def ask_subject(update, ctx):
    subject = " ".join(update.message.text.split())[:MAX_SUBJECT]   # sin saltos de linea
    if not subject:
        await update.message.reply_text("El asunto no puede estar vacío. Escríbelo de nuevo:")
        return ASK_SUBJECT
    ctx.user_data["asunto"] = subject
    await update.message.reply_text("Ahora escribe el mensaje (cuerpo) del correo:")
    return ASK_TEXT

async def ask_text(update, ctx):
    ctx.user_data["texto"] = update.message.text.strip()[:MAX_BODY]
    u = get_user(update.effective_user.id)
    restante = DAILY_LIMIT - u["used_today"]
    opciones = [n for n in QTY_OPTIONS if n <= min(MAX_REPEAT_SAME_ADDRESS, restante)]
    if not opciones:
        await update.message.reply_text(f"Llegaste al límite diario de {DAILY_LIMIT} correos.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(str(n), callback_data=f"qty:{n}") for n in opciones]]
    await update.message.reply_text("¿Cuántas veces quieres enviarlo a ese correo?",
                                    reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def qty_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    qty = int(q.data.split(":")[1])
    addr, asunto, texto = (ctx.user_data.get("email"), ctx.user_data.get("asunto"),
                           ctx.user_data.get("texto"))
    if not (addr and asunto and texto):
        await q.edit_message_text("Esta solicitud ya no es válida, usa /menu de nuevo.")
        return
    ctx.user_data["qty"] = qty
    kb = [[InlineKeyboardButton("✅ Confirmar envío", callback_data="send:confirm")],
         [InlineKeyboardButton("❌ Cancelar", callback_data="send:cancel")]]
    await q.edit_message_text(
        f"Para: {addr}\nAsunto: {asunto}\n\nMensaje:\n{texto}{FOOTER}\n\nVeces: {qty}\n\n¿Confirmas?",
        reply_markup=InlineKeyboardMarkup(kb))

async def send_confirm_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "send:cancel":
        ctx.user_data.clear()
        await q.edit_message_text("Envío cancelado.")
        return
    addr, asunto, texto, qty = (ctx.user_data.get("email"), ctx.user_data.get("asunto"),
                                ctx.user_data.get("texto"), ctx.user_data.get("qty"))
    if not (addr and asunto and texto and qty):
        await q.edit_message_text("Esta solicitud ya no es válida, intenta de nuevo con /menu.")
        return
    # respeta el limite diario real aunque el usuario haya tardado en confirmar
    restante = DAILY_LIMIT - get_user(uid)["used_today"]
    qty = min(qty, restante)
    if qty <= 0:
        ctx.user_data.clear()
        await q.edit_message_text(f"Llegaste al límite diario de {DAILY_LIMIT} correos.")
        return
    if is_suppressed(addr):
        ctx.user_data.clear()
        await q.edit_message_text("Esa dirección pidió no recibir más correos. Envío cancelado.")
        return

    body = texto + FOOTER
    ok = fail = 0
    await q.edit_message_text("Enviando...")
    for _ in range(qty):
        try:
            await asyncio.to_thread(send_email_sync, addr, asunto, body)
            ok += 1
        except smtplib.SMTPRecipientsRefused as e:
            fail += 1
            logging.warning("Correo rechazado %s: %s", addr, e)
            break                       # la direccion no existe / rechazada: no insistir
        except smtplib.SMTPAuthenticationError as e:
            fail += 1
            logging.error("Fallo de autenticacion SMTP: %s", e)
            break
        except Exception as e:
            fail += 1
            logging.warning("Fallo correo a %s: %s", addr, e)
        await asyncio.sleep(SECONDS_BETWEEN_EMAILS)
    db.execute("UPDATE users SET used_today=used_today+? WHERE user_id=?", (ok, uid)); db.commit()
    ctx.user_data.clear()
    await ctx.bot.send_message(uid, f"Terminado. Enviados: {ok} | Fallidos: {fail}")

async def cancelar(update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("Operación cancelada.")
    return ConversationHandler.END

async def menu_cmd(update, ctx):
    await update.message.reply_text("Elige una opción:",
                                    reply_markup=main_menu(update.effective_user.id))

# ================= Admin =================
async def confirmar_pago(update, ctx):
    if not is_owner(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usa: /confirmarpago CONCEPTO"); return
    concept = ctx.args[0].strip().upper()
    row = db.execute("SELECT user_id, plan FROM pending_payments WHERE concept=?",
                     (concept,)).fetchone()
    if not row:
        await update.message.reply_text("Concepto no encontrado o ya usado."); return
    uid, code = row
    plan = PLANS[code]
    exp = extend_subscription(uid, plan["dias"])
    db.execute("DELETE FROM pending_payments WHERE concept=?", (concept,))
    try:
        chat = await ctx.bot.get_chat(uid)
        username = chat.username or ""
    except Exception:
        username = ""
    db.execute("INSERT INTO payments(user_id,username,plan,amount,concept,date) VALUES(?,?,?,?,?,?)",
              (uid, username, plan["nombre"], plan["precio"], concept, datetime.now().isoformat()))
    db.commit()
    await update.message.reply_text(
        f"Activado: usuario {uid}, plan {plan['nombre']}, vence {exp[:10]}.")
    try:
        await ctx.bot.send_message(
            uid, f"Pago confirmado. Tu plan {plan['nombre']} está activo hasta el {exp[:10]}. "
                f"Usa /menu para empezar.")
    except Exception:
        pass

async def pendientes(update, ctx):
    if not is_owner(update.effective_user.id): return
    rows = db.execute("SELECT concept,user_id,plan,created FROM pending_payments "
                      "ORDER BY created DESC").fetchall()
    if not rows:
        await update.message.reply_text("No hay pagos pendientes."); return
    lineas = "\n".join(f"{c} | user {u} | {PLANS[p]['nombre']} | {t[:16]}" for c, u, p, t in rows)
    await update.message.reply_text(f"Pagos pendientes:\n{lineas}")

def stats_text():
    now = datetime.now()
    month, today = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
    total = q("SELECT SUM(amount) FROM payments")
    mes = q("SELECT SUM(amount) FROM payments WHERE date LIKE ?", month + "%")
    hoy = q("SELECT SUM(amount) FROM payments WHERE date LIKE ?", today + "%")
    n_pagos = q("SELECT COUNT(*) FROM payments")
    activos = q("SELECT COUNT(*) FROM users WHERE expires > ?", now.isoformat())
    usuarios = q("SELECT COUNT(*) FROM users")
    ultimos = db.execute("SELECT date,username,user_id,plan,amount FROM payments "
                         "ORDER BY id DESC LIMIT 10").fetchall()
    lineas = "\n".join(f"{d[:16]} | @{un or uid} | {p} | ${a:.2f}"
                       for d, un, uid, p, a in ultimos) or "Sin pagos aún."
    return (f"PANEL DE PAGOS\n\n"
           f"Ingresos hoy: ${hoy:.2f}\nIngresos del mes: ${mes:.2f}\n"
           f"Ingresos totales: ${total:.2f} ({n_pagos} pagos)\n"
           f"Suscripciones activas: {activos} de {usuarios} usuarios\n\n"
           f"Últimos pagos:\n{lineas}")

async def panel(update, ctx):
    if not is_owner(update.effective_user.id): return
    await update.message.reply_text(stats_text())

# ============= Owner panel (mesa de control, solo botones) =============
def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin:stats")],
        [InlineKeyboardButton("💳 Pagos pendientes", callback_data="admin:pending")],
        [InlineKeyboardButton("👥 Usuarios premium", callback_data="admin:users")],
        [InlineKeyboardButton("➕ Activar manual", callback_data="admin:manual")],
    ])

async def admin_manual_start(update, ctx):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("No autorizado.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    await q.edit_message_text("Escribe el ID de Telegram del usuario a activar:")
    return ASK_MANUAL_UID

async def admin_manual_uid(update, ctx):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("Debe ser un número. Escribe el ID de Telegram:")
        return ASK_MANUAL_UID
    ctx.user_data["manual_uid"] = int(txt)
    kb = [[InlineKeyboardButton(f"{p['nombre']} ({p['dias']} días)",
                                callback_data=f"admin:manualdays:{code}")] for code, p in PLANS.items()]
    await update.message.reply_text("Elige el plan a otorgar:", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def admin_manualdays_callback(update, ctx):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("No autorizado.", show_alert=True)
        return
    await q.answer()
    code = q.data.split(":")[2]
    uid = ctx.user_data.get("manual_uid")
    if not uid:
        await q.edit_message_text("Esta solicitud ya no es válida, usa el botón de nuevo.")
        return
    plan = PLANS[code]
    exp = extend_subscription(uid, plan["dias"])
    await q.edit_message_text(f"Usuario {uid} activo con plan {plan['nombre']} hasta {exp[:10]}.",
                              reply_markup=BACK_ADMIN)
    try:
        await ctx.bot.send_message(uid, f"Tu plan {plan['nombre']} está activo hasta el {exp[:10]}. "
                                        f"Usa /menu para empezar.")
    except Exception:
        pass

async def staff_add_start(update, ctx):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("No autorizado.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    role = q.data.split(":")[2]            # "coowner" o "developer"
    ctx.user_data["staff_role"] = role
    await q.edit_message_text(
        f"Escribe el ID de Telegram del usuario a agregar como {STAFF_LABELS[role]}:")
    return ASK_STAFF_UID

async def staff_add_uid(update, ctx):
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("Debe ser un número. Escribe el ID de Telegram:")
        return ASK_STAFF_UID
    target = int(txt)
    role = ctx.user_data.get("staff_role")
    try:
        chat = await ctx.bot.get_chat(target)
        username = chat.username or ""
    except Exception:
        username = ""
    db.execute("INSERT OR REPLACE INTO staff VALUES(?,?,?,?)",
              (target, role, username, datetime.now().isoformat()))
    db.commit()
    label = STAFF_LABELS[role]
    await update.message.reply_text(
        f"Listo: {target} (@{username}) agregado como {label}."
        if username else f"Listo: {target} agregado como {label}.")
    try:
        if role == "coowner":
            aviso = ("Fuiste agregado como Co-owner del bot: tienes los mismos permisos "
                    "que el owner (confirmar pagos, activar usuarios, manejar la mesa de "
                    "control). Usa /start.")
        else:
            aviso = "Fuiste agregado como Developer en los créditos del bot. Usa /start."
        await ctx.bot.send_message(target, aviso)
    except Exception:
        pass
    return ConversationHandler.END

BACK_ADMIN = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Regresar", callback_data="admin:home")]])

async def owner_panel(update, ctx):
    if not is_owner(update.effective_user.id): return
    await update.message.reply_text("Mesa de control", reply_markup=owner_menu())

async def admin_router(update, ctx):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("No autorizado.", show_alert=True)
        return
    await q.answer()
    data = q.data

    if data == "admin:home":
        await q.edit_message_text("Mesa de control", reply_markup=owner_menu())

    elif data == "admin:stats":
        await q.edit_message_text(stats_text(), reply_markup=BACK_ADMIN)

    elif data == "admin:pending":
        rows = db.execute("SELECT concept,user_id,plan,created FROM pending_payments "
                          "ORDER BY created DESC LIMIT 25").fetchall()
        if not rows:
            await q.edit_message_text("No hay pagos pendientes.", reply_markup=BACK_ADMIN)
            return
        kb = [[InlineKeyboardButton(f"✅ {c} ({PLANS[p]['nombre']}, user {u})",
                                    callback_data=f"admin:confirm:{c}")] for c, u, p, t in rows]
        kb.append([InlineKeyboardButton("⬅️ Regresar", callback_data="admin:home")])
        await q.edit_message_text("Toca un concepto para confirmarlo:",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("admin:confirm:"):
        concept = data.split(":", 2)[2]
        row = db.execute("SELECT user_id, plan FROM pending_payments WHERE concept=?",
                         (concept,)).fetchone()
        if not row:
            await q.edit_message_text("Ese concepto ya no existe o ya se confirmó.",
                                      reply_markup=BACK_ADMIN)
            return
        uid, code = row
        plan = PLANS[code]
        exp = extend_subscription(uid, plan["dias"])
        db.execute("DELETE FROM pending_payments WHERE concept=?", (concept,))
        try:
            chat = await ctx.bot.get_chat(uid)
            username = chat.username or ""
        except Exception:
            username = ""
        db.execute("INSERT INTO payments(user_id,username,plan,amount,concept,date) "
                  "VALUES(?,?,?,?,?,?)",
                  (uid, username, plan["nombre"], plan["precio"], concept, datetime.now().isoformat()))
        db.commit()
        await q.edit_message_text(
            f"Activado: usuario {uid}, plan {plan['nombre']}, vence {exp[:10]}.",
            reply_markup=BACK_ADMIN)
        try:
            await ctx.bot.send_message(
                uid, f"Pago confirmado. Tu plan {plan['nombre']} está activo hasta el "
                    f"{exp[:10]}. Usa /menu para empezar.")
        except Exception:
            pass

    elif data == "admin:users":
        rows = db.execute("SELECT user_id, expires FROM users WHERE expires > ? "
                          "ORDER BY expires DESC LIMIT 30", (datetime.now().isoformat(),)).fetchall()
        if not rows:
            await q.edit_message_text("No hay usuarios premium activos.", reply_markup=BACK_ADMIN)
            return
        kb = [[InlineKeyboardButton(f"🔴 Desactivar {uid}", callback_data=f"admin:deact:{uid}")]
             for uid, exp in rows]
        kb.append([InlineKeyboardButton("⬅️ Regresar", callback_data="admin:home")])
        texto = "Usuarios premium activos:\n" + "\n".join(
            f"{uid} - vence {exp[:10]}" for uid, exp in rows)
        await q.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("admin:deact:"):
        uid = int(data.split(":")[2])
        db.execute("UPDATE users SET expires=NULL WHERE user_id=?", (uid,)); db.commit()
        await q.edit_message_text(f"Usuario {uid} desactivado.", reply_markup=BACK_ADMIN)

async def activar_manual(update, ctx):
    if not is_owner(update.effective_user.id): return
    try:
        uid, dias = int(ctx.args[0]), int(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usa: /activar USER_ID DIAS"); return
    exp = extend_subscription(uid, dias)
    await update.message.reply_text(f"Usuario {uid} activo hasta {exp[:10]}.")

async def desactivar(update, ctx):
    if not is_owner(update.effective_user.id): return
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usa: /desactivar USER_ID"); return
    db.execute("UPDATE users SET expires=NULL WHERE user_id=?", (uid,)); db.commit()
    await update.message.reply_text("Suscripción desactivada.")

# ---- Lista de bajas (correos que respondieron BAJA) ----
async def bloquear(update, ctx):
    if not is_owner(update.effective_user.id): return
    if not ctx.args or not EMAIL_RE.match(ctx.args[0].strip()):
        await update.message.reply_text("Usa: /bloquear correo@ejemplo.com"); return
    addr = ctx.args[0].strip().lower()
    db.execute("INSERT OR IGNORE INTO suppressed VALUES(?,?)", (addr, datetime.now().isoformat()))
    db.commit()
    await update.message.reply_text(f"{addr} agregado a la lista de bajas. Ya nadie podrá enviarle.")

async def desbloquear(update, ctx):
    if not is_owner(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usa: /desbloquear correo@ejemplo.com"); return
    db.execute("DELETE FROM suppressed WHERE email=?", (ctx.args[0].strip().lower(),)); db.commit()
    await update.message.reply_text("Listo, quitado de la lista de bajas.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_router, pattern="^menu:enviar$"),
                     CallbackQueryHandler(menu_router, pattern="^join:recheck$")],
        states={
            ASK_EMAIL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_subject)],
            ASK_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_text)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_manual_start, pattern="^admin:manual$")],
        states={
            ASK_MANUAL_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_manual_uid)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )

    staff_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(staff_add_start, pattern="^staff:add:")],
        states={
            ASK_STAFF_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_uid)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("confirmarpago", confirmar_pago))
    app.add_handler(CommandHandler("pendientes", pendientes))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("activar", activar_manual))
    app.add_handler(CommandHandler("desactivar", desactivar))
    app.add_handler(CommandHandler("bloquear", bloquear))
    app.add_handler(CommandHandler("desbloquear", desbloquear))
    app.add_handler(CommandHandler("ownerpanel", owner_panel))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(conv)
    app.add_handler(admin_conv)
    app.add_handler(staff_conv)
    app.add_handler(CallbackQueryHandler(acepto_callback, pattern="^acepto:si$"))
    app.add_handler(CallbackQueryHandler(qty_callback, pattern="^qty:"))
    app.add_handler(CallbackQueryHandler(send_confirm_callback, pattern="^send:"))
    app.add_handler(CallbackQueryHandler(admin_manualdays_callback, pattern="^admin:manualdays:"))
    app.add_handler(CallbackQueryHandler(admin_router, pattern="^admin:"))
    app.add_handler(CallbackQueryHandler(menu_router))  # catch-all: premium/soporte/home/staff/plan:*

    app.run_polling()

if __name__ == "__main__":
    main()
