#!/usr/bin/env python3
"""
Thai Weather MCP Server using TMD (Thai Meteorological Department) API
"""

import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Annotated
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
import httpx
import dotenv
from pydantic import Field 
import re
import re
from datetime import datetime, timedelta, timezone

# Load environment variables
dotenv.load_dotenv()

TMD_BASE = "https://data.tmd.go.th/nwpapi/v1/forecast"
TMD_TOKEN = os.getenv("TMD_WEATHER_API_KEY")

# Create FastMCP server
mcp = FastMCP("thai-weather", stateless_http=True)

def _auth_headers() -> Dict[str, str]:
    if not TMD_TOKEN:
        raise ValueError("Missing TMD_TOKEN env var")
    return {
        "accept": "application/json",
        "authorization": f"Bearer {TMD_TOKEN}",
    }

async def _get_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    r = await client.get(url, headers=_auth_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def _thai_cond_label(code: int) -> str:
    """Convert weather condition code to Thai description"""
    labels = {
        1: "ท้องฟ้าแจ่มใส (Clear sky)",
        2: "มีเมฆบางส่วน (Partly cloudy)", 
        3: "เมฆเป็นส่วนมาก (Mostly cloudy)",
        4: "มีเมฆมาก (Very cloudy)",
        5: "ฝนเล็กน้อย (Light rain)", 
        6: "ฝนปานกลาง (Moderate rain)",
        7: "ฝนหนัก (Heavy rain)", 
        8: "ฝนฟ้าคะนอง (Thunderstorm)",
        9: "หนาวจัด (Very cold)", 
        10: "หนาว (Cold)",
        11: "เย็น (Cool)", 
        12: "ร้อนจัด (Very hot)",
    }
    return labels.get(int(code), f"Unknown condition ({code})")

THAI_ONLY = r'^[\u0E00-\u0E7F\s\(\)]+$'  # Thai Unicode block

@mcp.tool()
async def get_weather_by_province(
    province: Annotated[str, Field(pattern=THAI_ONLY, description="ชื่อจังหวัด (ภาษาไทยเท่านั้น) เช่น 'ขอนแก่น'")],
    duration: Annotated[int, Field(ge=1, le=7, description="จำนวนวัน 1-7")] = 1,
    date: Optional[str] = None
) -> str:
    """
    ดึงข้อมูลพยากรณ์อากาศสำหรับจังหวัดในประเทศไทย

    อาร์กิวเมนต์:
        province: ชื่อจังหวัด (ภาษาไทย เช่น 'ขอนแก่น')
        duration: จำนวนวันที่ต้องการพยากรณ์ (1-7, ค่าเริ่มต้น: 1)
        date: วันที่ในรูปแบบ YYYY-MM-DD (ค่าเริ่มต้น: วันนี้)
    """
    if re.search(r'[A-Za-z]', province):
        raise ToolError("โปรดส่งชื่อจังหวัดเป็นภาษาไทย เช่น 'ขอนแก่น' ไม่ใช่ 'Khon Kaen'")

    if not TMD_TOKEN:
        return "❌ TMD_WEATHER_API_KEY environment variable is not set. Please get your API key from https://data.tmd.go.th/"

    # Build API URL - using the location/daily/place endpoint
    url = f"{TMD_BASE}/location/daily/place"
    
    # Prepare query parameters
    params = {
        "province": province,
        "duration": min(duration, 7),  # Limit to reasonable duration
        "fields": "tc_min,tc_max,rh,cond,ws10m,wd10m"  # Temperature, humidity, condition, wind
    }
    
    if date:
        params["date"] = date
    
    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, url, params)
            
            # Handle the actual API response structure
            if "WeatherForecasts" not in data or not data["WeatherForecasts"]:
                return f"❌ No weather data available for province: {province}"
            
            # Process response - note the different structure from documentation
            weather_data = data["WeatherForecasts"][0]
            location_info = weather_data["location"]
            forecasts = weather_data["forecasts"]
            # Format response as JSON
            result_data = {
                "location": {
                    "name": location_info['name'],
                    "province": location_info['province'],
                    "region": location_info['region'],
                    "coordinates": {
                        "latitude": location_info['lat'],
                        "longitude": location_info['lon']
                    }
                },
                "forecasts": []
            }
            
            for i, forecast in enumerate(forecasts):
                forecast_date = forecast["time"][:10]  # Extract YYYY-MM-DD
                data_values = forecast["data"]
                
                forecast_data = {
                    "day": i + 1,
                    "date": forecast_date,
                    "temperature": {},
                    "humidity": None,
                    "condition": {},
                    "wind": {}
                }
                
                # Temperature
                if "tc_min" in data_values and "tc_max" in data_values:
                    forecast_data["temperature"] = {
                        "min": round(data_values['tc_min'], 1),
                        "max": round(data_values['tc_max'], 1),
                        "unit": "°C"
                    }
                elif "tc" in data_values:
                    forecast_data["temperature"] = {
                        "current": round(data_values['tc'], 1),
                        "unit": "°C"
                    }
                    
                # Humidity
                if "rh" in data_values:
                    forecast_data["humidity"] = {
                        "value": round(data_values['rh'], 1),
                        "unit": "%"
                    }
                    
                # Weather condition
                if "cond" in data_values:
                    condition_desc = _thai_cond_label(data_values['cond'])
                    forecast_data["condition"] = {
                        "code": data_values['cond'],
                        "description": condition_desc
                    }
                    
                # Wind
                wind_data = {}
                if "ws10m" in data_values:
                    wind_data["speed"] = {
                        "value": round(data_values['ws10m'], 1),
                        "unit": "m/s"
                    }
                    
                if "wd10m" in data_values:
                    wind_data["direction"] = {
                        "degrees": round(data_values['wd10m'], 1),
                        "unit": "°"
                    }
                
                if wind_data:
                    forecast_data["wind"] = wind_data
                
                result_data["forecasts"].append(forecast_data)
            
            import json
            return json.dumps(result_data, ensure_ascii=False, indent=2)
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return "❌ Invalid API key. Please check your TMD_WEATHER_API_KEY."
            elif e.response.status_code == 404:
                return f"❌ Province not found: {province}"
            else:
                error_text = e.response.text if hasattr(e.response, 'text') else str(e)
                return f"❌ API Error ({e.response.status_code}): {error_text}"
        except Exception as e:
            return f"❌ Error fetching weather data: {str(e)}"
