# Lululemon Price Tracker

The Lululemon Price Tracker is a Python script using Discord designed to track product prices on the Lululemon website. It allows users to monitor the prices of their favorite Lululemon products and receive notifications when there are price reductions or sales.

## How it Works

The Lululemon Price Tracker uses web scraping techniques to gather product information from the Lululemon website. It periodically checks the prices of the products and sends notifications through a Discord channel if there are any price drops or sales.

## Getting Started

1. Install the required dependencies:
   ```bash
   pip install discord
   pip install pandas
   pip install requests
   pip install aiohttp
   pip install beautifulsoup4
   pip install selenium
   pip install price-parser
   ```

2. Create a Discord bot and obtain the bot token.

3. Configure the bot token and Discord channel IDs in the script:
   ```python
   DISCORD_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
   DISCORD_CHANNEL_ID = "YOUR_DISCORD_CHANNEL_ID_HERE"
   DISCORD_CHANNELSEND_ID = "YOUR_DISCORD_CHANNELSEND_ID_HERE"
   ```

4. Create a CSV file named `products.csv` with the product URLs you want to track. The CSV should have five columns: "Product", "url", "alert_price", "price" and "alert". Entries in the CSV file may look like the following example:
   ```
   Product,URL
   Lululemon Yoga Pants,https://www.lululemon.com/yoga-pants, 100, 120, FALSE
   Lululemon Sports Bra,https://www.lululemon.com/sports-bra, 50, 80, FALSE
   ```
   You do not need to modify this file, simply create a csv file with the appropriate columns without any entries as they will be added from Discord.

5. Run the script:
   ```bash
   python price_tracker.py
   ```

## Features

- Tracks product prices and detects price reductions.
- Sends Discord notifications for price drops and sales.
- Allows users to add and remove products from tracking.

## Discord Commands

- `!products`: Shows the list of products currently being tracked.
- `!sale`: Shows the products that are currently on sale.
- `!remove <URL>`: Removes a product from tracking. Replace `<URL>` with the URL of the product you want to remove.

## Customization

You can customize the frequency of price checks and other settings in the script. Additionally, you can modify the appearance and content of the Discord notifications.

## Disclaimer

The Lululemon Price Tracker is an unofficial tool and may stop working if the Lululemon website structure or policies change. Use it responsibly and ensure compliance with Lululemon's terms and conditions.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

Please feel free to modify the README file to include more details, instructions, or any other relevant information you'd like to provide to users. Also, don't forget to include the license file (`LICENSE`) in your project directory, as mentioned in the README template above.
