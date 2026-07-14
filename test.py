import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ContentType

TOKEN = "8419775960:AAHkvKsliEqiFKgLOYsXQx6w0a2FyWGKvZs"

bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(F.content_type == ContentType.VIDEO)
async def get_video_file_id(message: Message):
    file_id = message.video.file_id
    await message.answer(f"📁 Video file_id:\n\n{file_id}")


@dp.message()
async def other_messages(message: Message):
    await message.answer("📹 Iltimos, video yuboring.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())