@mcp.tool()
async def get_thai_provinces() -> str:
    """Get a list of major Thai provinces for weather lookup (JSON format)."""
    provinces_data = {
        "regions": {
            "central": {
                "name": "กลาง (Central)",
                "provinces": [
                    "กรุงเทพมหานคร", "สมุทรปราการ", "นนทบุรี", "ปทุมธานี", 
                    "นครปฐม", "สมุทรสาคร", "สมุทรสงคราม", "นครนายก", "ปราจีนบุรี"
                ]
            },
            "north": {
                "name": "เหนือ (North)",
                "provinces": [
                    "เชียงใหม่", "เชียงราย", "แม่ฮ่องสอน", "ลำปาง", "ลำพูน", 
                    "น่าน", "พะเยา", "แพร่", "อุตรดิตถ์", "ตาก"
                ]
            },
            "northeast": {
                "name": "อีสาน (Northeast)",
                "provinces": [
                    "ขอนแก่น", "นครราชสีมา", "อุดรธานี", "อุบลราชธานี", "บุรีรัมย์",
                    "สุรินทร์", "ศรีสะเกษ", "ยศธร", "ชัยภูมิ", "เลย", "สกลนคร"
                ]
            },
            "south": {
                "name": "ใต้ (South)",
                "provinces": [
                    "นครศรีธรรมราช", "สงขลา", "ภูเก็ต", "สุราษฎร์ธานี", "กระบี่",
                    "ชุมพร", "ตรัง", "พังงา", "ระนอง", "สตูล", "ยะลา", "ปัตตานี"
                ]
            },
            "east": {
                "name": "ตะวันออก (East)",
                "provinces": [
                    "ชลบุรี", "ระยอง", "จันทบุรี", "ตราด", "ฉะเชิงเทรา", "สระแก้ว"
                ]
            },
            "west": {
                "name": "ตะวันตก (West)",
                "provinces": [
                    "กาญจนบุรี", "เพชรบุรี", "ประจวบคีรีขันธ์", "ราชบุรี", "สุพรรณบุรี"
                ]
            }
        },
        "usage_examples": [
            {
                "function": "get_weather_by_province",
                "arguments": {"province": "ขอนแก่น"}
            },
            {
                "function": "get_weather_by_province", 
                "arguments": {"province": "กรุงเทพมหานคร", "duration": 3}
            },
            {
                "function": "get_weather_by_province",
                "arguments": {"province": "เชียงใหม่", "duration": 5, "date": "2024-01-15"}
            }
        ]
    }
    
    import json
    return json.dumps(provinces_data, ensure_ascii=False, indent=2)

