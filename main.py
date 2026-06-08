import os
import re
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.tl.types import User, Chat, Channel, InputNotifyPeer


load_dotenv()

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "tg_bark")

BARK_KEY = os.getenv("BARK_KEY", "")
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")

MY_USERNAME = os.getenv("MY_USERNAME", "").lstrip("@").lower()

PUSH_SELF_MESSAGES = os.getenv("PUSH_SELF_MESSAGES", "false").lower() == "true"

STATE_FILE = os.getenv("STATE_FILE", "state.json")

client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
http_session: Optional[aiohttp.ClientSession] = None
_me_cache: Optional[User] = None


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"push_enabled": True}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"push_enabled": True}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_push_enabled() -> bool:
    state = load_state()
    return bool(state.get("push_enabled", True))


def set_push_enabled(enabled: bool):
    state = load_state()
    state["push_enabled"] = enabled
    save_state(state)


def display_name(entity) -> str:
    if entity is None:
        return "未知"

    if isinstance(entity, User):
        name = " ".join(
            part for part in [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ] if part
        ).strip()
        return name or getattr(entity, "username", None) or str(entity.id)

    if isinstance(entity, (Chat, Channel)):
        return getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)

    return getattr(entity, "title", None) or getattr(entity, "username", None) or "未知"



def has_username_mention(text: str) -> bool:
    if not MY_USERNAME or not text:
        return False

    pattern = rf"(?<!\w)@{re.escape(MY_USERNAME)}(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


async def is_reply_to_me(event) -> bool:
    if not event.is_reply:
        return False

    try:
        reply_msg = await event.get_reply_message()
        if not reply_msg:
            return False

        return reply_msg.sender_id == _me_cache.id
    except Exception:
        return False


async def is_chat_muted(event) -> bool:
    """检查当前对话在 Telegram 客户端中是否被静音。"""
    try:
        input_peer = await event.get_input_chat()
        result = await client(
            functions.account.GetNotifySettingsRequest(
                peer=InputNotifyPeer(peer=input_peer)
            )
        )
        mute_until = getattr(result, "mute_until", None)
        if mute_until is None:
            return False
        # mute_until 为 datetime，如果在未来则表示已静音
        # Telegram 永久静音时 mute_until 通常设为一个很远的未来时间 (2147483647)
        if isinstance(mute_until, int):
            return mute_until > int(datetime.now(timezone.utc).timestamp())
        if isinstance(mute_until, datetime):
            now = datetime.now(timezone.utc)
            if mute_until.tzinfo is None:
                mute_until = mute_until.replace(tzinfo=timezone.utc)
            return mute_until > now
        return False
    except Exception:
        return False


async def should_push(event) -> tuple[bool, str]:
    if event.out and not PUSH_SELF_MESSAGES:
        return False, "忽略自己发出的消息"

    # 检查对话是否在 Telegram 中被静音
    if await is_chat_muted(event):
        return False, "对话已静音"

    if event.is_private:
        return True, "私聊"

    text = event.raw_text or ""

    if event.message.mentioned:
        return True, "被@"

    if has_username_mention(text):
        return True, "用户名@"

    if await is_reply_to_me(event):
        return True, "回复你"

    return False, "非私聊且未@你"


def generate_tg_link(event, chat, sender) -> str:
    """根据消息事件生成最精准的 Telegram 跳转 Scheme 链接。"""
    try:
        # 1. 私聊情况
        if event.is_private:
            if isinstance(sender, User) and getattr(sender, "username", None):
                return f"tg://resolve?domain={sender.username}"
            if sender:
                return f"tg://openmessage?user_id={sender.id}"
            return "tg://"

        # 2. 群组或频道情况
        # 优先使用公开的 username，可以精准定位到具体消息
        if chat and getattr(chat, "username", None):
            return f"tg://resolve?domain={chat.username}&post={event.message.id}"

        # 针对无 username 的私有群组/频道
        if chat:
            # Telethon 中超级群和频道都是 Channel 类型
            if isinstance(chat, Channel):
                return f"tg://privatepost?channel={chat.id}&post={event.message.id}"
            # 普通小群是 Chat 类型
            if isinstance(chat, Chat):
                return f"tg://openmessage?chat_id={chat.id}"
            return f"tg://openmessage?chat_id={chat.id}"

    except Exception:
        pass

    return "tg://"


async def handle_saved_messages_command(event) -> bool:
    """
    只处理 Telegram 收藏夹 / Saved Messages 里的命令。
    支持：
    /on
    /off
    /status
    /help
    """

    if not event.out:
        return False

    if not event.is_private:
        return False

    if event.chat_id != _me_cache.id:
        return False

    text = (event.raw_text or "").strip().lower()

    if text == "/on":
        set_push_enabled(True)
        await event.reply("✅ Bark 推送已开启")
        return True

    if text == "/off":
        set_push_enabled(False)
        await event.reply("🔕 Bark 推送已关闭")
        return True

    if text == "/status":
        status = "开启 ✅" if is_push_enabled() else "关闭 🔕"
        await event.reply(f"当前 Bark 推送状态：{status}")
        return True

    if text == "/help":
        await event.reply(
            "Telegram Bark 控制命令：\n\n"
            "/on 开启推送\n"
            "/off 关闭推送\n"
            "/status 查看当前状态\n"
            "/help 查看帮助\n\n"
            "说明：这些命令只在 Telegram 收藏夹 / Saved Messages 里生效。"
        )
        return True

    return False


async def push_bark(title: str, body: str, url: Optional[str] = None) -> bool:
    global http_session
    api_url = f"{BARK_SERVER}/{BARK_KEY}"

    payload = {
        "title": title,
        "body": body,
        "group": "Telegram",
        "sound": "healthnotification",
        "icon": "https://telegram.org/img/t_logo.png",
    }

    if url:
        payload["url"] = url

    for i in range(3):
        try:
            if http_session is None or http_session.closed:
                http_session = aiohttp.ClientSession()
            async with http_session.post(api_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

        await asyncio.sleep(2)

    return False


@client.on(events.NewMessage)
async def on_new_message(event):
    try:
        if await handle_saved_messages_command(event):
            return

        if not is_push_enabled():
            return

        ok, reason = await should_push(event)
        if not ok:
            return

        chat = await event.get_chat()
        sender = await event.get_sender()

        chat_name = display_name(chat)
        sender_name = display_name(sender)

        content = "点击查看"

        title = f"Telegram｜{reason}"

        if event.is_private:
            body = f"{sender_name}: {content}"
        else:
            body = f"{chat_name}\n{sender_name}: {content}"

        tg_link = generate_tg_link(event, chat, sender)
        await push_bark(title, body, tg_link)

    except Exception:
        pass


async def main():
    global http_session

    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("请先在 .env 里配置 TG_API_ID 和 TG_API_HASH")

    if not BARK_KEY:
        raise RuntimeError("请先在 .env 里配置 BARK_KEY")

    http_session = aiohttp.ClientSession()

    try:
        await client.start()

        global _me_cache
        _me_cache = await client.get_me()

        await client.run_until_disconnected()
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
