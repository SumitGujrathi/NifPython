from datetime import datetime, time
import time
import requests
import pandas as pd
import numpy as np
import pymongo
from decouple import config


# List of Holidays Important *** Date formate should be like MM-DD-YYYY eg. 01-01-2023****

OffDates = ["01-26-2023","03-07-2023","03-30-2023","04-04-2023","04-07-2023","04-14-2023","05-01-2023",
"06-28-2023","08-15-2023","09-19-2023","10-02-2023","10-24-2023","11-14-2023","11-27-2023","12-25-2023",
"01-01-2023","01-07-2023","01-08-2023","01-14-2023","01-15-2023","01-21-2023","01-22-2023","01-28-2023",
"01-29-2023","02-11-2023","02-18-2023","02-19-2023","02-25-2023",
"02-26-2023","03-04-2023","03-05-2023","03-11-2023","03-12-2023","03-18-2023","03-19-2023","03-25-2023",
"03-26-2023","04-01-2023","04-02-2023","04-08-2023","04-09-2023","04-15-2023","04-16-2023","04-22-2023",
"04-23-2023","04-29-2023","04-30-2023","05-06-2023","05-07-2023","05-13-2023","05-14-2023","05-20-2023",
"05-21-2023","05-27-2023","05-28-2023","06-03-2023","06-04-2023","06-10-2023","06-11-2023","06-17-2023",
"06-18-2023","06-24-2023","06-25-2023","07-01-2023","07-02-2023","07-08-2023","07-09-2023","07-15-2023",
"07-16-2023","07-22-2023","07-23-2023","07-29-2023","07-30-2023","08-05-2023","08-06-2023","08-12-2023",
"08-13-2023","08-19-2023","08-20-2023","08-26-2023","08-27-2023","09-02-2023","09-03-2023","09-09-2023",
"09-10-2023","09-16-2023","09-17-2023","09-23-2023","09-24-2023","09-30-2023","10-01-2023","10-07-2023",
"10-08-2023","10-14-2023","10-15-2023","10-21-2023","10-22-2023","10-28-2023","10-29-2023","11-04-2023",
"11-05-2023","11-11-2023","11-12-2023","11-18-2023","11-19-2023","11-25-2023","11-26-2023","12-02-2023",
"12-03-2023","12-09-2023","12-10-2023","12-16-2023","12-17-2023","12-23-2023","12-24-2023","12-30-2023","12-31-2023"
]

# NSE Data Pull + Data Clearing + MongoDB Funciton


# SECRET_KEY = config("SECRET_KEY")


def Pull_Chain_Data(Symbol):

    # MongoDB Connection
    # SECRET_KEY = config("SECRET_KEY")

    Dataset_Name = "NSE"
    Collection = Symbol

    linkMD = f"mongodb+srv://sumitgujrathi24:8lpVTDIBtyHKWRH3@sumit.ybjewjm.mongodb.net/{Dataset_Name}?retryWrites=true&w=majority"
#     linkMD = SECRET_KEY

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


# Date Setting 

Today_Date = datetime.now().strftime('%m-%d-%Y')
print(Today_Date)


# On_Day = Today_Date in OffDates # If False Market is On.
On_Day = Today_Date in OffDates

print(On_Day)


# Start Time Setting 

cur_time = datetime.now().strftime('%H:%M:%S')
cur_time = datetime.strptime(cur_time, '%H:%M:%S').time()
print(cur_time)


# Start Time Setting

Start_time = '10:05:00' # time on web 09:17:00 in india
Start_time = datetime.strptime(Start_time, '%H:%M:%S').time()


# Stop Time Setting 

Stop_time = '10:30:00'   # time on web 15:28:00 in india
Stop_time = datetime.strptime(Stop_time, '%H:%M:%S').time()


restart = True
# def date_input():
#     if (Today_Date == input()):
#         restart = True

while restart:

    if (On_Day == False):

        # Date Setting 
        Today_Date = datetime.now().strftime('%m-%d-%Y')
        Today_Date = Today_Date
        On_Day = Today_Date in OffDates
        
        restart = True

        while restart:

            if(cur_time > Start_time and cur_time < Stop_time):
                restart = True
                # Current Time Setting 
                cur_time = datetime.now().strftime('%H:%M:%S')
                cur_time = datetime.strptime(cur_time, '%H:%M:%S').time()
                cur_time = cur_time

                # Main Script here below =>


                Pull_Chain_Data("NIFTY")
#                 Pull_Chain_Data("BANKNIFTY")




                # <= Main Script End =>
                print(cur_time)
                print("Pulling")
                time.sleep(240)
                
            else:
                # Current Time Setting
                cur_time = datetime.now().strftime('%H:%M:%S')
                cur_time = datetime.strptime(cur_time, '%H:%M:%S').time()
                cur_time = cur_time


                print("Time Over")
                print(cur_time)
                restart = "Wait"      

                Today_Date = datetime.now().strftime('%m-%d-%Y')
                Today_Date = Today_Date    
                On_Day = Today_Date in OffDates
                # On_Day = input() in OffDates


                if (On_Day == False):
                # Date Setting 

                    Today_Date = datetime.now().strftime('%m-%d-%Y')
                    Today_Date = Today_Date
                    On_Day = Today_Date in OffDates

                    time.sleep(1)
                    restart = "Wait"
                time.sleep(50)
                break

                # restart = False

    else:

        cur_time = datetime.now().strftime('%H:%M:%S')
        cur_time = datetime.strptime(cur_time, '%H:%M:%S').time()
        cur_time = cur_time


        print("Off Day")
        print(cur_time)
        restart = "Wait"
        Today_Date = datetime.now().strftime('%m-%d-%Y')
        Today_Date = Today_Date    
        On_Day = Today_Date in OffDates
        # On_Day = input() in OffDates
      
        if (On_Day == True):
        # Date Setting 

            Today_Date = datetime.now().strftime('%m-%d-%Y')
            Today_Date = Today_Date
            On_Day = Today_Date in OffDates

            time.sleep(1)
            restart = True
        time.sleep(43200)         



