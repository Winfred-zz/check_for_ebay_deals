# This is working code, but it's a bit crude.
# It'll alert me to any deals on eBay that are under a certain price, but only once for each item.
# But it will then no longer alert on a second item that is more expensive either, that's a design choice for now.
# if the skipped item is sold or a different item becomes cheaper, it will alert again on that search query.
#
#
from myebayfunctions import get_ebay_data
# open a csv file into an array of dicts
import csv
import time
import discord
import os
from myhelperfunctions import logger,sigterm_handler
import signal
from datetime import datetime
from dotenv import load_dotenv

signal.signal(signal.SIGTERM, sigterm_handler)
my_logger = logger(log_filepath='logs/check_for_ebay_deals.log', logger_name='check_for_ebay_deals',debug=True)
my_logger.info("starting up")

# =============================================================================
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CHANNELID = int(os.getenv('CHANNELID'))


# =============================================================================
# Discord related
# =============================================================================
client = discord.Client(intents=discord.Intents.default())
@client.event
async def on_ready():
    my_logger.info(str(client.user) + " has connected to Discord")
    channel = client.get_channel(CHANNELID) #  general channel
    await channel.send(discord_message) #  Sends message to channel
    await client.close()
# =============================================================================
my_logger.info("loading items to skip from configs/skip_items.txt")
skip_item_list = []
with open("configs/skip_items.txt", "r") as file:
    for line in file:
        skip_item_list.append(line.strip())
# =============================================================================
if __name__ == "__main__":
    while True:
        # if it's between 12pm and 2am, check for deals
        if datetime.now().hour > 12 or datetime.now().hour < 2:
            # =============================================================================
            my_logger.info("loading deals to check from from configs/ebay_deals_to_check.csv")
            csvfile = "configs/ebay_deals_to_check.csv"
            # creating a csv reader object here, so it reloads the file each time and we don't have to restart the bot when updating the csv
            with open(csvfile, mode='r') as file:
                csv_reader = csv.DictReader(file)
                # Initialize an empty list to store the dictionaries
                data_list = []
                # Iterate through each row in the CSV file
                for row in csv_reader:
                    # Append each row (as a dictionary) to the list
                    data_list.append(row)
            # =============================================================================
            for data in data_list:
                search_query = data['search_query']
                max_price = data['max_price']
                friendly_name = data['friendly_name']

                search_results = get_ebay_data(search_query, completed=False)

                first_result = search_results[0]
                first_result_price = float(first_result['price'].replace("$", "").replace(",", ""))
                my_logger.info("First result price: " + str(first_result_price))

                if first_result_price <= float(max_price):
                    discord_message = f"[Found a {friendly_name} for {first_result_price}]({first_result['link']})"
                    client.run(TOKEN) # this isn't the best way to do this, ideally we'd use asyncio to run the bot in the background. But this way is easier for now.
                    # the way it's set up, the bot will log in, send the message, then log out.
                    # now add the item to the skip list
                    with open("configs/skip_items.txt", "a") as file:
                        file.write(f"{first_result['id']}\n")
                    skip_item_list.append(first_result['id'])
                else:
                    my_logger.info("No deals found for " + friendly_name)
                    time.sleep(60) # sleep for a minute
        
        time.sleep(3600) # sleep for an hour
    
