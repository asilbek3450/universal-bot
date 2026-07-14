import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import instaloader
import requests
import wikipedia
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

try:
    from pytubefix import YouTube
except ImportError:  # pragma: no cover - fallback for older environments
    from pytube import YouTube

from buttons import menyu
from config import API_TOKEN, RAPIDAPI_KEY


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

DOWNLOADS_DIR = Path("downloads")
YOUTUBE_DIR = Path("videos")
TELEGRAM_SAFE_VIDEO_SIZE = 49 * 1024 * 1024
MAX_INSTAGRAM_POSTS = 50
MAX_INSTAGRAM_PAGES = 5

INSTAGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
INSTAGRAM_SELECTIONS: dict[int, list[dict]] = {}
INSTAGRAM_SHORTCODE_CACHE: dict[str, dict] = {}


class BotStates(StatesGroup):
    wiki_savol = State()
    harry_potter_qahramon = State()
    instagram = State()
    youtube = State()


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    result = []
    while text:
        part = text[:limit]
        split_at = max(part.rfind("\n"), part.rfind(". "), part.rfind(" "))
        if split_at < limit // 2:
            split_at = limit
        result.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return result


async def answer_long(msg: Message, text: str, reply_markup=None) -> None:
    parts = chunks(text)
    for index, part in enumerate(parts):
        await msg.answer(part, reply_markup=reply_markup if index == len(parts) - 1 else None)


def request_json(url: str, *, timeout: int = 20, **kwargs):
    response = requests.get(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def post_json(url: str, *, timeout: int = 25, **kwargs):
    response = requests.post(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def fetch_wikipedia_summary(query: str) -> str:
    wikipedia.set_lang("uz")
    try:
        return wikipedia.summary(query, sentences=3, auto_suggest=False)
    except wikipedia.exceptions.DisambiguationError as exc:
        variants = "\n".join(f"- {item}" for item in exc.options[:8])
        return f"Bu savol bo'yicha bir nechta mavzu topildi. Aniqroq yozing:\n{variants}"
    except wikipedia.exceptions.PageError:
        search_results = wikipedia.search(query, results=3)
        if not search_results:
            raise
        return wikipedia.summary(search_results[0], sentences=3, auto_suggest=False)


def fetch_harry_potter_character(query: str) -> dict | None:
    data = request_json("https://hp-api.onrender.com/api/characters")
    query = query.lower()
    for hero in data:
        if query in hero.get("name", "").lower():
            return hero
    return None


def fetch_currency_rates() -> str:
    data = request_json("https://cbu.uz/uz/arkhiv-kursov-valyut/json/")
    by_code = {item.get("Ccy"): item for item in data}
    lines = []
    for code, label in (("USD", "Dollar"), ("EUR", "Yevro"), ("RUB", "Rubl")):
        item = by_code.get(code)
        if item:
            lines.append(f"{label}: 1 {code} = {item['Rate']} so'm")

    date = next((item.get("Date") for item in data if item.get("Date")), "")
    if date:
        lines.append(f"\nSana: {date}")
    return "\n".join(lines) if lines else "Valyuta ma'lumotlari topilmadi."


def normalize_instagram_input(text: str) -> tuple[str, str]:
    value = clean_text(text)

    if value.startswith("@"):
        username = value[1:]
        if INSTAGRAM_USERNAME_RE.fullmatch(username):
            return "profile", username

    parsed = urlparse(value)
    if parsed.netloc and "instagram.com" in parsed.netloc.lower():
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("Instagram linki to'liq emas.")

        kind = parts[0].lower()
        if kind in {"p", "reel", "reels", "tv"} and len(parts) >= 2:
            return "post", parts[1]

        username = parts[0].lstrip("@")
        if INSTAGRAM_USERNAME_RE.fullmatch(username):
            return "profile", username

    if INSTAGRAM_USERNAME_RE.fullmatch(value):
        return "profile", value

    raise ValueError("Instagram uchun @username yoki post/reel link yuboring.")


def create_instaloader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        download_comments=False,
        download_geotags=False,
        download_pictures=True,
        download_video_thumbnails=False,
        download_videos=True,
        quiet=True,
        save_metadata=False,
        compress_json=False,
    )


def fetch_instagram_profile(username: str) -> tuple[str, str | None]:
    loader = create_instaloader()
    profile = instaloader.Profile.from_username(loader.context, username)
    bio = profile.biography.strip()
    if len(bio) > 700:
        bio = bio[:700].rstrip() + "..."

    full_name = profile.full_name or "Ko'rsatilmagan"
    text = (
        f"Instagram profil\n"
        f"Username: @{profile.username}\n"
        f"Ism: {full_name}\n"
        f"Postlar: {profile.mediacount}\n"
        f"Obunachilar: {profile.followers}\n"
        f"Kuzatayotganlari: {profile.followees}"
    )
    if profile.is_private:
        text += "\nHolati: yopiq profil"
    if bio:
        text += f"\n\nBio:\n{bio}"

    return text, profile.profile_pic_url


def instagram_caption(node: dict) -> str:
    caption = node.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text") or ""
    elif isinstance(caption, str):
        text = caption
    else:
        text = ""

    if len(text) > 900:
        return text[:900].rstrip() + "..."
    return text


def extract_instagram_media(node: dict, seen: set[str] | None = None) -> list[dict[str, str]]:
    seen = seen or set()
    media: list[dict[str, str]] = []
    has_children = False

    for child in node.get("carousel_media") or []:
        has_children = True
        media.extend(extract_instagram_media(child, seen))

    sidecar = node.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict):
        for edge in sidecar.get("edges") or []:
            child = edge.get("node", edge)
            if isinstance(child, dict):
                has_children = True
                media.extend(extract_instagram_media(child, seen))

    if has_children and media:
        return media

    video_versions = node.get("video_versions") or []
    if video_versions:
        url = next((item.get("url") for item in video_versions if item.get("url")), "")
        if url and url not in seen:
            seen.add(url)
            media.append({"type": "video", "url": url})
        return media

    for key in ("video_url", "playable_url"):
        url = node.get(key)
        if isinstance(url, str) and url.startswith("http") and url not in seen:
            seen.add(url)
            media.append({"type": "video", "url": url})
            return media

    image_versions = node.get("image_versions2") or {}
    candidates = image_versions.get("candidates") or []
    url = next((item.get("url") for item in candidates if item.get("url")), "")
    if not url:
        url = node.get("display_url") or node.get("thumbnail_src") or node.get("url")

    if isinstance(url, str) and url.startswith("http") and url not in seen:
        seen.add(url)
        media.append({"type": "photo", "url": url})

    return media


