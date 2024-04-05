from bs4 import BeautifulSoup
import time
import requests
from myhelperfunctions import logger

my_logger = logger(log_filepath='logs/myebayfunctions.log', logger_name='myebayfunctions',debug=True)

def get_ebay_data(search_query,pages=1,completed=False):
    headers = {
    # get updated user agent string here: https://www.whatismybrowser.com/detect/what-is-my-user-agent/
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }
    if completed:
        LH_Sold = '1'
    else:
        LH_Sold = '0'
    params = {
    '_nkw': search_query,      # search query
    'LH_Sold': LH_Sold,                   # shows sold items  
    'LH_BIN': '1',                       # buy it now
    'LH_PrefLoc': '1',                   # US only
    'LH_FS': '1',                        # free shipping
    '_sop': '15',                        # sort by price + shipping lowest first
    '_pgn': 1                            # page number
    }
    data = []
    sellerinfo = None
    sellername = None
    salescount = None
    sellerrating = None
    while True:
        try:
            while True:
                try:
                    page = requests.get('https://www.ebay.com/sch/i.html', params=params, headers=headers, timeout=30)
                    break
                except requests.exceptions.RequestException as e:
                    my_logger.error("Requests get Error: " + str(e))
                    time.sleep(60)
            
            soup = BeautifulSoup(page.text, 'lxml')
            my_logger.info("Extracting page: " + str(params['_pgn']))
            
            for products in soup.select(".s-item__info"):
                if products.select_one(".s-item__title span").text != "Shop on eBay":
                    title = products.select_one(".s-item__title span").text
                    if products.select_one(".s-item__subtitle span") is not None:
                        subtitle = products.select_one(".s-item__subtitle span").text
                        if subtitle != "Parts Only":
                            price = products.select_one(".s-item__price").text
                            if products.select_one(".s-item__seller-info-text") is not None:
                                sellerinfo = products.select_one(".s-item__seller-info-text").text
                                sellername = sellerinfo.split(" ")[0]
                                salescount = sellerinfo.split(" ")[1]
                                sellerrating = sellerinfo.split(" ")[2]

                            link = products.select_one(".s-item__link")["href"]
                            id = link.split("/")[-1].split("?")[0]
                            if "to" not in price:
                                data.append({
                                "title" : title,
                                "subtitle" : subtitle,
                                "price" : price,
                                "sellerinfo" : sellerinfo,
                                "link" : link,
                                "sellername" : sellername,
                                "salescount" : salescount,
                                "sellerrating" : sellerrating,
                                "id" : id
                                })

            params['_pgn'] += 1
            if params['_pgn'] > pages:
                break
        except Exception as e:
            my_logger.error("Error: " + str(e))
            break
    return data