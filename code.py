

import time
import wifi
import socketpool
import adafruit_requests
import ssl
import board
import digitalio
import busio
import displayio
import terminalio
from adafruit_display_text import label
import os
import json
import vectorio
import rtc
import adafruit_ntp



from font_orbitron_medium_webfont_24 import FONT as BIGFONT
from font_orbitron_medium_webfont_12 import FONT as SMALLFONT
from json_details import HOST_IP, HOST_PORT, JSON_PWD

# -------------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------------
SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")

# Heating Config
URL = f"http://{HOST_IP}:{HOST_PORT}/{JSON_PWD}/all"

NORMAL_INTERVAL = 300    # 5 Minutes
ERROR_INTERVAL = 900     # 15 Minutes
SYNC_INTERVAL = 86400 # 24 hrs

class HeatingError(Exception): pass
class WiFiError(Exception): pass
class ParseError(Exception): pass

display = board.DISPLAY

# -------------------------------------------------------------------------
# NETWORK HELPERS
# -------------------------------------------------------------------------
pool = socketpool.SocketPool(wifi.radio)
requests = adafruit_requests.Session(pool, ssl.create_default_context())
internal_rtc = rtc.RTC()
ntp = adafruit_ntp.NTP(pool, tz_offset=0)

def ensure_wifi(max_attempts=10):
    current_attempts = 0

    if wifi.radio.connected:
        return
    
    print("Reconnecting WiFi...")

    while not wifi.radio.connected:
        try:
            wifi.radio.connect(SSID, PASSWORD)
            print("Connected!")
        except OSError as e:
            print(f"Retry in 10s... ({e})")
            time.sleep(10)

            current_attempts += 1
            if current_attempts >= max_attempts:
                raise WiFiError(f"Connect failed: {e}")
        
            
def extract_value(blob, key_bytes):
    """Extract a numeric value from raw JSON bytes. Necessary because converting the received string to json causes errors with CircuitPython"""
    idx = blob.find(key_bytes)
    if idx == -1:
        return 0.0
    
    start = idx + len(key_bytes)
    end = start
    while end < len(blob) and blob[end] not in (44, 125):  # , or }
        end += 1
    
    try:
        return float(blob[start:end])
    except ValueError as e:
        print(f"Error extracting values: {e}")


def sync_time():
    ensure_wifi()
    print("Syncing clock...")
    try:
        internal_rtc.datetime = ntp.datetime
        print("Clock Synced!")
    except (OSError, ArithmeticError) as e:
        print(f"NTP Sync Fail: {e}")

def get_local_time_str():
    """
    Reads UTC from internal clock.
    Adds +1 (Winter) or +2 (Summer) (for Vienna)
    Returns 'HH:MM' string.
    """
    now_utc = time.localtime()
    
    # 1. Determine DST
    # (Simplified EU Rule: March to Oct)
    is_summer = False
    # Check months
    if now_utc.tm_mon > 3 and now_utc.tm_mon < 10:
        is_summer = True
    elif now_utc.tm_mon == 3: # March
        # Last Sunday logic roughly
        d31 = time.mktime((now_utc.tm_year, 3, 31, 12, 0, 0, 0, 0, 0))
        last_sunday = 31 - time.localtime(d31).tm_wday
        if now_utc.tm_mday > last_sunday: is_summer = True
    elif now_utc.tm_mon == 10: # October
        d31 = time.mktime((now_utc.tm_year, 10, 31, 12, 0, 0, 0, 0, 0))
        last_sunday = 31 - time.localtime(d31).tm_wday
        if now_utc.tm_mday < last_sunday: is_summer = True
        
    # 2. Calculate Offset
    offset = 2 if is_summer else 1
    
    # 3. Apply Offset
    local_seconds = time.mktime(now_utc) + (offset * 3600)
    local_t = time.localtime(local_seconds)
    
    return f"{local_t.tm_hour:02d}:{local_t.tm_min:02d}"

def check_heating_error(raw_bytes):
    start = raw_bytes.find(b'"error":')
    if start == -1: return None

    brace_open = raw_bytes.find(b'{', start)
    brace_close = raw_bytes.find(b'}', brace_open)

    if brace_open != -1 and brace_close != -1:
        content = raw_bytes[brace_open+1 : brace_close]
        if len(content.strip()) > 0:
            return content.decode("utf-8", "ignore")
    return None


