import re
import os
import asyncio
import logging
import aiofiles
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    PeerIdInvalid,
    InviteRequestSent
)
from urllib.parse import urlparse
from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ADMIN_LIMIT,
    ADMIN_IDS,
    DEFAULT_LIMIT
) # @Mod_By_Kamal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client(
    "app_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=1000
)

user = Client(
    "session_923294013197",
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number="+923294",
    workers=1000
) # @Mod_By_Kamal

START_MESSAGE = """
『⭐️ 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃 𝐒𝐂𝐑𝐀𝐏𝐄𝐑 ⭐️』

⌾ 𝐒𝐂𝐑𝐀𝐏𝐄 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃𝐒 from Telegram channels

⚡️ 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒:
▬▬▬▬▬▬▬▬▬▬▬▬▬
/scr - Scrape from single channel 📺
/mc - Scrape from multiple channels 📡
/clean - Format card data from file 🧹

<strong>𝐄𝐗𝐀𝐌𝐏𝐋𝐄𝐒:</strong>
❯ /scr @Mod_By_Kamal 100
❯ /scr @Mod_By_Kamal 100 515462
❯ /scr @Mod_By_Kamal 100 BankName
❯ /scr t.me/Mod_By_Kamal 100
❯ /scr https://t.me/+ZBqGFP5RpmY2Y1 100

【✯ 𝐇𝐀𝐏𝐏𝐘 𝐒𝐂𝐑𝐀𝐏𝐈𝐍𝐆! ✯】
""" # @Mod_By_Kamal

async def scrape_messages(client, channel_username, limit, start_number=None, bank_name=None):
    messages = []
    count = 0
    pattern = r'\d{16}\D*\d{2}\D*\d{2,4}\D*\d{3,4}'
    bin_pattern = re.compile(r'^\d{6}') if start_number else None

    logger.info(f"Starting to scrape messages from {channel_username} with limit {limit}")

    async for message in user.search_messages(channel_username):
        if count >= limit:
            break
        text = message.text or message.caption
        if text:
            if bank_name and bank_name.lower() not in text.lower():
                continue
            matched_messages = re.findall(pattern, text) # @Mod_By_Kamal
            if matched_messages:
                formatted_messages = []
                for matched_message in matched_messages:
                    extracted_values = re.findall(r'\d+', matched_message)
                    if len(extracted_values) == 4:
                        card_number, mo, year, cvv = extracted_values
                        year = year[-2:]
                        if start_number:
                            if card_number.startswith(start_number[:6]):
                                formatted_messages.append(f"{card_number}|{mo}|{year}|{cvv}")
                        else:
                            formatted_messages.append(f"{card_number}|{mo}|{year}|{cvv}")
                messages.extend(formatted_messages)
                count += len(formatted_messages)
    logger.info(f"Scraped {len(messages)} messages from {channel_username}")
    return messages[:limit] # @Mod_By_Kamal

def remove_duplicates(messages):
    unique_messages = list(set(messages))
    duplicates_removed = len(messages) - len(unique_messages)
    logger.info(f"Removed {duplicates_removed} duplicates")
    return unique_messages, duplicates_removed

