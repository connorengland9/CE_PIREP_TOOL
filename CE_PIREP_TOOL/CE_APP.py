import requests
import time
import json
import re
import urllib3
from flask import Flask, render_template, jsonify, make_response
from datetime import datetime, timezone, timedelta

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- CONFIGURATION ---
MAIN_AIRPORTS = [
    {"id": "PGUM", "name": "Agana Airport"},
    {"id": "PGUA", "name": "Andersen AFB"},
    {"id": "PGSN", "name": "Saipan Airport"}
]

AUX_AIRPORTS = [
    {"id": "PGRO", "name": "Rota Int'l"},
    {"id": "PGWT", "name": "West Tinian"}
]

# --- LOGIC HELPERS ---
def get_cloud_base(layer):
    """Safely extract cloud base as an integer."""
    base = layer.get('base')
    try:
        if base is not None:
            return int(base)
    except (ValueError, TypeError):
        pass
    return None

def check_pirep_condition(station_data):
    conditions = []
    
    # 1. CEILING
    ceiling_layers = []
    clouds = station_data.get('clouds', [])
    for layer in clouds:
        cover = layer.get('cover', '')
        base = get_cloud_base(layer)
        if cover in ['BKN', 'OVC', 'VV'] and base is not None and base <= 5000:
            ceiling_layers.append(base)
    if ceiling_layers:
        conditions.append(f"CIG {min(ceiling_layers)}FT")
    
    # 2. VISIBILITY
    vis = station_data.get('visib')
    if vis is not None:
        try:
            if isinstance(vis, str) and '+' in vis:
                 val = float(vis.replace('+', ''))
            else:
                 val = float(vis)
            if val <= 5.0: conditions.append(f"VIS {vis}SM")
        except ValueError: pass

    # 3. HAZARDOUS WX
    wx = station_data.get('wxString', "")
    if 'TS' in wx: conditions.append("THUNDERSTORM")
    if 'VA' in wx: conditions.append("VOLCANIC ASH")
    if 'FC' in wx: conditions.append("FUNNEL CLOUD")
    if 'GR' in wx: conditions.append("HAIL")
    if 'WS' in wx: conditions.append("WIND SHEAR")
    if '+RA' in wx: conditions.append("HEAVY RAIN")
    
    if conditions:
        return True, " / ".join(sorted(list(set(conditions))))
    return False, "PIREP NOT REQUIRED"

def check_ifr_status(station_data):
    # 1. CEILING
    clouds = station_data.get('clouds', [])
    for layer in clouds:
        cover = layer.get('cover', '')
        base = get_cloud_base(layer)
        if cover in ['BKN', 'OVC', 'VV'] and base is not None and base < 1000:
            return True
            
    # 2. VISIBILITY
    vis = station_data.get('visib')
    if vis is not None:
        try:
            if isinstance(vis, str) and '+' in vis:
                 val = float(vis.replace('+', ''))
            else:
                 val = float(vis)
            if val < 3.0: return True
        except ValueError: pass
        
    return False

def parse_ddhhmm_from_text(raw_text):
    """
    Extracts timestamp from METAR text (e.g. '150354Z') and converts to ISO string.
    Uses current month/year, handling month boundary wrapping.
    """
    if not raw_text:
        return None
        
    # Regex for DDHHMMZ (e.g., 150354Z)
    match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_text)
    if not match:
        return None
        
    day, hour, minute = map(int, match.groups())
    
    now = datetime.now(timezone.utc)
    
    # Create a candidate date using current year/month
    try:
        candidate = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        # Handle invalid day (e.g. 31st in a 30-day month if clocks drift)
        return None

    # Handle month wrap-around logic
    # If candidate is far in the future (e.g. today is 1st, metar says 30th), it's last month
    if (candidate - now).days > 5: 
        # Move back one month
        if now.month == 1:
            candidate = candidate.replace(year=now.year - 1, month=12)
        else:
            prev_month = now.month - 1
            try:
                candidate = candidate.replace(month=prev_month)
            except ValueError:
                 pass 

    # If candidate is far in the past (e.g. today is 30th, metar says 1st), it's next month
    elif (now - candidate).days > 5:
        pass

    return candidate.isoformat()

