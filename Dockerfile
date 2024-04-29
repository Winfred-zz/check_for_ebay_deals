FROM python:3

RUN apt-get update && apt-get install -y git
RUN python -m pip install --upgrade pip
# can use this if pip keeps using cached modules (it does)
RUN pip cache purge

RUN pip install httpx[http2] parsel nested_lookup
RUN pip install requests
RUN pip install beautifulsoup4
RUN pip install discord.py
RUN pip install colorlog
RUN pip install python-dotenv

WORKDIR /data/check_for_ebay_deals
CMD [ "python", "check_for_ebay_deals.py" ]