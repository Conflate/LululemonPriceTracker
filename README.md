# lululemon Price Tracker

A Discord-accessible price tracker for lululemon product pages. Users can add product URLs from Discord, and the tracker checks those products on a schedule. When a product first appears on sale, drops further in price, or reaches a configured target price, the bot posts an alert to Discord.

The tracker uses a real Chrome browser session through Chrome DevTools Protocol (CDP). This is more reliable for lululemon than plain HTTP scraping or fully headless browser checks.

## Features

- Add products from Discord with `!track <url>`.
- Track current price, sale status, and optional target price.
- Alert when a product goes on sale or drops in price.
- List tracked products with readable current status.
- Pause, resume, remove, and manually check products from Discord.
- Store product configuration separately from runtime price state.

## Requirements

- Python 3.10+
- Google Chrome
- A Discord bot token
- A Discord channel for commands
- A Discord channel for alerts

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 pricetrack_updated.py init
```

## Discord Setup

Create a Discord bot in the Discord Developer Portal and enable **Message Content Intent** for the bot.

Create either a `.env` file or `env/token.env` with:

```text
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_CHANNELSEND_ID=123456789012345678
DISCORD_COMMAND_PREFIX=!
CHROME_CDP_URL=http://127.0.0.1:9222
```

`DISCORD_CHANNEL_ID` is where alerts are posted. `DISCORD_CHANNELSEND_ID` is where users type commands. They can be the same channel.

Do not commit `.env` or `env/token.env` to GitHub.

## Start Chrome

For lululemon, the recommended setup is a real Chrome session connected over CDP. The window must stay open, but it can be minimized. Use a separate browser profile so the tracker does not interfere with your normal Chrome session.

macOS:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/lululemon-price-tracker-chrome"
```

Windows PowerShell:

```powershell
& "$Env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$Env:USERPROFILE\lululemon-price-tracker-chrome"
```

If lululemon shows a cookie prompt, region prompt, or challenge, bring up the Chrome window and handle it manually once.

## Run The Bot

```bash
python3 pricetrack_updated.py --cdp-url http://127.0.0.1:9222 bot
```

The bot checks active products every 10 minutes by default:

```bash
python3 pricetrack_updated.py --cdp-url http://127.0.0.1:9222 bot --interval-minutes 30
```

## Discord Commands

```text
!track <url>
!track <url> target 79
!products
!sale
!remove <id-or-url>
!pause <id-or-url>
!resume <id-or-url>
!check
!trackerhelp
```

Examples:

```text
!track https://shop.lululemon.com/en-ca/p/...
!track https://shop.lululemon.com/en-ca/p/... target 79
!products
!check
```

When a product is added, the bot checks it once immediately to save the current baseline price. It should not alert just because the product was added.

## CLI Usage

Products can also be managed from the terminal:

```bash
python3 pricetrack_updated.py add "https://shop.lululemon.com/..." --name "Align Pant 25" --target-price 79
python3 pricetrack_updated.py list
python3 pricetrack_updated.py --cdp-url http://127.0.0.1:9222 check
python3 pricetrack_updated.py --cdp-url http://127.0.0.1:9222 run --interval-minutes 10
```

## Alert Rules

The tracker alerts when:

- a product first appears on sale,
- the observed price drops below the previous observed price,
- the observed price is at or below `target_price`,
- the same sale price has not already been alerted.

## Local Files

- `products.csv`: products to track.
- `tracker_state.json`: last observed price, sale status, and alert state.
- `pricechecker.log`: runtime logs.
- `debug-pages/`: saved HTML/screenshots when parsing fails.
- `.browser-profile/`: Playwright browser profile when not using CDP.

Only `products.csv` is intended to be user-editable. Runtime files should not be committed to GitHub.

## Notes On Headless Mode

Headless Playwright and headless Chrome were tested, but lululemon did not respond reliably in this environment. The minimized real-Chrome CDP setup is currently the recommended path.

## Tests

```bash
python3 -m unittest
```

## Disclaimer

This is an unofficial tool. lululemon may change its website structure or bot protections at any time, which can require parser or browser-flow updates. Use responsibly and follow lululemon's terms and applicable laws.
