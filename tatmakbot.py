import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    logging.critical("No bot token was provided")
    raise SystemExit(1)

ids = os.environ.get("ADMIN_ID", "")
ADMIN_ID = [int(i) for i in ids.split(",")]
if not ADMIN_ID:
    logging.critical("No ADMIN_ID was provided")
    raise SystemExit(1)
try:
    ADMIN_ID = int(ADMIN_ID[0])
except ValueError:
    logging.critical("ADMIN_ID must be an integer")
    raise SystemExit(1)

CHOOSE_DISH_ADD, ENTER_QUANTITY_ADD = range(2)
CHOOSE_DISH_MODIFY, ENTER_NEW_QUANTITY = range(2, 4)

MAX_ITEMS_PER_USER = 10
CONVERSATION_TIMEOUT = 120
DATA_FILE = "orders.json"

MENU: List[List[Any]] = [
    ["Батыр", 250],
    ["Сочная", 195],
    ["Гирос без фри", 190],
    ["Гирос с Фри (пицца)", 220],
    ["Пицца с курицей", 170],
    ["Сборная", 175],
    ["Татмак", 210],
    ["Пицца с грибами", 170],
    ["Пицца с сыром", 150],
    ["Пицца с колбасой", 170],
    ["Карри-мини", 205],
    ["Барбекю", 205],
]
DISH_PRICE = {dish: price for dish, price in MENU}

orders: Dict[int, Dict[int, dict]] = {}


def load_orders() -> None:
    global orders
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            orders = {
                int(chat_id): {
                    int(user_id): user_data for user_id, user_data in chat_data.items()
                }
                for chat_id, chat_data in data.items()
            }
    except FileNotFoundError:
        orders = {}
    except Exception as e:
        logger.exception("Ошибка загрузки заказов: %s", e)
        orders = {}


def save_orders() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Ошибка сохранения заказов: %s", e)


def get_user_display(user) -> Tuple[str, str]:
    name = user.full_name
    alias = user.username or f"id{user.id}"
    return name, alias


def total_user_items(chat_id: int, user_id: int) -> int:
    if chat_id not in orders or user_id not in orders[chat_id]:
        return 0
    return sum(orders[chat_id][user_id]["items"].values())


def format_order(chat_id: int) -> str:
    if chat_id not in orders or not orders[chat_id]:
        return "Заказов пока нет"

    dish_dict: Dict[str, List[Tuple[str, str, int]]] = {}
    for _, data in orders[chat_id].items():
        name = data["name"]
        alias = data["alias"]
        items = data["items"]
        for dish, qty in items.items():
            dish_dict.setdefault(dish, []).append((name, alias, qty))

    lines = ["🍽 Текущие заказы:"]
    for dish, users in dish_dict.items():
        total_qty = sum(qty for _, _, qty in users)
        if len(users) == 1:
            name, alias, qty = users[0]
            alias_str = f"({alias})" if alias else ""
            lines.append(f"• {qty} {dish} {name} {alias_str}".strip())
            continue

        parts = []
        for name, alias, qty in users:
            alias_str = f"({alias})" if alias else ""
            parts.append(f"{qty} {name} {alias_str}".strip())

        details = ", ".join(parts[:-1]) + " и " + parts[-1] if len(parts) > 1 else parts[0]
        lines.append(f"• {total_qty} {dish} ({details})")

    return "\n".join(lines)


def calculate_total(chat_id: int) -> Tuple[str, int]:
    if chat_id not in orders or not orders[chat_id]:
        return "Заказов пока нет.", 0

    lines = ["💰 Расчёт заказа:"]
    grand_total = 0

    for _, data in orders[chat_id].items():
        name = data["name"]
        alias = data["alias"]
        items = data["items"]
        if not items:
            continue

        user_lines = [f"👤 {name} ({alias}):"]
        user_total = 0
        for dish, qty in items.items():
            price = DISH_PRICE.get(dish)
            if price is None:
                cost_str = "? руб."
            else:
                cost = price * qty
                cost_str = f"{cost} руб."
                user_total += cost
            user_lines.append(f"  • {dish} x{qty} = {cost_str}")

        user_lines.append(f"  Итого: {user_total} руб.")
        lines.extend(user_lines)
        grand_total += user_total

    if len(lines) == 1:
        return "Нет позиций для расчёта", 0

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"🧾 ОБЩАЯ СУММА: {grand_total} руб.")
    return "\n".join(lines), grand_total


def get_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Dict[str, Any]:
    sessions = context.user_data.setdefault("sessions", {})
    return sessions.setdefault(chat_id, {})


def clear_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    sessions = context.user_data.get("sessions", {})
    sessions.pop(chat_id, None)
    if not sessions:
        context.user_data.pop("sessions", None)


