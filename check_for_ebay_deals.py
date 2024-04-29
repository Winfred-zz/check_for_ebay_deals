# This is working code, but it's a bit crude.
# It'll alert me to any deals on eBay that are under a certain price, but only once for each item.
# But it will then no longer alert on a second item that is more expensive either, that's a design choice for now.
# if the skipped item is sold or a different item becomes cheaper, it will alert again on that search query.
#
#
from myebayfunctions import get_ebay_data
import csv
import time
import platform
import discord
import os
from myhelperfunctions import logger,sigterm_handler
import signal
from datetime import datetime
from dotenv import load_dotenv
from discord.ext import commands, tasks
from discord.ext.commands import Context
import functools
import typing
import asyncio

signal.signal(signal.SIGTERM, sigterm_handler)
my_logger = logger(log_filepath='logs/check_for_ebay_deals.log', logger_name='check_for_ebay_deals',debug=True)
my_logger.info("starting up")
intents = discord.Intents.default()

# =============================================================================
def to_thread(func: typing.Callable) -> typing.Coroutine:
    '''
    This is a helper function to run blocking functions in a separate thread to prevent the "Heartbeat blocked for more than 10 seconds" warning
    https://stackoverflow.com/questions/65881761/discord-gateway-warning-shard-id-none-heartbeat-blocked-for-more-than-10-second
    '''
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

# =============================================================================
my_logger.info("loading items to skip from configs/skip_items.txt")
skip_item_list = []
with open("configs/skip_items.txt", "r") as file:
    for line in file:
        skip_item_list.append(line.strip())
# =============================================================================
@to_thread
def load_deal_data_and_start_checking():
    '''
    This function loads the deals to check from ebay_deals_to_check.csv
    and then checks for new deals and price drops
    '''
    discord_messages = []
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

            for search_result in search_results:
                if search_result['id'] not in skip_item_list:
                    search_result_price = float(search_result['price'].replace("$", "").replace(",", ""))
                    my_logger.info("First result price: " + str(search_result_price))

                    if search_result_price <= float(max_price):
                        my_logger.info(str(search_result_price) + " is less than " + str(max_price) + " for " + friendly_name)
                        discord_message = f"[Found a {friendly_name} for {search_result_price}]({search_result['link']})"
                        discord_messages.append(discord_message)
                        time.sleep(10)
                        with open("configs/skip_items.txt", "a") as file:
                            file.write(f"{search_result['id']}\n")
                        skip_item_list.append(search_result['id'])
                        break
                    else:
                        my_logger.info(str(search_result_price) + " is greater than " + str(max_price) + " for " + friendly_name)
                        break
                
            time.sleep(60) # sleep for a minute
    return discord_messages

# =============================================================================
# This is the main class for the discord bot
# =============================================================================
class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        """
        This creates custom bot variables so that we can access these variables in cogs more easily.

        For example, The config is available using the following code:
        - self.config # In this class
        - bot.config # In this file
        - self.bot.config # In cogs
        """
    @tasks.loop(minutes=60.0)
    async def status_task(self) -> None:
        """
        Setup the check website task to run every 60 minutes
        """
        discord_messages = await load_deal_data_and_start_checking()
        if discord_messages:
            channel = self.get_channel(CHANNELID)
            my_logger.info(f"Sending messages to channel")
            for discord_message in discord_messages:
                await channel.send(discord_message)

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """
        Before starting the status changing task, we make sure the bot is ready
        """
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        """
        This will just be executed when the bot starts the first time.
        """
        my_logger.info(f"Logged in as {self.user.name}")
        my_logger.info(f"discord.py API version: {discord.__version__}")
        my_logger.info(f"Python version: {platform.python_version()}")
        my_logger.info(
            f"Running on: {platform.system()} {platform.release()} ({os.name})"
        )
        my_logger.info("-------------------")
        self.status_task.start()


    async def on_command_completion(self, context: Context) -> None:
        """
        The code in this event is executed every time a normal command has been *successfully* executed.

        :param context: The context of the command that has been executed.
        """
        full_command_name = context.command.qualified_name
        split = full_command_name.split(" ")
        executed_command = str(split[0])
        if context.guild is not None:
            my_logger.info(
                f"Executed {executed_command} command in {context.guild.name} (ID: {context.guild.id}) by {context.author} (ID: {context.author.id})"
            )
        else:
            my_logger.info(
                f"Executed {executed_command} command by {context.author} (ID: {context.author.id}) in DMs"
            )

    async def on_command_error(self, context: Context, error) -> None:
        """
        The code in this event is executed every time a normal valid command catches an error.

        :param context: The context of the normal command that failed executing.
        :param error: The error that has been faced.
        """
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=f"**Please slow down** - You can use this command again in {f'{round(hours)} hours' if round(hours) > 0 else ''} {f'{round(minutes)} minutes' if round(minutes) > 0 else ''} {f'{round(seconds)} seconds' if round(seconds) > 0 else ''}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!", color=0xE02B2B
            )
            await context.send(embed=embed)
            if context.guild:
                my_logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the guild {context.guild.name} (ID: {context.guild.id}), but the user is not an owner of the bot."
                )
            else:
                my_logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the bot's DMs, but the user is not an owner of the bot."
                )
        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                # We need to capitalize because the command arguments have no capital letter in the code and they are the first word in the error message.
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        else:
            raise error

if __name__ == "__main__":
    load_dotenv()
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    CHANNELID = int(os.getenv('CHANNELID'))
    bot = DiscordBot()
    bot.run(DISCORD_TOKEN)