import os
import asyncio
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions
from telethon.tl.types import User, Chat, Channel, InputNotifyPeer


load_dotenv("env")

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "tg_bark")

BARK_KEY = os.getenv("BARK_KEY", "")
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")

PUSH_SELF_MESSAGES = os.getenv("PUSH_SELF_MESSAGES", "false").lower() == "true"

client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
http_session: Optional[aiohttp.ClientSession] = None


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
    except Exception as e:
        print(f"[WARN] is_chat_muted 检查失败，按“未静音”处理: {e}")
        return False


async def should_push(event) -> tuple[bool, str]:
    if event.out and not PUSH_SELF_MESSAGES:
        return False, "忽略自己发出的消息"

    # 完全跟随 Telegram 客户端的通知设置：
    # 只要这个对话没有被静音，就推送（无论私聊/群/频道）；
    # 静音了就不推送。
    if await is_chat_muted(event):
        return False, "对话已静音"

    if event.is_private:
        return True, "私聊"

    return True, "群消息"


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
        pushed = await push_bark(title, body, tg_link)
        if not pushed:
            print(f"[WARN] Bark 推送失败: chat_id={event.chat_id}, reason={reason}")

    except Exception as e:
        print(f"[ERROR] 处理消息时出错: chat_id={getattr(event, 'chat_id', None)}, err={e}")


async def main():
    global http_session

    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("请先在 env 里配置 TG_API_ID 和 TG_API_HASH")

    if not BARK_KEY:
        raise RuntimeError("请先在 env 里配置 BARK_KEY")

    http_session = aiohttp.ClientSession()

    try:
        await client.start()
        me = await client.get_me()
        print(f"[INFO] 已登录：{display_name(me)} (id={me.id})")

        await client.run_until_disconnected()
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