async def send_quantity_prompt(
    source_message: Message,
    dish: str,
    prompt_type: str,
) -> int:
    if prompt_type == "add":
        text = (
            f"Вы выбрали {dish}\n"
            f"Введите количество ОТВЕТОМ на это сообщение\n"
            f"Только целое положительное число, суммарно не более {MAX_ITEMS_PER_USER}"
        )
    else:
        text = (
            f"Изменение позиции: {dish}\n"
            "Введите новое количество ОТВЕТОМ на это сообщение\n"
            "Только целое положительное число"
        )

    sent = await source_message.reply_text(text)
    return sent.message_id


def is_expected_reply(update: Update, expected_message_id: Optional[int], bot_id: Optional[int]) -> bool:
    message = update.effective_message
    if message is None or message.reply_to_message is None:
        return False
    if expected_message_id is not None and message.reply_to_message.message_id != expected_message_id:
        return False
    reply_author = message.reply_to_message.from_user
    if bot_id is not None and reply_author and reply_author.id != bot_id:
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для оформления заказов в татмаке\n"
        "Доступные команды:\n"
        "/menu - показать меню\n"
        "/list - показать текущий заказ\n"
        "/total - показать стоимость заказов\n"
        "/add - добавить блюдо в заказ\n"
        "/modify - изменить количество блюда\n"
        "/remove - удалить свой заказ\n"
        "/cancel - отменить текущий ввод\n"
        "/over - сброс всех заказов (только крутых челиков)"
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not MENU:
        await update.message.reply_text("Меню пока пусто. Пожалуйста, добавьте блюда позже")
        return

    text = "📋 Меню:\n"
    for dish, price in MENU:
        text += f"• {dish} - {price} руб.\n"
    await update.message.reply_text(text)


async def list_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_order(update.effective_chat.id))


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, _ = calculate_total(update.effective_chat.id)
    await update.message.reply_text(text)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    clear_session(context, chat_id)

    if chat_id in orders and user_id in orders[chat_id]:
        del orders[chat_id][user_id]
        if not orders[chat_id]:
            orders.pop(chat_id, None)
        save_orders()
        await update.message.reply_text("Ваш заказ удалён")
        return

    await update.message.reply_text("У вас нет активного заказа")


async def over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_ID:
        await update.message.reply_text("⛔ У тебя нет доступа, лошара")
        return

    global orders
    orders = {}
    try:
        if os.path.exists(DATA_FILE):
            os.remove(DATA_FILE)
            await update.message.reply_text("🗑 Файл с заказами удалён. Все заказы сброшены")
        else:
            await update.message.reply_text("Файл с заказами не найден. Заказы в памяти очищены")
    except Exception as e:
        logger.exception("Ошибка удаления файла: %s", e)
        await update.message.reply_text("Ошибка при удалении файла")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_session(context, update.effective_chat.id)
    await update.message.reply_text("Действие отменено")
    return ConversationHandler.END


