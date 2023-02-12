from datetime import datetime, time
import time
import requests
import pandas as pd
import numpy as np
import pymongo
from decouple import config


SECRET_KEY = config("SECRET_KEY")

def Pull_Chain_Data(Symbol):

    # MongoDB Connection
    # SECRET_KEY = config("SECRET_KEY")

    Dataset_Name = "NSE"
    Collection = Symbol

    # linkMD = f"mongodb+srv://sumitgujrathi24:8lpVTDIBtyHKWRH3@sumit.ybjewjm.mongodb.net/{Dataset_Name}?retryWrites=true&w=majority"
    linkMD = SECRET_KEY

    Client = pymongo.MongoClient(linkMD, 27017)

    db = Client[Dataset_Name]

    DATA_NSE = db[Collection]


    # NSE live data Option Chain Nifty

    url = f'https://www.nseindia.com/api/option-chain-indices?symbol={Symbol}'  # for NIFTY -BANKNIFTY Copy-paste


    headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, '
                            'like Gecko) '
                            'Chrome/80.0.3987.149 Safari/537.36',
            'accept-language': 'en,gu;q=0.9,hi;q=0.8', 'accept-encoding': 'gzip, deflate, br'}
    session = requests.Session()
    request = session.get(url, headers=headers)
    cookies = dict(request.cookies)

    # print(response.status_code)
    # DATA = response.content

    # list of item to be data framed => 1 = 'expiryDates' , 2 = 'data' , 3 = 'underlyingValue' , 4 = 'strikePrices'

    try:
        DATA = session.get(url, headers=headers, timeout=0.5, cookies=cookies).json()['records']['data']

        Cols = ['PE.underlyingValue','expiryDate','PE.openInterest','PE.changeinOpenInterest','PE.impliedVolatility','PE.lastPrice','PE.totalBuyQuantity','PE.totalSellQuantity','strikePrice','CE.totalSellQuantity','CE.totalBuyQuantity','CE.lastPrice','CE.impliedVolatility','CE.changeinOpenInterest','CE.openInterest']

        df = pd.json_normalize(DATA)

        df = df.fillna(0)

        df = df[Cols]

        # Output Data

        PE_OI = df['PE.openInterest'].sum()
        PE_COI =  df['PE.changeinOpenInterest'].sum()
        PE_Volat =  np.round(df['PE.impliedVolatility'].mean(),2)

        CE_OI = df['CE.openInterest'].sum()
        CE_COI = df['CE.changeinOpenInterest'].sum()
        CE_Volat = np.round(df['CE.impliedVolatility'].mean(),2)

        NIFTY = df['PE.underlyingValue'].max()
        PCR = np.round(((PE_COI / CE_COI) -1),2)

        # Date  format => YYYY,MM,DD, HH,MM,SS
        Pull_Time = datetime.now()
        OI = {"Date":Pull_Time, "PE_OI":PE_OI,"CE_OI":CE_OI, "PE_COI":PE_COI, "CE_COI":CE_COI, "NIFTY":NIFTY, "PCR":PCR, "PE_Volatility": PE_Volat, "CE_Volatility": CE_Volat}

        DATA_NSE.insert_one(OI)

        print(OI)



        # OI Data & save to CSV file

        # OI_DATA.to_csv(f'{path}\\OPTION_OI_DATA.csv', mode='a', index=False, header=False)

    except:
        pass


Pull_Chain_Data("NIFTY")