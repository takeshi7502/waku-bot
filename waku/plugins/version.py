import pyrogram

from waku import database
from waku.config import app_config
from waku.version import runtime_info_text


@pyrogram.Client.on_message(pyrogram.filters.command("version"), group=0)
async def version_command(client: pyrogram.Client, message: pyrogram.types.Message):
    user = message.from_user
    if user is None:
        return
    db_user = await database.get_user_by_id(user.id)
    if db_user is None:
        return
    if not db_user.is_bot_global_admin and user.id not in app_config.owners:
        await message.reply_text("Permission denied")
        return
    await message.reply_text(runtime_info_text())