def get_weather_data():
    main_results = []
    aux_results = []
    
    all_ids = [a['id'] for a in MAIN_AIRPORTS] + [a['id'] for a in AUX_AIRPORTS]
    id_string = ",".join(all_ids)

    print(f"\n[METAR] Fetching weather data for {len(all_ids)} stations...")

    try:
        url = "https://www.aviationweather.gov/api/data/metar"
        params = { "ids": id_string, "format": "json", "taf": "false", "hours": 2, "_": int(time.time()) }
        
        response = requests.get(url, params=params, timeout=10)
        json_data = response.json() if response.status_code == 200 else []
            
    except Exception as e:
        print(f"[METAR] Connection Failed: {e}")
        json_data = []

    # --- PROCESS MAIN AIRPORTS ---
    for airport in MAIN_AIRPORTS:
        code = airport["id"]
        station_reports = [item for item in json_data if item.get('icaoId') == code]
        station_reports.sort(key=lambda x: x.get('reportTime', ''), reverse=True)
        found = station_reports[0] if station_reports else None
        
        if found:
            is_needed, reason = check_pirep_condition(found)
            is_ifr = check_ifr_status(found)
            
            if is_ifr:
                is_needed = True
                if "IFR CONDITIONS" not in reason:
                    if reason == "PIREP NOT REQUIRED":
                        reason = "IFR CONDITIONS"
                    else:
                        reason = f"IFR CONDITIONS • {reason}"
            
            # --- FIX: FORCE PARSE TIME FROM TEXT ---
            raw_ob = found.get('rawOb', '')
            text_based_time = parse_ddhhmm_from_text(raw_ob)
            final_time = found.get('reportTime', '')
            
            if text_based_time:
                final_time = text_based_time
            
            main_results.append({
                "id": code, "name": airport["name"], "raw": raw_ob,
                "time": final_time, 
                "isoTime": final_time, # Parsing logic relies on this key in frontend
                "pirep_needed": is_needed, 
                "reason": reason, "is_ifr": is_ifr, "status": "online"
            })
        else:
            main_results.append({
                "id": code, "name": airport["name"], "raw": "WAITING FOR DATA...",
                "time": "", "isoTime": "", 
                "pirep_needed": False, "reason": "NO DATA", "is_ifr": False, "status": "offline"
            })

    # --- PROCESS AUX AIRPORTS ---
    for airport in AUX_AIRPORTS:
        code = airport["id"]
        station_reports = [item for item in json_data if item.get('icaoId') == code]
        station_reports.sort(key=lambda x: x.get('reportTime', ''), reverse=True)
        found = station_reports[0] if station_reports else None
        
        if found:
            is_needed, reason = check_pirep_condition(found)
            is_ifr = check_ifr_status(found)
            
            if is_ifr:
                is_needed = True
                if "IFR CONDITIONS" not in reason:
                    if reason == "PIREP NOT REQUIRED":
                        reason = "IFR CONDITIONS"
                    else:
                        reason = f"IFR CONDITIONS • {reason}"
            
            raw_ob = found.get('rawOb', '')
            text_based_time = parse_ddhhmm_from_text(raw_ob)
            final_time = found.get('reportTime', '')
            
            if text_based_time:
                final_time = text_based_time

            aux_results.append({ 
                "id": code, "name": airport["name"], "raw": raw_ob, 
                "time": final_time,
                "isoTime": final_time,
                "pirep_needed": is_needed, "reason": reason, "is_ifr": is_ifr, "status": "online"
            })
        else:
            aux_results.append({ 
                "id": code, "name": airport["name"], "raw": "NO DATA AVAILABLE", "time": "",
                "isoTime": "",
                "pirep_needed": False, "reason": "NO DATA", "is_ifr": False, "status": "offline"
            })

    return main_results, aux_results

