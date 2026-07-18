import os

# aiogram validates the token while importing bot.publisher. Tests never make
# Telegram requests, but need a syntactically valid placeholder.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_IMPORT_ONLY")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test_channel")
os.environ.setdefault("MODERATOR_CHAT_ID", "1")
