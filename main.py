import os
import re
import json
import asyncio
import logging
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

MAX_BODY_LEN = int(os.getenv("MAX_BODY_LEN", "500"))
PUSH_SELF_MESSAGES = os.getenv("PUSH_SELF_MESSAGES", "false").lower() == "true"

STATE_FILE = os.getenv("STATE_FILE", "state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"push_enabled": True}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning("读取状态文件失败，使用默认开启状态: %s", e)
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


def shorten(text: str, limit: int = MAX_BODY_LEN) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


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


def message_summary(event) -> str:
    msg = event.message

    if event.raw_text:
        return shorten(event.raw_text)

    if msg.photo:
        return "[图片]"
    if msg.video:
        return "[视频]"
    if msg.voice:
        return "[语音]"
    if msg.audio:
        return "[音频]"
    if msg.document:
        return "[文件]"
    if msg.sticker:
        return "[贴纸]"

    return "[非文本消息]"


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

        me = await client.get_me()
        return reply_msg.sender_id == me.id
    except Exception as e:
        logging.warning("检查回复消息失败: %s", e)
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
    except Exception as e:
        logging.warning("检查对话静音状态失败: %s", e)
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

    me = await client.get_me()

    if event.chat_id != me.id:
        return False

    text = (event.raw_text or "").strip().lower()

    if text == "/on":
        set_push_enabled(True)
        await event.reply("✅ Bark 推送已开启")
        logging.info("收到 Saved Messages 命令：开启推送")
        return True

    if text == "/off":
        set_push_enabled(False)
        await event.reply("🔕 Bark 推送已关闭")
        logging.info("收到 Saved Messages 命令：关闭推送")
        return True

    if text == "/status":
        status = "开启 ✅" if is_push_enabled() else "关闭 🔕"
        await event.reply(f"当前 Bark 推送状态：{status}")
        logging.info("收到 Saved Messages 命令：查看状态")
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
        logging.info("收到 Saved Messages 命令：查看帮助")
        return True

    return False


async def push_bark(title: str, body: str, url: Optional[str] = None) -> bool:
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
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload, timeout=10) as resp:
                    resp_text = await resp.text()
                    if resp.status == 200:
                        logging.info("Bark推送成功: %s", title)
                        return True

                    logging.warning(
                        "Bark推送失败，第%s次，HTTP=%s，返回=%s",
                        i + 1,
                        resp.status,
                        resp_text,
                    )
        except Exception as e:
            logging.warning("Bark请求异常，第%s次: %s", i + 1, e)

        await asyncio.sleep(2)

    return False


@client.on(events.NewMessage)
async def on_new_message(event):
    try:
        if await handle_saved_messages_command(event):
            return

        if not is_push_enabled():
            logging.debug("Bark推送当前已关闭，跳过")
            return

        ok, reason = await should_push(event)
        if not ok:
            logging.debug("跳过消息: %s", reason)
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

        logging.info("准备推送: %s | %s", title, body)
        await push_bark(title, body, "tg://")

    except Exception as e:
        logging.exception("处理消息失败: %s", e)


async def main():
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("请先在 .env 里配置 TG_API_ID 和 TG_API_HASH")

    if not BARK_KEY:
        raise RuntimeError("请先在 .env 里配置 BARK_KEY")

    logging.info("启动 Telegram -> Bark 监听程序")
    logging.info("当前 Bark 推送状态: %s", "开启" if is_push_enabled() else "关闭")

    await client.start()

    me = await client.get_me()
    logging.info(
        "已登录 Telegram: id=%s username=%s",
        me.id,
        getattr(me, "username", None),
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