async def send_results(client, message, unique_messages, duplicates_removed, source_name, bin_filter=None, bank_filter=None):
    if unique_messages:
        try:
            file_name = f"x{len(unique_messages)}_{source_name.replace(' ', '_')}.txt"
            async with aiofiles.open(file_name, mode='w') as f:
                await f.write("\n".join(unique_messages)) # @Mod_By_Kamal)

            user_link = await get_user_link(message)
            caption = (
                f"『⭐️ 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃 𝐒𝐂𝐑𝐀𝐏𝐄𝐑 ⭐️』\n\n"
                f"✅ <b>𝐒𝐂𝐑𝐀𝐏𝐏𝐄𝐷 𝐒𝐔𝐂𝐂𝐄𝐒𝐒𝐅𝐔𝐿𝐿𝐘</b>\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"🌐 <b>𝐒𝐎𝐔𝐑𝐂𝐄:</b> <code>{source_name}</code>\n"
                f"📝 <b>𝐀𝐌𝐎𝐔𝐍𝐓:</b> <code>{len(unique_messages)}</code>\n"
                f"🗑️ <b>𝐃𝐔𝐏𝐋𝐈𝐂𝐀𝐓𝐄𝐒 𝐑𝐄𝐌𝐎𝐕𝐄𝐃:</b> <code>{duplicates_removed}</code>\n"
            )
            if bin_filter:
                caption += f"🔍 <b>𝐁𝐈𝐍 𝐅𝐈𝐋𝐓𝐄𝐑:</b> <code>{bin_filter}</code>\n" # @Mod_By_Kamal
            if bank_filter:
                caption += f"🏦 <b>𝐁𝐀𝐍𝐊 𝐅𝐈𝐋𝐓𝐄𝐑:</b> <code>{bank_filter}</code>\n"
            caption += (
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"👤 <b>𝐒𝐂𝐑𝐀𝐏𝐏𝐸𝐷 𝐁𝐘:</b> {user_link}\n"
            )

            chat_id = message.chat.id
            reply_id = getattr(message, 'replied_to_message_id', None)

            await message.delete()

            if reply_id:
                await client.send_document(
                    chat_id,
                    document=file_name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_id
                )
            else:
                await client.send_document(
                    chat_id,
                    document=file_name,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                ) # @Mod_By_Kamal

            logger.info(f"Results sent successfully for {source_name}")
        except Exception as e:
            logger.error(f"Error sending results: {e}")
            try:
                await client.send_message(
                    message.chat.id,
                    f"<b>❌ 𝐄𝐑𝐑𝐎𝐑 𝐒𝐄𝐍𝐷𝐈𝐍𝐆 𝐅𝐈𝐋𝐄:</b> <code>{str(e)}</code>",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        finally:
            try:
                if os.path.exists(file_name):
                    os.remove(file_name) # @Mod_By_Kamal
            except Exception as e:
                logger.error(f"Error removing file: {e}")
    else:
        try:
            await message.delete()
            await client.send_message(
                message.chat.id,
                "<b>❌ 𝐍𝐎 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃𝐒 𝐅𝐎𝐔𝐍𝐃</b>",
                reply_to_message_id=getattr(message, 'replied_to_message_id', None),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error sending no results message: {e}") # @Mod_By_Kamal

async def get_user_link(message):
    if message.from_user is None:
        return '<a href="https://t.me/ScrapperxCleanerBot">Scrapper x Cleaner</a>'
    else:
        user_first_name = message.from_user.first_name
        user_last_name = message.from_user.last_name or ""
        user_full_name = f"{user_first_name} {user_last_name}".strip()
        return f'<a href="tg://user?id={message.from_user.id}">{user_full_name}</a>'

async def join_private_chat(client, invite_link):
    try:
        await client.join_chat(invite_link)
        logger.info(f"Joined chat via invite link: {invite_link}")
        return True
    except UserAlreadyParticipant:
        logger.info(f"Already a participant in the chat: {invite_link}")
        return True # @Mod_By_Kamal
    except InviteRequestSent:
        logger.info(f"Join request sent to the chat: {invite_link}")
        return False
    except (InviteHashExpired, InviteHashInvalid) as e:
        logger.error(f"Failed to join chat {invite_link}: {e}")
        return False

async def send_join_request(client, invite_link, message):
    try:
        await client.join_chat(invite_link)
        logger.info(f"Sent join request to chat: {invite_link}")
        return True
    except PeerIdInvalid as e:
        logger.error(f"Failed to send join request to chat {invite_link}: {e}")
        return False
    except InviteRequestSent:
        logger.info(f"Join request sent to the chat: {invite_link}")
        await message.edit_text("<b>𝐇𝐞𝐲 𝐁𝐫𝐨 𝐈 𝐇𝐚𝐯𝐞 𝐒𝐞𝐧𝐭 𝐉𝐨𝐢𝐧 𝐑𝐞𝐪𝐮𝐞𝐬𝐭✅</b>") # @Mod_By_Kamal
        return False

def setup_scr_handler(app):
    @app.on_message(filters.command(["scr", "ccscr", "scrcc"], prefixes=["/", ".", ",", "!"]) & (filters.group | filters.private))
    async def scr_cmd(client, message):
        args = message.text.split()[1:]
        user_id = message.from_user.id if message.from_user else None

        if len(args) < 2:
            await client.send_message(message.chat.id, "<b>⚠️ 𝐏𝐫𝐨𝐯𝐢𝐝𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐚𝐧𝐝 𝐚𝐦𝐨𝐮𝐧𝐭 𝐭𝐨 𝐬𝐜𝐫𝐚𝐩𝐞 ❌</b>", reply_to_message_id=message.id)
            logger.warning("Invalid command: Missing arguments")
            return # @Mod_By_Kamal

        channel_identifier = args[0]
        chat = None
        channel_name = ""
        channel_username = ""

        if channel_identifier.lstrip("-").isdigit():
            chat_id = int(channel_identifier)
            try:
                chat = await user.get_chat(chat_id)
                channel_name = chat.title
                logger.info(f"Scraping from private channel: {channel_name} (ID: {chat_id})")
            except Exception as e:
                await client.send_message(message.chat.id, "<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐯𝐚𝐥𝐢𝐝 𝐜𝐡𝐚𝐭 𝐈𝐃 ❌</b>")
                logger.error(f"Failed to fetch private channel: {e}")
                return
        else:
            if channel_identifier.startswith("https://t.me/+"):
                invite_link = channel_identifier
                temporary_msg = await client.send_message(message.chat.id, "<b>𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐔𝐬𝐞𝐫𝐧𝐚𝐦𝐞...</b>", reply_to_message_id=message.id)
                joined = await join_private_chat(user, invite_link)
                if not joined:
                    request_sent = await send_join_request(user, invite_link, temporary_msg)
                    if not request_sent:
                        return
                else:
                    await temporary_msg.delete()
                    chat = await user.get_chat(invite_link)
                    channel_name = chat.title
                    logger.info(f"Joined private channel via link: {channel_name}")
            elif channel_identifier.startswith("https://t.me/"):
                channel_username = channel_identifier[13:]
            elif channel_identifier.startswith("t.me/"):
                channel_username = channel_identifier[5:]
            else:
                channel_username = channel_identifier # @Mod_By_Kamal

            if not chat:
                try:
                    chat = await user.get_chat(channel_username)
                    channel_name = chat.title
                    logger.info(f"Scraping from public channel: {channel_name} (Username: {channel_username})")
                except Exception as e:
                    await client.send_message(message.chat.id, "<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐜𝐨𝐫𝐫𝐞𝐜𝐭 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐨𝐫 𝐜𝐡𝐚𝐭 𝐈𝐃 ❌</b>")
                    logger.error(f"Failed to fetch public channel: {e}")
                    return

        try:
            limit = int(args[1])
            logger.info(f"Scraping limit set to: {limit}")
        except ValueError:
            await client.send_message(message.chat.id, "<b>⚠️ 𝐈𝐧𝐯𝐚𝐥𝐢𝐝 𝐥𝐢𝐦𝐢𝐭 𝐯𝐚𝐥𝐮𝐞. 𝐏𝐥𝐞𝐚𝐬𝐞 𝐩𝐫𝐨𝐯𝐢𝐝𝐞 𝐚 𝐯𝐚𝐥𝐢𝐝 𝐧𝐮𝐦𝐛𝐞𝐫 ❌</b>")
            logger.warning("Invalid limit value provided")
            return # @Mod_By_Kamal

        start_number = None
        bank_name = None
        bin_filter = None
        if len(args) > 2:
            if args[2].isdigit():
                start_number = args[2]
                bin_filter = args[2][:6]
                logger.info(f"BIN filter applied: {bin_filter}")
            else:
                bank_name = " ".join(args[2:])
                logger.info(f"Bank filter applied: {bank_name}")

        max_lim = ADMIN_LIMIT if user_id in ADMIN_IDS else DEFAULT_LIMIT
        if limit > max_lim:
            await client.send_message(message.chat.id, f"<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨! 𝐀𝐦𝐨𝐮𝐧𝐭 𝐨𝐯𝐞𝐫 𝐌𝐚𝐱 𝐥𝐢𝐦𝐢𝐭 𝐢𝐬 {max_lim} ❌</b>")
            logger.warning(f"Limit exceeded: {limit} > {max_lim}")
            return # @Mod_By_Kamal

        temporary_msg = await client.send_message(message.chat.id, "<b>𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐓𝐡𝐞 𝐔𝐬𝐞𝐫𝐧𝐚𝐦𝐞...</b>", reply_to_message_id=message.id)
        await asyncio.sleep(1.5)

        await temporary_msg.edit_text("<b>𝐒𝐜𝐫𝐚𝐩𝐢𝐧𝐠 𝐈𝐧 𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬</b>")

        temporary_msg.replied_to_message_id = message.id

        scrapped_results = await scrape_messages(user, chat.id, limit, start_number=start_number, bank_name=bank_name)
        unique_messages, duplicates_removed = remove_duplicates(scrapped_results) # @Mod_By_Kamal

        if not unique_messages:
            await temporary_msg.delete()
            await client.send_message(message.chat.id, "<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨 ❌ 𝐍𝐨 𝐂𝐫𝐞𝐝𝐢𝐭 𝐂𝐚𝐫𝐝 𝐅𝐨𝐮𝐧𝐝</b>", reply_to_message_id=message.id)
        else:
            await send_results(client, temporary_msg, unique_messages, duplicates_removed, channel_name, bin_filter=bin_filter, bank_filter=bank_name)

    @app.on_message(filters.command(["mc", "multiscr", "mscr"], prefixes=["/", ".", ",", "!"]) & (filters.group | filters.private))
    async def mc_cmd(client, message):
        args = message.text.split()[1:]
        if len(args) < 2:
            await client.send_message(message.chat.id, "<b>⚠️ 𝐏𝐫𝐨𝐯𝐢𝐝𝐞 𝐚𝐭 𝐥𝐞𝐚𝐬𝐭 𝐨𝐧𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞</b>", reply_to_message_id=message.id)
            logger.warning("Invalid command: Missing arguments")
            return # @Mod_By_Kamal

        channel_identifiers = args[:-1]
        limit = int(args[-1])
        user_id = message.from_user.id if message.from_user else None

        max_lim = ADMIN_LIMIT if user_id in ADMIN_IDS else DEFAULT_LIMIT
        if limit > max_lim:
            await client.send_message(message.chat.id, f"<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨! 𝐀𝐦𝐨𝐮𝐧𝐭 𝐨𝐯𝐞𝐫 𝐌𝐚𝐱 𝐥𝐢𝐦𝐢𝐭 𝐢𝐬 {max_lim} ❌</b>")
            logger.warning(f"Limit exceeded: {limit} > {max_lim}")
            return

        temporary_msg = await client.send_message(message.chat.id, "<b>𝐒𝐜𝐫𝐚𝐩𝐢𝐧𝐠 𝐈𝐧 𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬</b>", reply_to_message_id=message.id) # @Mod_By_Kamal

        temporary_msg.replied_to_message_id = message.id

        all_messages = []
        tasks = []

        for channel_identifier in channel_identifiers:
            parsed_url = urlparse(channel_identifier)
            channel_username = parsed_url.path.lstrip('/') if not parsed_url.scheme else channel_identifier

            tasks.append(scrape_messages_task(user, channel_username, limit, client, message))

        results = await asyncio.gather(*tasks)
        for result in results:
            all_messages.extend(result) # @Mod_By_Kamal

        unique_messages, duplicates_removed = remove_duplicates(all_messages)
        unique_messages = unique_messages[:limit]

        if not unique_messages:
            await temporary_msg.delete()
            await client.send_message(message.chat.id, "<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨 ❌ 𝐍𝐨 𝐂𝐫𝐞𝐝𝐢𝐭 𝐂𝐚𝐫𝐝 𝐅𝐨𝐮𝐧𝐝</b>", reply_to_message_id=message.id)
        else:
            await send_results(client, temporary_msg, unique_messages, duplicates_removed, "Multiple Chats")

async def scrape_messages_task(client, channel_username, limit, bot_client, message):
    try:
        chat = None
        if channel_username.startswith("https://t.me/+"):
            invite_link = channel_username
            temporary_msg = await bot_client.send_message(message.chat.id, "<b>𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐔𝐬𝐞𝐫𝐧𝐚𝐦𝐞...</b>", reply_to_message_id=message.id)
            joined = await join_private_chat(client, invite_link)
            if not joined:
                request_sent = await send_join_request(client, invite_link, temporary_msg)
                if not request_sent:
                    return []
            else:
                await temporary_msg.delete()
                chat = await client.get_chat(invite_link)
        else:
            chat = await client.get_chat(channel_username) # @Mod_By_Kamal

        return await scrape_messages(client, chat.id, limit)
    except Exception as e:
        await bot_client.send_message(message.chat.id, f"<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐜𝐨𝐫𝐫𝐞𝐜𝐭 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐟𝐨𝐫 {channel_username} ❌</b>", reply_to_message_id=message.id)
        logger.error(f"Failed to scrape from {channel_username}: {e}")
        return []

@app.on_message(filters.command("start", prefixes=["/", ".", ",", "!"]) & (filters.group | filters.private))
async def start(client, message):
    buttons = [
        [InlineKeyboardButton("Update Channel", url="https://t.me/KamalxKiller"), InlineKeyboardButton("Dev👨‍💻", user_id=5248903529)]
    ]
    await client.send_message(
        message.chat.id,
        START_MESSAGE,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons),
        reply_to_message_id=message.id
    ) # @Mod_By_Kamal

def normalize_card(text):
    text = re.sub(r'[\n\r\|\/]', ' ', text)
    numbers = re.findall(r'\d+', text)

    cc = mm = yy = cvv = ''

    for num in numbers:
        if len(num) >= 13 and len(num) <= 19 and not cc:
            cc = num
        elif len(num) == 4 and num.startswith('20') and not yy:
            yy = num
        elif len(num) == 2 and int(num) <= 12 and not mm:
            mm = num
        elif len(num) == 2 and not yy:
            yy = '20' + num
        elif (len(num) == 3 or len(num) == 4) and not cvv:
            cvv = num # @Mod_By_Kamal

    if cc and mm and yy and cvv:
        if len(yy) == 2:
            yy = '20' + yy
        return f"{cc}|{mm}|{yy}|{cvv}"

    return None

@app.on_message(filters.command(["clean"], prefixes=["/", ".", ",", "!"]) & (filters.group | filters.private))
async def clean_cmd(client, message):
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply_text("<b>⚠️ 𝐏𝐥𝐞𝐚𝐬𝐞 𝐫𝐞𝐩𝐥𝐲 𝐭𝐨 𝐚 𝐟𝐢𝐥𝐞 𝐜𝐨𝐧𝐭𝐚𝐢𝐧𝐢𝐧𝐠 𝐜𝐫𝐞𝐝𝐢𝐭 𝐜𝐚𝐫𝐝 𝐝𝐚𝐭𝐚</b>")
        return # @Mod_By_Kamal

    document = message.reply_to_message.document

    if not document.file_name.endswith('.txt'):
        await message.reply_text("<b>⚠️ 𝐏𝐥𝐞𝐚𝐬𝐞 𝐩𝐫𝐨𝐯𝐢𝐝𝐞 𝐚 𝐭𝐞𝐱𝐭 (.𝐭𝐱𝐭) 𝐟𝐢𝐥𝐞</b>")
        return

    status_msg = await message.reply_text("<b>𝐏𝐫𝐨𝐜𝐞𝐬𝐬𝐢𝐧𝐠 𝐟𝐢𝐥𝐞...</b>") # @Mod_By_Kamal

    try:
        file_path = await client.download_media(document)

        async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = await f.read()

        lines = content.strip().split('\n')
        cleaned_cards = []

        for line in lines:
            if line.strip():
                normalized = normalize_card(line)
                if normalized:
                    cleaned_cards.append(normalized) # @Mod_By_Kamal

        unique_cards, duplicates_removed = remove_duplicates(cleaned_cards)

        if unique_cards:
            output_filename = f"cleaned_x{len(unique_cards)}.txt"
            async with aiofiles.open(output_filename, 'w') as f:
                await f.write('\n'.join(unique_cards))

            user_link = await get_user_link(message)
            caption = (
                f"『⭐️ 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃 𝐒𝐂𝐑𝐀𝐏𝐄𝐑 ⭐️』\n\n"
                f"✅ <b>𝐂𝐋𝐄𝐀𝐍𝐄𝐃 𝐒𝐔𝐂𝐂𝐄𝐒𝐒𝐅𝐔𝐿𝐿𝐘</b>\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"🌐 <b>𝐒𝐎𝐔𝐑𝐂𝐄:</b> <code>File Cleaning 🧹</code>\n"
                f"📝 <b>𝐀𝐌𝐎𝐔𝐍𝐓:</b> <code>{len(unique_cards)}</code>\n"
                f"🗑️ <b>𝐃𝐔𝐏𝐋𝐈𝐂𝐀𝐓𝐄𝐒 𝐑𝐄𝐌𝐎𝐕𝐄𝐃:</b> <code>{duplicates_removed}</code>\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"👤 <b>𝐂𝐋𝐄𝐀𝐍𝐄𝐃 𝐁𝐘:</b> {user_link}\n"
            ) # @Mod_By_Kamal

            await status_msg.delete()
            await client.send_document(
                message.chat.id,
                output_filename,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id
            )

            os.remove(file_path)
            os.remove(output_filename)

        else:
            await status_msg.delete()
            await client.send_message(
                message.chat.id,
                "<b>❌ 𝐍𝐎 𝐕𝐀𝐿𝐈𝐃 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃𝐒 𝐅𝐎𝐔𝐍𝐃</b>",
                reply_to_message_id=message.id
            )
            os.remove(file_path) # @Mod_By_Kamal

    except Exception as e:
        logger.error(f"Error in clean command: {e}")
        await status_msg.delete()
        await client.send_message(
            message.chat.id,
            f"<b>❌ 𝐄𝐑𝐑𝐎𝐑 𝐏𝐑𝐎𝐂𝐄𝐒𝐒𝐈𝐍𝐆 𝐅𝐈𝐋𝐄:</b> <code>{str(e)}</code>",
            reply_to_message_id=message.id
        )

if __name__ == "__main__":
    setup_scr_handler(app)
    user.start()
    app.run()
