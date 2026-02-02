import os
import re
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

RATE_TEXT = (
    "Актуальный курс:\\n"
    "Перевод A → B: 1 X = 1.6 Y\\n"
    "Перевод B → A: 2.2 Y = 1 X"
)

DIR_1_TITLE = "Из 🇦 в 🇧"
DIR_2_TITLE = "Из 🇧 в 🇦"

class Form(StatesGroup):
    direction = State()
    amount = State()
    bank_from = State()
    bank_to = State()
    card_number = State()
    fio = State()
    phone = State()

def is_digits_only(s: str) -> bool:
    return s.strip().isdigit()


def is_letters_only(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ\\s\\-]+", s.strip()))

def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Актуальный курс", callback_data="rate")
    kb.button(text=DIR_1_TITLE, callback_data="dir1")
    kb.button(text=DIR_2_TITLE, callback_data="dir2")
    kb.adjust(1)
    return kb.as_markup()

def back_to_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Меню", callback_data="menu")
    return kb.as_markup()

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден. Проверь .env (BOT_TOKEN=...)")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        name = message.from_user.first_name or "друг"
        await message.answer(
            f"Приветствую, {name}! Я чат-бот.\\nВыберите подходящий пункт 👇",
            reply_markup=menu_kb()
        )

    @dp.callback_query(F.data == "menu")
    async def cb_menu(call: CallbackQuery, state: FSMContext):
        await state.clear()
        name = call.from_user.first_name or "друг"
        await call.message.edit_text(
            f"Приветствую, {name}! Я чат-бот.\\nВыберите подходящий пункт 👇",
            reply_markup=menu_kb()
        )
        await call.answer()

    @dp.callback_query(F.data == "rate")
    async def cb_rate(call: CallbackQuery):
        await call.message.edit_text(RATE_TEXT, reply_markup=back_to_menu_kb())
        await call.answer()

    @dp.callback_query(F.data == "dir1")
    async def cb_dir1(call: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.update_data(direction="A_TO_B")
        await state.set_state(Form.amount)
        await call.message.edit_text(
            "Для перевода (A → B) необходимо заполнить заявку:\\n\\n"
            "1) Сумма перевода (только цифры):",
            reply_markup=back_to_menu_kb()
        )
        await call.answer()

    @dp.callback_query(F.data == "dir2")
    async def cb_dir2(call: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.update_data(direction="B_TO_A")
        await state.set_state(Form.amount)
        await call.message.edit_text(
            "Для перевода (B → A) необходимо заполнить заявку:\\n\\n"
            "1) Сумма перевода (только цифры):",
            reply_markup=back_to_menu_kb()
        )
        await call.answer()

    @dp.message(Form.amount)
    async def form_amount(message: Message, state: FSMContext):
        if not is_digits_only(message.text):
            return await message.answer("Ошибка: сумма только цифрами. Введите ещё раз:")
        await state.update_data(amount=message.text.strip())
        await state.set_state(Form.bank_from)
        await message.answer("2) Банк с которого переводите (буквы/цифры):")

    @dp.message(Form.bank_from)
    async def form_bank_from(message: Message, state: FSMContext):
        txt = message.text.strip()
        if not txt:
            return await message.answer("Ошибка: пусто. Введите ещё раз:")
        await state.update_data(bank_from=txt)
        await state.set_state(Form.bank_to)
        await message.answer("3) Банк получателя (буквы/цифры):")

    @dp.message(Form.bank_to)
    async def form_bank_to(message: Message, state: FSMContext):
        txt = message.text.strip()
        if not txt:
            return await message.answer("Ошибка: пусто. Введите ещё раз:")
        await state.update_data(bank_to=txt)
        await state.set_state(Form.card_number)
        await message.answer("4) Номер карты получателя (только цифры):")

    @dp.message(Form.card_number)
    async def form_card(message: Message, state: FSMContext):
        txt = message.text.strip()
        if not is_digits_only(txt):
            return await message.answer("Ошибка: карта только цифрами. Введите ещё раз:")
        await state.update_data(card_number=txt)
        await state.set_state(Form.fio)
        await message.answer("5) ФИО получателя (только буквы):")

    @dp.message(Form.fio)
    async def form_fio(message: Message, state: FSMContext):
        txt = message.text.strip()
        if not is_letters_only(txt):
            return await message.answer("Ошибка: ФИО только буквами (можно пробел/дефис). Введите ещё раз:")
        await state.update_data(fio=txt)
        await state.set_state(Form.phone)
        await message.answer("6) Номер телефона получателя (только цифры):")

    @dp.message(Form.phone)
    async def form_phone(message: Message, state: FSMContext):
        txt = message.text.strip()
        if not is_digits_only(txt):
            return await message.answer("Ошибка: телефон только цифрами. Введите ещё раз:")

        await state.update_data(phone=txt)
        data = await state.get_data()

        await message.answer(
            "Проверьте данные:\\n"
            f"Сумма перевода: {data.get('amount')}\\n"
            f"ФИО получателя: {data.get('fio')}\\n"
            f"Номер телефона: {data.get('phone')}",
            reply_markup=back_to_menu_kb()
        )
        await state.clear()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


