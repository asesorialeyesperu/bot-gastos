import os
import json
import logging
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
import httpx

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
NOTION_DB_ID      = os.environ["NOTION_DB_ID"]
TU_CHAT_ID        = os.environ.get("TU_CHAT_ID", "")

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CLIENTE ANTHROPIC ────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

CATEGORIAS = [
    "Alimentación", "Transporte", "Entretenimiento",
    "Salud", "Educación", "Hogar", "Ropa", "Tecnología", "Otros"
]

# ─── FUNCIÓN: PARSEAR GASTO CON IA ────────────────────────────────────────────
def parsear_gasto(texto: str) -> dict | None:
    prompt = f"""Analiza este texto de gasto personal en soles peruanos y extrae la información.
Responde SOLO con JSON válido, sin backticks ni texto adicional.

Texto: "{texto}"

Formato requerido:
{{"descripcion": "descripción breve del gasto", "monto": número_en_soles, "categoria": "una de: {', '.join(CATEGORIAS)}", "nota": "detalle extra si lo hay o cadena vacía"}}

Reglas:
- Si el texto menciona dólares, convierte a soles multiplicando por 3.75
- Si no hay monto claro, pon monto: 0
- La categoría debe ser exactamente una de las opciones listadas
- descripcion debe ser corta (máximo 5 palabras)"""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ─── FUNCIÓN: GUARDAR EN NOTION ───────────────────────────────────────────────
def guardar_en_notion(gasto: dict) -> str:
    hoy = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Descripción": {"title": [{"text": {"content": gasto["descripcion"]}}]},
            "Monto": {"number": float(gasto["monto"])},
            "Categoría": {"select": {"name": gasto["categoria"]}},
            "Fecha": {"date": {"start": hoy}},
            "Nota": {"rich_text": [{"text": {"content": gasto.get("nota", "")}}]},
            "Registrado vía": {"select": {"name": "Telegram"}}
        }
    }
    with httpx.Client() as client:
        resp = client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28"
            },
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("url", "")

# ─── FUNCIÓN: OBTENER DATOS DEL MES ───────────────────────────────────────────
def obtener_datos_mes(inicio: str, fin: str) -> dict:
    payload = {
        "filter": {
            "and": [
                {"property": "Fecha", "date": {"on_or_after": inicio}},
                {"property": "Fecha", "date": {"on_or_before": fin}}
            ]
        },
        "page_size": 100
    }
    with httpx.Client() as client:
        resp = client.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28"
            },
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        gastos = resp.json().get("results", [])

    total = 0
    por_cat = {}
    for g in gastos:
        props = g["properties"]
        monto = props.get("Monto", {}).get("number") or 0
        cat = (props.get("Categoría", {}).get("select") or {}).get("name", "Otros")
        total += monto
        por_cat[cat] = por_cat.get(cat, 0) + monto

    return {"total": total, "por_categoria": por_cat, "cantidad": len(gastos)}

def obtener_resumen_mes() -> dict:
    ahora = datetime.now()
    inicio = ahora.replace(day=1).strftime("%Y-%m-%d")
    fin = ahora.strftime("%Y-%m-%d")
    return obtener_datos_mes(inicio, fin)

# ─── FUNCIÓN: GENERAR TEXTO DE RESUMEN ────────────────────────────────────────
def generar_texto_resumen(data: dict, titulo: str) -> str:
    if not data["cantidad"]:
        return f"📊 *{titulo}*\n\nNo hay gastos registrados en este período."

    lineas = [f"📊 *{titulo}*\n"]
    lineas.append(f"💰 Total: *S/ {data['total']:.2f}*")
    lineas.append(f"🧾 Gastos registrados: {data['cantidad']}\n")

    if data["por_categoria"]:
        lineas.append("*Por categoría:*")
        sorted_cats = sorted(data["por_categoria"].items(), key=lambda x: x[1], reverse=True)
        for cat, monto in sorted_cats:
            pct = (monto / data["total"] * 100) if data["total"] > 0 else 0
            lineas.append(f"  • {cat}: S/ {monto:.2f} ({pct:.0f}%)")

    return "\n".join(lineas)

