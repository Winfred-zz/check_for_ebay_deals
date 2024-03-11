# check_for_ebay_deals
Python script that checks ebay search queries for low buy it now prices and then notifies on Discord.

It's relatively basic, but it works.

The following modules are required:

* pip install httpx[http2] parsel nested_lookup
* pip install requests
* pip install beautifulsoup4
* pip install discord.py
* pip install colorlog
* pip install python-dotenv

Check the configs directory for the files that need to be configured.

It's assumed you've already associated your bot with your server/channel.

[Here is a general guide on that](https://realpython.com/how-to-make-a-discord-bot-python/)

Once the CSV file and config files are configured, if you run check_for_ebay_deals.py it will enter a loop and check the search queries once per hour.

Currently the script is set up to not alert between 2am and 12pm.