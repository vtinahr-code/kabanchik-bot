# Kabanchik → Telegram для Railway

Цей сервіс перевіряє Kabanchik щохвилини та надсилає нові бухгалтерські замовлення в Telegram.

## Змінні Railway

Додайте в Railway → Service → Variables:

- `TELEGRAM_BOT_TOKEN` — новий токен від BotFather.
- `TELEGRAM_CHAT_ID` — ваш числовий chat ID.
- `POLL_SECONDS` — `60`.
- `STATE_FILE` — `/data/seen.json`.
- `TARGET_URLS` — необов'язково.
- `KEYWORDS` — необов'язково.

## Як дізнатися CHAT_ID

1. Відкрийте свого бота в Telegram і натисніть Start.
2. У браузері відкрийте:
   `https://api.telegram.org/botНОВИЙ_ТОКЕН/getUpdates`
3. Знайдіть `"chat":{"id":123456789...}`.
4. Число після `id` — це `TELEGRAM_CHAT_ID`.

## Railway Volume

Щоб бот не забував уже знайдені оголошення після перезапуску:

1. У Railway відкрийте проєкт.
2. Натисніть `⌘K` → Create Volume.
3. Підключіть Volume до сервісу.
4. Mount path: `/data`.

## Безпека

Токен, який був надісланий у чат, потрібно відкликати через BotFather:
`/revoke`, потім `/token`.

Новий токен не публікуйте і не надсилайте в чат.
