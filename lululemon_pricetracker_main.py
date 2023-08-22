#April 30th, 2022
#Lululemon Price Tracker Discord Bot
#Requires discord Bot Token, Channel ID, and Channel Send ID. Please refer to online guides on how to find these
#Requires a products.csv file in the same directory. This file will store all the product links.

import discord
import pandas as pd
import requests
import os
import time
import asyncio
import csv
import re
import aiohttp
import logging
import sys
import concurrent.futures
import traceback
import math
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from urllib.parse import urlparse
from pandas import DataFrame
from price_parser import Price
from discord.ext import commands
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


# Configuration
PRODUCT_URL_CSV = "products.csv"
DISCORD_BOT_TOKEN = ""
DISCORD_CHANNEL_ID = ""
DISCORD_CHANNELSEND_ID = ""
intents = discord.Intents.default()

intents.members = True
intents.presences = True
client = discord.Client(intents=intents)
last_request_time = 0
THROTTLE_INTERVAL_SECONDS = 5

session = requests.Session()
retry = Retry(total=5, backoff_factor=0.1, status_forcelist=[ 500, 502, 503, 504 ])
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pricechecker.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

# Retreives URLs from CSV file
def get_urls(csv_file):
    df = pd.read_csv(csv_file)
    return df

# Reads CSV file
def read_csv_file(filename):
    urls = []
    with open(filename, newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            urls.append(row[0])
    return urls

def get_response(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36 Edge/16.16299",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "TE": "Trailers"
    }
    response = requests.get(url, headers=headers)
    return response.text


async def fetch_url(session, url):
    async with session.get(url) as response:
        response.raise_for_status()
        html = await response.text()
        return html

async def process_product(session, product):
    url = product["url"]
    logging.info(f"Currently checking: {url}")
    try:
        html = await fetch_url(session, url)
        soup = BeautifulSoup(html, "html.parser")
        on_sale_check = re.search('<span class="pill-2i_OA product-description_pdpTitlePill__127xe pillSecondary-2s4zo">.*?</span>', html)
        if on_sale_check:
            try:
                price_element = soup.find('span', {'class': 'price-1jnQj price'}).find_all('span')[1]
                price_element_string = price_element.text
                price_element_digits = re.findall(r'\d+', price_element_string)
                if price_element_digits:

                    price_element_digits_float = [float(digit) for digit in price_element_digits]
                    min_price_found = min(price_element_digits_float)
                    logging.info(
                        f"Product on Sale: {product['Product']} with price: {price_element_digits} compare with min price: {min_price_found} and status: {product['Status']} and math is: {math.isnan(product['alert_price'])}")
                    # if product is already on sale, compare price
                    if product["Status"] and not math.isnan(product["alert_price"]): #if price is not NaN
                        if min_price_found < float(product["alert_price"]):
                            product["alert_price"] = min_price_found
                            product["alert"] = True
                        else:
                            logging.info("Product on sale but no further price reductions")
                            product["alert"] = False

                    else:
                        product["alert_price"] = min_price_found
                        product["alert"] = True #Sale Alert is Sent
                else:
                    logging.info(f"[1] Product not on sale: {url}")
                    product["alert"] = False
            except Exception as e:
                logging.error(f"Unable to retrieve price:{e}: {url}")
                product["alert"] = False
        else:
            product["alert"] = False
            logging.info(f"[2] Product not on sale: {url}")
    except Exception as e:
        logging.error(f"{url} with error message: {e}")
        product["alert"] = False

    return product

async def send_discord_message(product, channel_id):
    try:
        channel = await client.fetch_channel(channel_id)
        await channel.send(f"Price drop alert for {product['Product']}: {product['url']} with price: {product['alert_price']}")
        logging.info(f"Message sent to Discord channel {channel_id}: Price drop alert for {product['Product']}: {product['url']}")
    except Exception as e:
        logging.error(f"Error sending message to Discord channel {channel_id}: {e}")
    return


@client.event
async def on_message(message):
    url = ""
    if message.channel.id == DISCORD_CHANNELSEND_ID and message.author != client.user:
        if message.content.startswith("http://") or message.content.startswith("https://"):
            # Do something with the URL, e.g. send a reply message
            await message.channel.send("Thanks for the URL!")
            url = message.content
            url = url.strip()
        elif message.content.startswith('!products'):
            # Read the CSV file to get the list of products
            with open('products.csv', 'r') as f:
                reader = csv.reader(f)
                next(reader)
                products = list(reader)
            if not products:
                response = "No products found"
            else:
            # Send a message to the user with the list of products
                response = 'Here are the products I am currently tracking:\n'
            for product in products:
                response += f'- {product[0]} ({product[1]})\n'
            await message.channel.send(response)
            logging.info(f"Sending response: {response}")
        elif message.content.startswith('!sale'):
            # Read the CSV file to get the list of products
            with open('products.csv', 'r') as f:
                reader = csv.reader(f)
                next(reader)
                products = list(reader)

            # Filter the products that are on sale
            sale_products = [product for product in products if len(product) >= 4 and product[3] == 'True']

            # Send a message to the user with the list of sale products
            if not sale_products:
                response = "No products on sale at the moment"
            else:
                response = 'Here are the products on sale:\n'
                for product in sale_products:
                    response += f'- {product[0]} ({product[1]}) with Current Price: {product[2]}\n'
            await message.channel.send(response)
            logging.info(f"Sending response: {response}")
        elif message.content.startswith('http://') or message.content.startswith('https://'):
            # Add the new URL to the CSV file
            with open('products.csv', 'a') as f:
                writer = csv.writer(f)
                writer.writerow([message.content])

            # Send a message to the user confirming that the URL has been added
            await message.channel.send('Thanks for the URL!')
        elif message.content.startswith("!remove"):
            if len(message.content.split(" "))<2:
                await message.channel.send("Invalid URL or command, please try again.")
            else:
                _, toRemove = message.content.split(" ")
                toRemove = toRemove.strip()
                logging.info(f"Removing Product: {toRemove}")
                # Remove the product from the DataFrame
                df = pd.read_csv(PRODUCT_URL_CSV)
                df.drop(df.loc[df['url'] == toRemove].index, inplace=True)
                df.to_csv(PRODUCT_URL_CSV, index=False)
                await message.channel.send(f"Removed Product: {toRemove}")
        else:
            await message.channel.send("Invalid URL or command, please try again.")

    if url:
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        try:
          #  print("Testing: ", soup.find('h1', {'class': re.compile('OneLinkNoTx.*product-title_title__i8NUw')}).text.strip())
            product_name = soup.find('h1', {'class': 'OneLinkNoTx product-title_title__i8NUw'}).text.strip()
        except Exception as e:
            logging.error(f"Could not find product name, giving it a generic name based on the URL: {e}")
            parsed_url = urlparse(url)
            path_segments = parsed_url.path.split('/')
            product_name = path_segments[3]

        with open(PRODUCT_URL_CSV, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([product_name, url])
        logging.info(f"Added Product: {product_name}: {url}")
        await message.channel.send(f"New product URL '{url}' has been added.")


@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user}")

async def main():
    await client.wait_until_ready()
    logging.info("Client is now ready")

    # Set up the task queue
    queue = asyncio.Queue()
    workers = [asyncio.create_task(process_queue(queue)) for _ in range(5)]

    try:
        # Read the list of products from the CSV file
        df = pd.read_csv(PRODUCT_URL_CSV)

        if df.empty:
            logging.info("CSV FILE IS EMPTY")
        else:
            # Create an event to signal the URL checking
            check_event = asyncio.Event()

            # Start the task for periodic URL checking
            asyncio.create_task(check_urls_periodically(queue, df.to_dict("records")))

            # Start the workers for processing the queue
            await asyncio.gather(*workers)

    except Exception as e:
        logging.error(f"Error occurred in main: {e}")
        traceback.print_exc()



async def process_queue(queue):
    async with aiohttp.ClientSession() as session:
        while True:
        # Get the next product from the task queue
            product = await queue.get()
            try:
                if isinstance(product, dict):
                    # Process the product asynchronously
                    product = await process_product(session, product)
                    logging.info(f"Processed product: {product}")
                    if product["alert"]:

                        await send_discord_message(product, DISCORD_CHANNEL_ID)
                        # product["alert"] = False
                        # product["Status"] = True

                        print("Replacing row: ", product)
                        df = pd.read_csv(PRODUCT_URL_CSV)
                        cur_url = product['url']
                        df.loc[df['url'] == cur_url, ['alert', 'Status', 'alert_price']] = [False, True,
                                                                                            product['alert_price']]
                        df.to_csv(PRODUCT_URL_CSV, index=False)


            except Exception as e:
                #logging.error(f"Error processing product name {product['Product']}, product url: {product['url']}: {e}")
                logging.error(f"Product: {product} with error: {e}")
                traceback.print_exc()
            # Mark the task as complete
            queue.task_done()

async def check_urls_periodically(queue, products):
    while True:
        for product in products:
            await queue.put(product)
        await asyncio.sleep(60 * 10)  # Sleep for 10 minutes before checking again


loop = asyncio.get_event_loop()
loop.create_task(client.start(DISCORD_BOT_TOKEN))
loop.create_task(main())
loop.run_forever()