def fetch_data():
    ensure_wifi()
    try:
        r = requests.get(URL, timeout=10)
    except (OSError, RuntimeError) as e:
        raise WiFiError(f"Request Failed: {e}")
        
    with r:
        if r.status_code != 200: 
            raise WiFiError(f"HTTP {r.status_code}")
        
        raw = r.content
        sys_err = check_heating_error(raw)
        
        try:
            t_out = extract_value(raw, b'"L_ambient":') / 10.0
            t_in  = extract_value(raw, b'"L_roomtemp_act":') / 10.0
            t_wat = extract_value(raw, b'"L_ontemp_act":') / 10.0
            temp_values =  (t_out, t_in, t_wat)

            return (temp_values, sys_err)

        except Exception:
            raise ParseError("JSON Extract Failed")


# -------------------------------------------------------------------------
# UI
# -------------------------------------------------------------------------

previous_temps = None 

def make_lbl(text, x, y, font=terminalio.FONT):
    return label.Label(font, text=text, color=0x000000, x=x, y=y, scale=1)

def draw_error_footer(group, msg):
    pal = displayio.Palette(1); pal[0] = 0x000000
    bg = vectorio.Rectangle(pixel_shader=pal, width=250, height=24, x=0, y=98)
    group.append(bg)
    lbl = label.Label(SMALLFONT, text=str(msg)[:28], color=0xFFFFFF)
    lbl.anchor_point = (0.5, 0.5); lbl.anchored_position = (125, 110)
    group.append(lbl)

def update_ui(data, error_msg):
    global previous_temps

    
    group = displayio.Group()

    bg = displayio.Bitmap(250, 122, 1)
    pal = displayio.Palette(1); pal[0] = 0xFFFFFF
    group.append(displayio.TileGrid(bg, pixel_shader=pal))
    
    h_pal = displayio.Palette(1); h_pal[0] = 0x000000
    group.append(vectorio.Rectangle(pixel_shader=h_pal, width=250, height=24, x=0, y=0))
    
    time_lbl = label.Label(SMALLFONT, text=f"LAST UPDATE: {get_local_time_str()}", color=0xFFFFFF)
    time_lbl.anchor_point = (0.0, 0.5); time_lbl.anchored_position = (5, 12)
    group.append(time_lbl)

    display_data = data if data else previous_temps
    
    if display_data:
        curr_out, curr_in, curr_wat = display_data
        group.append(vectorio.Rectangle(pixel_shader=h_pal, width=2, height=98, x=83, y=24))
        group.append(vectorio.Rectangle(pixel_shader=h_pal, width=2, height=98, x=166, y=24))

        def draw_col(title, val, x_pos):
            l = make_lbl(title, 0, 0, font=SMALLFONT)
            l.anchor_point = (0.5, 0.0); l.anchored_position = (x_pos, 35)
            group.append(l)
            v = make_lbl(f"{val:.1f}", 0, 0, font=BIGFONT)
            v.anchor_point = (0.5, 0.0); v.anchored_position = (x_pos, 55)
            group.append(v)
          
        draw_col("AUSSEN", curr_out, 41)
        draw_col("INNEN", curr_in, 125)
        draw_col("WASSER", curr_wat, 208)

        if data: previous_temps = data 
    
    if error_msg:
        draw_error_footer(group, error_msg)

    display.root_group = group
    
    display.refresh()
    print(f"Refreshed. Msg: {error_msg}")
    




if __name__ == "__main__":
    print("Starting...")
    sync_time()

    last_sync_time = time.monotonic()

    while True:
        current_temps = None
        current_error = None
        
        if (time.monotonic() - last_sync_time) > SYNC_INTERVAL:
            sync_time()
            last_sync_time = time.monotonic()
        
        try:
            current_temps, warning_msg = fetch_data()
            
            if warning_msg:
                print(f"Log: Heating Warning - {warning_msg}")
                current_error = f"Alert: {warning_msg}"
                
        except WiFiError as e:
            print(f"Log: Network - {e}")
            current_error = "WiFi Lost"
        except ParseError as e:
            print(f"Log: Parse - {e}")
            current_error = "Data Error"

        update_ui(current_temps, current_error)
  
        if current_error and current_temps is None:
            time.sleep(ERROR_INTERVAL)
        else:
            time.sleep(NORMAL_INTERVAL)