def instagram_post_kind(node: dict, media: list[dict[str, str]]) -> str:
    product_type = str(node.get("product_type") or "").lower()
    typename = str(node.get("__typename") or node.get("typename") or "").lower()
    media_type = node.get("media_type")

    if len(media) > 1 or media_type == 8:
        return "carousel"
    if "clips" in product_type or "clips" in typename or node.get("clips_metadata"):
        return "reels"
    if any(item["type"] == "video" for item in media) or media_type == 2:
        return "video"
    return "image"


def instagram_post_url(shortcode: str, kind: str) -> str:
    if kind == "reels":
        return f"https://www.instagram.com/reel/{shortcode}/"
    return f"https://www.instagram.com/p/{shortcode}/"


def fetch_instagram_posts_from_rapidapi(username: str) -> tuple[str, list[dict]]:
    if not RAPIDAPI_KEY:
        raise RuntimeError("RapidAPI key topilmadi.")

    result: list[dict] = []
    seen_shortcodes: set[str] = set()
    max_id = ""

    for _ in range(MAX_INSTAGRAM_PAGES):
        data = post_json(
            "https://instagram120.p.rapidapi.com/api/instagram/posts",
            json={"username": username, "maxId": max_id},
            headers={
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "instagram120.p.rapidapi.com",
                "Content-Type": "application/json",
            },
        )
        response_result = data.get("result", {})
        edges = response_result.get("edges") or []
        if not edges:
            break

        for edge in edges:
            node = edge.get("node", edge)
            if not isinstance(node, dict):
                continue

            shortcode = node.get("code") or node.get("shortcode") or node.get("pk") or node.get("id")
            shortcode = str(shortcode or "").strip()
            if not shortcode or shortcode in seen_shortcodes:
                continue

            media = extract_instagram_media(node)
            if not media:
                continue

            caption = instagram_caption(node)
            kind = instagram_post_kind(node, media)
            seen_shortcodes.add(shortcode)
            result.append(
                {
                    "shortcode": shortcode,
                    "kind": kind,
                    "url": instagram_post_url(shortcode, kind),
                    "caption": caption,
                    "media": media,
                }
            )
            if len(result) >= MAX_INSTAGRAM_POSTS:
                break

        if len(result) >= MAX_INSTAGRAM_POSTS:
            break

        page_info = response_result.get("page_info") or {}
        next_id = page_info.get("end_cursor") or ""
        if not page_info.get("has_next_page") or not next_id or next_id == max_id:
            break
        max_id = next_id

    if not result:
        raise RuntimeError("Instagram media topilmadi.")

    return f"@{username} so'nggi medialari", result