BKK_TZ = timezone(timedelta(hours=7))
THAI_ONLY = r'^[\u0E00-\u0E7F\s\(\)]+$'
BKK_TZ = timezone(timedelta(hours=7))

def _is_english(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s or ""))

def _next_5pm_starttime(bkk_now: datetime) -> str:
    """Return 'YYYY-MM-DDT17:00:00' in Asia/Bangkok."""
    target = bkk_now if bkk_now.hour < 17 else (bkk_now + timedelta(days=1))
    return target.strftime("%Y-%m-%dT17:00:00")

@mcp.tool()
async def predict_weather_at_5pm(
    province: str,
    amphoe: str | None = None,
    tambon: str | None = None,
    starttime: str | None = None,
    duration: int = 1,  # Add this to accept the parameter
    date: str | None = None  # Add this to accept the parameter
) -> str:
    """
    ทำนายสภาพอากาศเวลา 17:00 น. แบบ 'พื้นที่' (หลายจุดกริด) สำหรับจังหวัด/อำเภอ/ตำบล
    - ใช้ /forecast/area/place (domain=2 เป็นค่าเริ่มต้น; 1 ชั่วโมงล่วงหน้า 72 ชม. ความละเอียด ~6 กม.)
    - หากขณะเรียก >17:00 จะเลื่อนเป็น 17:00 ของวันถัดไป
    
    Args:
        province: ชื่อจังหวัด (ภาษาไทย เช่น 'กรุงเทพมหานคร')
        amphoe: ชื่ออำเภอ (ไม่บังคับ)
        tambon: ชื่อตำบล (ไม่บังคับ)
        starttime: เวลาที่ต้องการ YYYY-MM-DDTHH:MM:SS (ไม่บังคับ)
        duration: จำนวนวัน (ไม่ใช้ในฟังก์ชันนี้แต่รับค่าได้)
        date: วันที่ YYYY-MM-DD (ใช้แทน starttime หากระบุ)
    """
    # --- Validate name must be Thai per TMD docs (province/amphoe/tambon in Thai only) ---
    if any(_is_english(x) for x in [province, amphoe, tambon] if x):
        raise ToolError("โปรดส่งชื่อสถานที่เป็นภาษาไทย เช่น 'กรุงเทพมหานคร' / 'บางกะปิ'")

    if not TMD_TOKEN:
        raise ToolError("ต้องตั้งค่า env TMD_WEATHER_API_KEY ก่อนใช้งาน")

    # --- Handle date parameter - convert to starttime if provided ---
    if date and not starttime:
        starttime = f"{date}T17:00:00"
    
    # --- Compute starttime (Thailand time) ---
    if not starttime:
        starttime = _next_5pm_starttime(datetime.now(BKK_TZ))

    url = f"{TMD_BASE}/area/place"
    domain = 2  # Define domain variable since it's used in error message
    params = {
        "domain": domain,
        "province": province,
        "starttime": starttime,
    }
    if amphoe: params["amphoe"] = amphoe
    if tambon: params["tambon"] = tambon

    try:
        async with httpx.AsyncClient() as client:
            data = await _get_json(client, url, params)  # Bearer + accept headers sent
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 401:
            raise ToolError("401: Access token ไม่ถูกต้องหรือหมดอายุ")
        if code == 422:
            raise ToolError("422: พารามิเตอร์ไม่ถูกต้อง โปรดตรวจสอบ domain/starttime/ชื่อสถานที่ (ภาษาไทย)")
        if code == 404:
            raise ToolError("404: ไม่พบพื้นที่ตามชื่อที่ระบุ")
        raise

    # TMD responses vary between 'weather_forecast' and 'WeatherForecasts'
    blocks = (data.get("weather_forecast")
              or data.get("WeatherForecasts")
              or data)
    if not isinstance(blocks, list) or not blocks:
        raise ToolError(f"ไม่มีข้อมูลที่ {province} เวลา {starttime} (domain={domain})")

    # Flatten forecasts at requested starttime (API returns many lat/lon points)
    points = []
    for b in blocks:
        loc = b.get("location", {})
        f_list = b.get("forecasts", [])
        if not f_list: 
            continue
        f0 = f_list[0]  # for starttime-only request, TMD returns a single hour
        v = f0.get("data", {})
        points.append({
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "time": f0.get("time"),
            "tc": v.get("tc"),
            "rh": v.get("rh"),
            "cond": v.get("cond"),
            "rain": v.get("rain"),
            "ws10m": v.get("ws10m"),
            "wd10m": v.get("wd10m"),
        })

    if not points:
        raise ToolError(f"ไม่พบข้อมูลกริดสำหรับ {province} เวลา {starttime}")

    # Aggregate
    def safe_vals(key): 
        return [p[key] for p in points if isinstance(p.get(key), (int, float))]
    import statistics as stats
    tc_vals, rh_vals = safe_vals("tc"), safe_vals("rh")
    avg_tc = (sum(tc_vals)/len(tc_vals)) if tc_vals else None
    avg_rh = (sum(rh_vals)/len(rh_vals)) if rh_vals else None
    max_tc = max(tc_vals) if tc_vals else None
    min_tc = min(tc_vals) if tc_vals else None

    # Condition distribution
    from collections import Counter
    cond_counts = Counter([p["cond"] for p in points if p.get("cond") is not None])
    total = sum(cond_counts.values()) or 1
    def cond_label(c): return _thai_cond_label(c) if c is not None else "—"
    top_cond = cond_counts.most_common(1)[0][0] if cond_counts else None

    # Compose summary
    hdr = f"🌇 17:00 น. แบบพื้นที่ | {province}" + (f" · {amphoe}" if amphoe else "") + (f" · {tambon}" if tambon else "")
    lines = [hdr, f"⏰ starttime={starttime} · domain={domain} (ชั่วโมงละ · ≥{len(points)} จุดกริด)"]

    if avg_tc is not None:
        lines.append(f"🌡️ ค่าเฉลี่ยทั้งเมือง ~ {avg_tc:.1f}°C (สูงสุด {max_tc:.1f}°C / ต่ำสุด {min_tc:.1f}°C)")
    if avg_rh is not None:
        lines.append(f"💧 RH เฉลี่ย ~ {avg_rh:.0f}%")

    if cond_counts:
        parts = [f"{cond_label(c)} {n/total:.0%}" for c, n in cond_counts.most_common(3)]
        lines.append("☁️ สภาพเด่น: " + " · ".join(parts))

    # Show 3 sample gridpoints (north/east/southwest-ish by lat/lon spread)
    samples = sorted(points, key=lambda p: (p["lat"], p["lon"]))[:: max(1, len(points)//3)][:3]
    for i, s in enumerate(samples, 1):
        bits = []
        if s.get("tc") is not None: bits.append(f"{s['tc']:.1f}°C")
        if s.get("rh") is not None: bits.append(f"RH {s['rh']:.0f}%")
        if s.get("cond") is not None: bits.append(cond_label(s["cond"]))
        lines.append(f"• จุดตัวอย่าง {i}: ({s['lat']:.4f}, {s['lon']:.4f}) → " + ", ".join(bits))

    # Guidance
    if any((p.get("rain") or 0) > 0 for p in points) or any((p.get("cond") or 0) in {5,6,7,8} for p in points):
        lines.append("✅ คำแนะนำ: พกร่ม/กันฝน อาจมีการจราจรล่าช้า")
    elif top_cond in {3,4}:
        lines.append("ℹ️ เมฆมาก แดดอ่อน เหมาะกับกิจกรรมนอกอาคารแบบสั้น")
    else:
        lines.append("🎯 อากาศโดยรวมเหมาะกับกิจกรรมกลางแจ้ง")

    return "\n".join(lines)

if __name__ == "__main__":
    print("🇹🇭 Starting Thai Weather MCP Server...")
    print("📋 Available tools:")
    print("  • get_weather_by_province(province, duration?, date?)")
    print("  • get_thai_provinces()")
    print("\n🔑 Using TMD_WEATHER_API_KEY from environment")
    print("🌐 API endpoint: https://data.tmd.go.th/")
    
    mcp.run(transport="http", host="127.0.0.1", port=9000)