async def timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    if chat_id is None:
        return ConversationHandler.END

    session = get_session(context, chat_id)
    operation = session.get("operation", "действие")
    clear_session(context, chat_id)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Время ожидания истекло, {operation} отменено. Начните заново командой /add или /modify",
        )
    except Exception as e:
        logger.exception("Не удалось отправить сообщение о таймауте: %s", e)

    return ConversationHandler.END


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = get_session(context, chat_id)
    session.clear()
    session["operation"] = "добавление блюда"

    if not MENU:
        await update.message.reply_text("Меню пусто, невозможно добавить блюдо")
        clear_session(context, chat_id)
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{dish} - {price} руб.", callback_data=f"add_{dish}")]
        for dish, price in MENU
    ]
    await update.message.reply_text("Выберите блюдо:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_DISH_ADD


async def add_choose_dish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    session = get_session(context, chat_id)
    dish = query.data[4:]
    session["dish"] = dish
    session["operation"] = "добавление блюда"

    await query.edit_message_text(f"Выбрано: {dish}")
    prompt_message_id = await send_quantity_prompt(query.message, dish, "add")
    session["prompt_message_id"] = prompt_message_id
    return ENTER_QUANTITY_ADD


async def add_enter_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session = get_session(context, chat_id)

    if not is_expected_reply(update, session.get("prompt_message_id"), context.bot.id):
        wrong_attempts = session.get("wrong_attempts", 0) + 1
        session["wrong_attempts"] = wrong_attempts

        if wrong_attempts >= 2:
            clear_session(context, chat_id)
            await update.message.reply_text(
                "❌ Ввод отменён. Начните заново: /add"
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Ответьте именно на сообщение бота с запросом количества. "
            "Если отправите не туда ещё раз, я заберу все ваше имущество."
        )
        return ENTER_QUANTITY_ADD

    try:
        quantity = int(update.message.text)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите положительное целое число")
        return ENTER_QUANTITY_ADD

    dish = session.get("dish")
    if not dish:
        clear_session(context, chat_id)
        await update.message.reply_text("Ошибка: блюдо не выбрано. Начните заново с /add")
        return ConversationHandler.END

    current_total = total_user_items(chat_id, user_id)
    if current_total + quantity > MAX_ITEMS_PER_USER:
        await update.message.reply_text(
            f"❌ Суммарное количество заказанных порций не может превышать {MAX_ITEMS_PER_USER}. "
            f"У вас уже {current_total} порций. Попробуйте другое количество."
        )
        return ENTER_QUANTITY_ADD

    name, alias = get_user_display(update.effective_user)
    orders.setdefault(chat_id, {})
    orders[chat_id].setdefault(user_id, {"name": name, "alias": alias, "items": {}})

    current = orders[chat_id][user_id]["items"].get(dish, 0)
    orders[chat_id][user_id]["items"][dish] = current + quantity
    save_orders()
    clear_session(context, chat_id)

    await update.message.reply_text(f"✅ Добавлено {dish} x{quantity} в ваш заказ.\n\n{format_order(chat_id)}")
    return ConversationHandler.END


async def modify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session = get_session(context, chat_id)
    session.clear()
    session["operation"] = "изменение количества"

    if (
        chat_id not in orders
        or user_id not in orders[chat_id]
        or not orders[chat_id][user_id]["items"]
    ):
        clear_session(context, chat_id)
        await update.message.reply_text("У вас нет активных позиций для изменения")
        return ConversationHandler.END

    items = orders[chat_id][user_id]["items"]
    keyboard = [
        [InlineKeyboardButton(f"{dish} (x{qty})", callback_data=f"mod_{dish}")]
        for dish, qty in items.items()
    ]
    await update.message.reply_text(
        "Выберите блюдо для изменения количества:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_DISH_MODIFY


async def modify_choose_dish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    session = get_session(context, chat_id)
    dish = query.data[4:]
    session["dish"] = dish
    session["operation"] = "изменение количества"

    await query.edit_message_text(f"Выбрано для изменения: {dish}")
    prompt_message_id = await send_quantity_prompt(query.message, dish, "modify")
    session["prompt_message_id"] = prompt_message_id
    return ENTER_NEW_QUANTITY


async def modify_enter_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session = get_session(context, chat_id)

    if not is_expected_reply(update, session.get("prompt_message_id"), context.bot.id):
        wrong_attempts = session.get("wrong_attempts", 0) + 1
        session["wrong_attempts"] = wrong_attempts

        if wrong_attempts >= 2:
            clear_session(context, chat_id)
            await update.message.reply_text(
                "❌ Ввод отменён. Начните заново: /add"
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Ответьте именно на сообщение бота с запросом количества "
            "Если отправите не туда ещё раз, я заберу все ваше имущество"
        )
        return ENTER_QUANTITY_ADD

    try:
        new_qty = int(update.message.text)
        if new_qty <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите положительное целое число")
        return ENTER_NEW_QUANTITY

    dish = session.get("dish")
    if not dish:
        clear_session(context, chat_id)
        await update.message.reply_text("Ошибка. Начните заново с /modify")
        return ConversationHandler.END

    if (
        chat_id not in orders
        or user_id not in orders[chat_id]
        or dish not in orders[chat_id][user_id]["items"]
    ):
        clear_session(context, chat_id)
        await update.message.reply_text("Ошибка: позиция не найдена.")
        return ConversationHandler.END

    old_qty = orders[chat_id][user_id]["items"][dish]
    current_total = total_user_items(chat_id, user_id)
    new_total = current_total - old_qty + new_qty

    if new_total > MAX_ITEMS_PER_USER:
        await update.message.reply_text(
            f"❌ Суммарное количество заказанных порций не может превышать {MAX_ITEMS_PER_USER}. "
            f"У вас уже {current_total} порций. Попробуйте другое количество"
        )
        return ENTER_NEW_QUANTITY

    orders[chat_id][user_id]["items"][dish] = new_qty
    save_orders()
    clear_session(context, chat_id)

    await update.message.reply_text(
        f"✅ Количество {dish} изменено на {new_qty}.\n\n{format_order(chat_id)}"
    )
    return ConversationHandler.END


def build_conversation_handlers() -> List[ConversationHandler]:
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CHOOSE_DISH_ADD: [CallbackQueryHandler(add_choose_dish, pattern=r"^add_")],
            ENTER_QUANTITY_ADD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_enter_quantity)
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("add", add_start),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT,
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )

    modify_conv = ConversationHandler(
        entry_points=[CommandHandler("modify", modify_start)],
        states={
            CHOOSE_DISH_MODIFY: [CallbackQueryHandler(modify_choose_dish, pattern=r"^mod_")],
            ENTER_NEW_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, modify_enter_quantity)
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("modify", modify_start),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT,
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )

    return [add_conv, modify_conv]


def main() -> None:
    load_orders()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("list", list_order))
    application.add_handler(CommandHandler("total", total))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("over", over))

    for conv in build_conversation_handlers():
        application.add_handler(conv)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()