def fetch_instagram_post(shortcode: str) -> tuple[str, list[dict[str, str]]]:
    loader = create_instaloader()
    post = instaloader.Post.from_shortcode(loader.context, shortcode)

    caption = post.caption or ""
    if len(caption) > 900:
        caption = caption[:900].rstrip() + "..."

    title = f"@{post.owner_username}"
    if caption:
        title = f"{title}\n\n{caption}"

    media: list[dict[str, str]] = []
    if post.typename == "GraphSidecar":
        for item in post.get_sidecar_nodes():
            if item.is_video and item.video_url:
                media.append({"type": "video", "url": item.video_url})
            elif item.display_url:
                media.append({"type": "photo", "url": item.display_url})
    elif post.is_video and post.video_url:
        media.append({"type": "video", "url": post.video_url})
    else:
        media.append({"type": "photo", "url": post.url})

    return title, media[:10]


def normalize_youtube_url(text: str) -> str:
    value = clean_text(text)
    parsed = urlparse(value)
    host = parsed.netloc.lower().replace("www.", "")

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"

        if parsed.path.startswith("/shorts/"):
            parts = [part for part in parsed.path.split("/") if part]
            video_id = parts[1] if len(parts) >= 2 else ""
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"

    raise ValueError("YouTube link noto'g'ri. Masalan: https://youtu.be/video_id")


def download_youtube_video(link: str) -> tuple[Path, str]:
    YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
    yt = YouTube(link)
    streams = (
        yt.streams.filter(progressive=True, file_extension="mp4")
        .order_by("resolution")
        .desc()
    )

    stream = None
    for item in streams:
        size = item.filesize or item.filesize_approx or 0
        if not size or size <= TELEGRAM_SAFE_VIDEO_SIZE:
            stream = item
            break

    if stream is None:
        stream = streams.last()
    if stream is None:
        raise RuntimeError("Bu video uchun mos MP4 stream topilmadi.")

    file_path = Path(stream.download(output_path=str(YOUTUBE_DIR)))
    return file_path, yt.title


async def send_instagram_media(msg: Message, media: list[dict[str, str]], default_caption: str) -> None:
    for index, item in enumerate(media):
        caption = item.get("caption") or (default_caption if index == 0 else None)
        if caption and len(caption) > 1024:
            caption = caption[:1000].rstrip() + "..."

        if item["type"] == "video":
            await msg.answer_video(video=item["url"], caption=caption)
        else:
            await msg.answer_photo(photo=item["url"], caption=caption)

    await msg.answer("Tayyor.", reply_markup=menyu)


def caption_preview(caption: str, limit: int = 70) -> str:
    text = " ".join((caption or "").split())
    if not text:
        return "caption yo'q"
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def build_instagram_keyboard(user_id: int, posts: list[dict]) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for index in range(1, len(posts) + 1):
        row.append(
            InlineKeyboardButton(
                text=str(index),
                callback_data=f"ig:{user_id}:{index}",
            )
        )
        if len(row) == 5:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def instagram_posts_list_text(username: str, posts: list[dict]) -> str:
    labels = {
        "reels": "Reels",
        "video": "Video",
        "image": "Image",
        "carousel": "Post",
    }
    lines = [f"@{username} uchun {len(posts)} ta media topildi.", "Kerakli raqamni bosing:"]

    for index, post in enumerate(posts, start=1):
        label = labels.get(post.get("kind"), "Post")
        preview = caption_preview(post.get("caption", ""))
        line = f"{index}. {label} - {preview}"
        if sum(len(item) + 1 for item in lines) + len(line) > 3900:
            lines.append(f"... yana {len(posts) - index + 1} ta bor. Tugmalardan tanlang.")
            break
        lines.append(line)

    return "\n".join(lines)


def cache_instagram_posts(user_id: int, posts: list[dict]) -> None:
    INSTAGRAM_SELECTIONS[user_id] = posts
    for post in posts:
        shortcode = post.get("shortcode")
        if shortcode:
            INSTAGRAM_SHORTCODE_CACHE[str(shortcode)] = post