@app.route('/')
def index():
    resp = make_response(render_template('CE_PIREP_INDEX.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp

@app.route('/api/metars')
def api_metars():
    m, a = get_weather_data()
    return jsonify(m)

# --- PIREP FETCHING LOGIC ---

def fetch_awc_pireps():
    """Fetch from US Aviation Weather Center"""
    reports = []
    try:
        url = "https://www.aviationweather.gov/api/data/aircraftreport"
        params = { "format": "json", "bbox": "8.0,139.0,19.0,150.0", "age": 2, "_": int(time.time()) }
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            raw = res.json()
            if isinstance(raw, list):
                for p in raw:
                    reports.append({
                        "raw": p.get('rawRep', ''),
                        "time": p.get('reportTime', ''),
                        "type": "UUA" if "UUA" in p.get('rawRep', '') else "UA",
                        "acft": p.get('aircraftId', 'UNK'),
                        "fl": f"FL{int(p.get('alt')/100)}" if p.get('alt') else "UNK",
                        "source": "AWC"
                    })
    except Exception as e:
        print(f"[AWC] Error: {e}")
    return reports

def parse_pirep_fields(raw_text):
    acft_str = "UNK"
    fl_str = "UNK"
    
    if not raw_text:
        return acft_str, fl_str
        
    tp_match = re.search(r'/TP\s+([A-Z0-9\-/]+)', raw_text.upper())
    if tp_match:
        acft_str = tp_match.group(1).split('/')[0]

    fl_match = re.search(r'/FL\s*([A-Z0-9]+)', raw_text.upper()) 
    if fl_match:
        val_str = fl_match.group(1)
        if "DURC" in val_str:
            fl_str = "DURING CLIMB"
        elif "DURD" in val_str:
            fl_str = "DURING DESCENT"
        elif val_str.isdigit():
            val = int(val_str)
            fl_str = f"FL{val:03d}"
        else:
            fl_str = val_str
            
    if fl_str == "UNK":
        if "DURC" in raw_text.upper():
            fl_str = "DURING CLIMB"
        elif "DURD" in raw_text.upper():
            fl_str = "DURING DESCENT"
    
    return acft_str, fl_str

def fetch_navcanada_pireps():
    reports = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://plan.navcanada.ca/wxrecall/'
    }

    url1 = "https://plan.navcanada.ca/weather/api/alpha/?site=PGUM&radius=300&alpha=pirep"
    try:
        print(f" > [NAVCAN] Trying Method 1 (Site): {url1}")
        res = requests.get(url1, headers=headers, timeout=5, verify=False)
        
        if res.status_code == 200:
            json_resp = res.json()
            data_list = []
            if isinstance(json_resp, dict):
                data_list = json_resp.get('data', [])
            elif isinstance(json_resp, list):
                data_list = json_resp
            
            for item in data_list:
                raw_text = item.get('text', '')
                if not raw_text: continue
                
                raw_time = item.get('startValidity') or item.get('date')
                if not raw_time:
                    raw_time = datetime.now(timezone.utc).isoformat()
                
                if raw_time and 'T' in raw_time and not raw_time.endswith('Z') and '+' not in raw_time:
                    raw_time += 'Z'

                acft, fl = parse_pirep_fields(raw_text)

                reports.append({
                    "raw": raw_text,
                    "time": raw_time, 
                    "type": "UUA" if "UUA" in raw_text else "UA",
                    "acft": acft, "fl": fl, "source": "NAVCAN"
                })
    except Exception as e:
        print(f"   [ERROR] Method 1 failed: {e}")

    return reports

def normalize_pirep_text(text):
    """Normalize raw text to help deduplicate between sources."""
    if not text: return ""
    text_upper = text.upper()
    
    match = re.search(r'\b(UA|UUA)\b', text_upper)
    if match:
        core_text = text_upper[match.start():]
    else:
        core_text = text_upper
        
    clean_key = re.sub(r'[^A-Z0-9]', '', core_text)
    
    return clean_key

@app.route('/api/data')
def api_data():
    main_metars, aux_metars = get_weather_data()
    
    print("--- FETCHING PIREPS ---")
    
    awc_data = fetch_awc_pireps()
    print(f" > [AWC] Found {len(awc_data)} reports.")
    
    nc_data = fetch_navcanada_pireps()
    print(f" > [NAVCAN] Found {len(nc_data)} reports.")
    
    # SMART MERGE (Deduplication)
    combined = {}
    for r in awc_data:
        key = normalize_pirep_text(r['raw'])
        combined[key] = r
    
    count_new_nc = 0
    for r in nc_data:
        key = normalize_pirep_text(r['raw'])
        if key not in combined:
            combined[key] = r
            count_new_nc += 1
            
    if count_new_nc > 0:
        print(f"   [MERGE] Added {count_new_nc} unique reports from Nav Canada.")
    
    filtered_pireps = []
    max_age_seconds = 65 * 60
    now_ts = time.time()
    
    for p in combined.values():
        try:
            t_str = p['time']
            if t_str.endswith('Z'):
                t_str = t_str.replace('Z', '+00:00')
            
            p_dt = datetime.fromisoformat(t_str)
            p_ts = p_dt.timestamp()
            
            if (now_ts - p_ts) <= max_age_seconds:
                filtered_pireps.append(p)
        except Exception as e:
            print(f"[FILTER ERROR] {e}")
            filtered_pireps.append(p)
            
    final_pireps = filtered_pireps
    final_pireps.sort(key=lambda x: x['time'], reverse=True)
    
    print(f" > [MERGE] Sending {len(final_pireps)} unique reports (younger than 65m) to display.")

    return jsonify({ "metars": main_metars, "aux_metars": aux_metars, "pireps": final_pireps })

if __name__ == '__main__':
    print("=======================================")
    print("   ZUA DEV SERVER (DUAL FEED ENABLED)  ")
    print("   Sources: AviationWeather.gov + NavCanada")
    print("=======================================")
    # Running on All Interfaces (0.0.0.0) at port 5003
    app.run(host='0.0.0.0', port=5003, debug=True, threaded=True)