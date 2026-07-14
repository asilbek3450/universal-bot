
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# wikipedia, Harry Potter, Valyuta with emojie
menyu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="Wikipedia 📝"),
            KeyboardButton(text="Harry Potter 📚"),
            KeyboardButton(text="Valyuta 💰")
        ],
        [
            KeyboardButton(text="Instagram 📸"),
            KeyboardButton(text="Youtube 🎥"),
        ]
    ],
    resize_keyboard=True
)