import datetime 

cur_time = datetime.now().strftime('%H:%M:%S')

cur_time = datetime.strptime(cur_time, '%H:%M:%S').time()

cur_time = cur_time

print(cur_time)