# ─── RESUMEN AUTOMÁTICO SEMANAL ───────────────────────────────────────────────
async def enviar_resumen_semanal(app: Application):
    if not TU_CHAT_ID:
        logger.warning("TU_CHAT_ID no configurado, no se puede enviar resumen semanal.")
        return

    ahora = datetime.now()
    # Últimos 7 días
    from datetime import timedelta
    inicio = (ahora - timedelta(days=6)).strftime("%Y-%m-%d")
    fin = ahora.strftime("%Y-%m-%d")

    try:
        data_semana = obtener_datos_mes(inicio, fin)
        data_mes = obtener_resumen_mes()

        semana_txt = generar_texto_resumen(data_semana, "Resumen semanal")
        mes_txt = generar_texto_resumen(data_mes, f"Acumulado del mes ({ahora.strftime('%B')})")

        mensaje = f"🌅 *Buenos días, Brian\\!*\n\n{semana_txt}\n\n{mes_txt}"
        await app.bot.send_message(
            chat_id=int(TU_CHAT_ID),
            text=mensaje,
            parse_mode="Markdown"
        )
        logger.info("Resumen semanal enviado.")
    except Exception as e:
        logger.error(f"Error enviando resumen semanal: {e}")

# ─── HANDLERS DE TELEGRAM ─────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Hola Brian\\! Soy tu bot de gastos\\.\n\n"
        "📝 *Cómo registrar un gasto:*\n"
        "Solo escríbeme en lenguaje natural:\n"
        "• `almuerzo en La Mar 85`\n"
        "• `taxi a Miraflores 22 soles`\n"
        "• `Netflix 45`\n\n"
        "📊 *Comandos disponibles:*\n"
        "/resumen \\- Gasto del mes actual\n"
        "/semana \\- Gasto de los últimos 7 días\n"
        "/ayuda \\- Ver esta ayuda\n\n"
        "⏰ Cada domingo recibirás un resumen automático\\."
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Consultando Notion...")
    try:
        data = obtener_resumen_mes()
        mes = datetime.now().strftime("%B %Y")
        texto = generar_texto_resumen(data, f"Resumen de {mes}")
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en resumen: {e}")
        await update.message.reply_text("❌ Error al consultar Notion. Intenta en un momento.")

async def semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Consultando Notion...")
    try:
        from datetime import timedelta
        ahora = datetime.now()
        inicio = (ahora - timedelta(days=6)).strftime("%Y-%m-%d")
        fin = ahora.strftime("%Y-%m-%d")
        data = obtener_datos_mes(inicio, fin)
        texto = generar_texto_resumen(data, "Últimos 7 días")
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en resumen semanal: {e}")
        await update.message.reply_text("❌ Error al consultar Notion.")

async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if TU_CHAT_ID and str(update.effective_user.id) != TU_CHAT_ID:
        await update.message.reply_text("⛔ No estás autorizado.")
        return

    texto = update.message.text.strip()
    if not texto:
        return

    await update.message.reply_text("⏳ Procesando...")

    try:
        gasto = parsear_gasto(texto)

        if not gasto or gasto.get("monto", 0) == 0:
            await update.message.reply_text(
                "🤔 No pude detectar un monto. Intenta con algo como:\n"
                "`almuerzo 35` o `taxi 22 soles`",
                parse_mode="Markdown"
            )
            return

        guardar_en_notion(gasto)

        respuesta = (
            f"✅ *Guardado en Notion*\n\n"
            f"📝 {gasto['descripcion']}\n"
            f"💵 S/ {gasto['monto']:.2f}\n"
            f"🏷️ {gasto['categoria']}"
        )
        if gasto.get("nota"):
            respuesta += f"\n📌 {gasto['nota']}"

        await update.message.reply_text(respuesta, parse_mode="Markdown")

    except json.JSONDecodeError:
        await update.message.reply_text("❌ No entendí ese gasto. Intenta con más detalle.")
    except httpx.HTTPError as e:
        logger.error(f"Error Notion: {e}")
        await update.message.reply_text("❌ Error al guardar en Notion.")
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        await update.message.reply_text("❌ Ocurrió un error. Intenta de nuevo.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("semana", semana))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))

    # ─── SCHEDULER: resumen automático los domingos a las 9:00 AM (hora Lima) ──
    scheduler = AsyncIOScheduler(timezone="America/Lima")
    scheduler.add_job(
        enviar_resumen_semanal,
        trigger="cron",
        day_of_week="sun",
        hour=9,
        minute=0,
        args=[app]
    )
    scheduler.start()
    logger.info("Scheduler activado: resumen automático los domingos a las 9:00 AM (Lima).")

    logger.info("Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