def get_cached_instagram_post(user_id: int, shortcode: str) -> dict | None:
    for post in INSTAGRAM_SELECTIONS.get(user_id, []):
        if str(post.get("shortcode")) == shortcode:
            return post
    return INSTAGRAM_SHORTCODE_CACHE.get(shortcode)


async def send_instagram_post_item(msg: Message, post: dict) -> None:
    media = post.get("media") or []
    if not media:
        await msg.answer("Bu post ichida media topilmadi.", reply_markup=menyu)
        return

    caption = post.get("caption") or post.get("url") or "Instagram media"
    await send_instagram_media(msg, media, caption)


@dp.message(CommandStart())
async def salom_ber(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        f"Assalomu aleykum {msg.from_user.full_name}, WIKIPEDIA botimizga xush kelibsiz!",
        reply_markup=menyu,
    )


@dp.message(Command("cancel"))
async def cancel_handler(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Bekor qilindi. Menyudan bo'lim tanlang:", reply_markup=menyu)


@dp.message(F.text == "Wikipedia 📝")
async def wiki_handler(msg: Message, state: FSMContext):
    await msg.reply("Wikipedia bo'limiga kirdingiz, savol bering:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotStates.wiki_savol)


@dp.message(StateFilter(BotStates.wiki_savol))
async def wiki_savol_handler(msg: Message, state: FSMContext):
    savol = clean_text(msg.text)
    if not savol:
        await msg.answer("Savol matnini yuboring.")
        return

    try:
        javob = await asyncio.to_thread(fetch_wikipedia_summary, savol)
        await state.clear()
        await answer_long(msg, javob, reply_markup=menyu)
    except Exception as exc:
        logger.exception("Wikipedia error: %s", exc)
        await state.clear()
        await msg.answer("Kechirasiz, bu mavzu bo'yicha ma'lumot topilmadi.", reply_markup=menyu)


@dp.message(F.text == "Harry Potter 📚")
async def harry_potter_handler(msg: Message, state: FSMContext):
    await msg.answer(
        "Harry Potter bo'limiga kirdingiz, qaysi qahramon haqida ma'lumot olishni xohlaysiz?",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(BotStates.harry_potter_qahramon)


@dp.message(StateFilter(BotStates.harry_potter_qahramon))
async def harry_potter_qahramon_handler(msg: Message, state: FSMContext):
    qahramon = clean_text(msg.text)
    if not qahramon:
        await msg.answer("Qahramon ismini yuboring.")
        return

    try:
        hero = await asyncio.to_thread(fetch_harry_potter_character, qahramon)
    except Exception as exc:
        logger.exception("Harry Potter API error: %s", exc)
        await state.clear()
        await msg.answer("Hozir ma'lumot olishda xatolik bo'ldi.", reply_markup=menyu)
        return

    await state.clear()
    if hero is None:
        await msg.answer("Kechirasiz, bu qahramon topilmadi.", reply_markup=menyu)
        return

    name = hero.get("name") or "Noma'lum"
    house = hero.get("house") or "Noma'lum"
    actor = hero.get("actor") or "Noma'lum"
    year = hero.get("yearOfBirth") or "Noma'lum"
    caption = f"Ism: {name}\nUy: {house}\nHayotdagi ismi: {actor}\nTug'ilgan yili: {year}"
    image = hero.get("image")
    if image:
        await msg.answer_photo(photo=image, caption=caption, reply_markup=menyu)
    else:
        await msg.answer(caption, reply_markup=menyu)


@dp.message(F.text == "Valyuta 💰")
async def valyuta_handler(msg: Message):
    try:
        text = await asyncio.to_thread(fetch_currency_rates)
        await msg.answer(text, reply_markup=menyu)
    except Exception as exc:
        logger.exception("Currency API error: %s", exc)
        await msg.answer("Valyuta ma'lumotlarini olib bo'lmadi.", reply_markup=menyu)


@dp.message(F.text == "Instagram 📸")
async def instagram_handler(msg: Message, state: FSMContext):
    await msg.answer(
        "Instagram bo'limiga kirdingiz! @username yoki post/reel link yuboring:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(BotStates.instagram)


@dp.callback_query(F.data.startswith("ig:"))
async def instagram_inline_handler(call: CallbackQuery, state: FSMContext):
    try:
        _, owner_id_text, index_text = (call.data or "").split(":")
        owner_id = int(owner_id_text)
        index = int(index_text) - 1
    except ValueError:
        await call.answer("Tugma ma'lumoti noto'g'ri.", show_alert=True)
        return

    if call.from_user.id != owner_id:
        await call.answer("Bu tugma siz uchun emas.", show_alert=True)
        return

    posts = INSTAGRAM_SELECTIONS.get(owner_id) or []
    if index < 0 or index >= len(posts):
        await call.answer("Bu ro'yxat eskirgan. Username'ni qayta yuboring.", show_alert=True)
        return

    if call.message is None:
        await call.answer("Xabar topilmadi.", show_alert=True)
        return

    await call.answer("Yuborilmoqda...")
    try:
        await state.clear()
        await send_instagram_post_item(call.message, posts[index])
    except Exception as exc:
        logger.exception("Instagram inline send error: %s", exc)
        await call.message.answer("Media yuborishda xatolik bo'ldi.", reply_markup=menyu)


@dp.message(StateFilter(BotStates.instagram))
async def instagram_profile_handler(msg: Message, state: FSMContext):
    try:
        kind, value = normalize_instagram_input(msg.text)
    except ValueError as exc:
        await msg.answer(str(exc))
        return

    try:
        if kind == "profile":
            try:
                _, posts = await asyncio.to_thread(fetch_instagram_posts_from_rapidapi, value)
                cache_instagram_posts(msg.from_user.id, posts)
                await msg.answer(
                    instagram_posts_list_text(value, posts),
                    reply_markup=build_instagram_keyboard(msg.from_user.id, posts),
                )
                return
            except Exception as exc:
                logger.warning("Instagram RapidAPI fallback failed: %s", exc)
                text, photo_url = await asyncio.to_thread(fetch_instagram_profile, value)
                await state.clear()
                if photo_url:
                    await msg.answer_photo(photo=photo_url, caption=text, reply_markup=menyu)
                else:
                    await msg.answer(text, reply_markup=menyu)
                return

        cached_post = get_cached_instagram_post(msg.from_user.id, value)
        if cached_post:
            await state.clear()
            await send_instagram_post_item(msg, cached_post)
            return

        try:
            caption, media = await asyncio.to_thread(fetch_instagram_post, value)
        except Exception as exc:
            logger.warning("Instagram URL direct fetch failed: %s", exc)
            await state.clear()
            await msg.answer(
                "Bu URLni to'g'ridan-to'g'ri olishda xatolik bo'ldi. Avval username yuboring, "
                "ro'yxatdan kerakli raqamni tanlasangiz media yuboriladi.",
                reply_markup=menyu,
            )
            return

        await state.clear()
        if not media:
            await msg.answer("Bu linkdan media topilmadi.", reply_markup=menyu)
            return

        await send_instagram_media(msg, media, caption)
    except Exception as exc:
        logger.exception("Instagram error: %s", exc)
        await state.clear()
        await msg.answer(
            "Instagram ma'lumotini olib bo'lmadi. Profil public bo'lishi yoki link to'g'ri bo'lishi kerak.",
            reply_markup=menyu,
        )


@dp.message(F.text == "Youtube 🎥")
async def youtube_handler(msg: Message, state: FSMContext):
    await msg.answer("Youtube bo'limiga kirdingiz! link yuboring:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotStates.youtube)


@dp.message(StateFilter(BotStates.youtube))
async def youtube_link_handler(msg: Message, state: FSMContext):
    try:
        link = normalize_youtube_url(msg.text)
    except ValueError as exc:
        await msg.answer(str(exc))
        return

    await msg.answer("Video yuklanmoqda, biroz kuting...")
    try:
        file_path, title = await asyncio.to_thread(download_youtube_video, link)
        size = file_path.stat().st_size
        await state.clear()

        if size > TELEGRAM_SAFE_VIDEO_SIZE:
            await msg.answer(
                "Video juda katta. Telegram bot orqali 50 MB gacha video yuborish ishonchli ishlaydi.",
                reply_markup=menyu,
            )
            return

        await msg.answer_video(video=FSInputFile(file_path), caption=title, reply_markup=menyu)
    except Exception as exc:
        logger.exception("YouTube error: %s", exc)
        await state.clear()
        await msg.answer("YouTube videosini yuklab bo'lmadi. Linkni tekshirib qayta yuboring.", reply_markup=menyu)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
