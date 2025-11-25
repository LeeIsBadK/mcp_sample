#!/usr/bin/env python3
"""
Thai Weather MCP Server using TMD (Thai Meteorological Department) API
"""

import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Annotated
from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError
import httpx
import dotenv
from pydantic import Field 
import re
import json
from datetime import datetime, timedelta, timezone

# Load environment variables
dotenv.load_dotenv()

TMD_BASE = "https://data.tmd.go.th/nwpapi/v1/forecast"
TMD_TOKEN = os.getenv("TMD_WEATHER_API_KEY")

# Create FastMCP server
mcp = FastMCP("thai-weather", stateless_http=True)
# mcp = FastMCP("thai-weather")

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
    date: Optional[str] = None,
    ctx: Context = None
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
            await ctx.log(f"Fetching weather for {province} duration={duration} date={date}")
            await ctx.log(f"Request URL: {url} with params: {params}")
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


# ✅ Use a valid URI with a scheme
@mcp.resource(uri="memory://thai_provinces")
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
                    "สุรินทร์", "ศรีสะเกษ", "ยโสธร", "ชัยภูมิ", "เลย", "สกลนคร"
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
            {"function": "get_weather_by_province", "arguments": {"province": "ขอนแก่น"}},
            {"function": "get_weather_by_province", "arguments": {"province": "กรุงเทพมหานคร", "duration": 3}},
            {"function": "get_weather_by_province", "arguments": {"province": "เชียงใหม่", "duration": 5, "date": "2024-01-15"}}
        ]
    }
    # ✅ Return JSON-encoded string (UTF-8 safe)
    return json.dumps(provinces_data, ensure_ascii=False)


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
    date: str | None = None,  # Add this to accept the parameter
    ctx: Context = None
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
        if ctx:
            await ctx.log(f"Fetching 5PM weather for {province}{f', {amphoe}' if amphoe else ''}{f', {tambon}' if tambon else ''}")
            await ctx.log(f"Request URL: {url} with params: {params}")
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
    
    if ctx:
        await ctx.log(f"Found {len(points)} grid points for {province} at {starttime}")

    # Aggregate statistics
    def safe_vals(key): 
        return [p[key] for p in points if isinstance(p.get(key), (int, float))]
    
    tc_vals, rh_vals = safe_vals("tc"), safe_vals("rh")
    rain_vals = safe_vals("rain")
    
    # Calculate temperature statistics
    temperature_stats = {}
    if tc_vals:
        temperature_stats = {
            "average": round(sum(tc_vals) / len(tc_vals), 1),
            "max": round(max(tc_vals), 1),
            "min": round(min(tc_vals), 1),
            "unit": "°C"
        }
    
    # Calculate humidity statistics
    humidity_stats = {}
    if rh_vals:
        humidity_stats = {
            "average": round(sum(rh_vals) / len(rh_vals), 1),
            "unit": "%"
        }
    
    # Calculate rainfall statistics
    rainfall_stats = {}
    if rain_vals:
        rainfall_stats = {
            "average": round(sum(rain_vals) / len(rain_vals), 1),
            "max": round(max(rain_vals), 1),
            "unit": "mm"
        }

    # Condition distribution using only _thai_cond_label
    from collections import Counter
    cond_counts = Counter([p["cond"] for p in points if p.get("cond") is not None])
    total = sum(cond_counts.values()) or 1
    
    conditions = []
    for cond_code, count in cond_counts.most_common():
        condition_info = {
            "code": cond_code,
            "description": _thai_cond_label(cond_code),
            "percentage": round((count / total) * 100, 1),
            "grid_points": count
        }
        conditions.append(condition_info)

    # Sample grid points (factual data only)
    samples = sorted(points, key=lambda p: (p["lat"], p["lon"]))[:: max(1, len(points)//3)][:3]
    sample_points = []
    for i, s in enumerate(samples, 1):
        sample_data = {
            "point_id": i,
            "coordinates": {
                "latitude": round(s["lat"], 4) if s.get("lat") is not None else None,
                "longitude": round(s["lon"], 4) if s.get("lon") is not None else None
            },
            "time": s.get("time"),
            "data": {}
        }
        
        if s.get("tc") is not None:
            sample_data["data"]["temperature"] = {
                "value": round(s["tc"], 1),
                "unit": "°C"
            }
        if s.get("rh") is not None:
            sample_data["data"]["humidity"] = {
                "value": round(s["rh"], 1),
                "unit": "%"
            }
        if s.get("cond") is not None:
            sample_data["data"]["condition"] = {
                "code": s["cond"],
                "description": _thai_cond_label(s["cond"])
            }
        if s.get("rain") is not None:
            sample_data["data"]["rainfall"] = {
                "value": round(s["rain"], 1),
                "unit": "mm"
            }
        if s.get("ws10m") is not None:
            sample_data["data"]["wind_speed"] = {
                "value": round(s["ws10m"], 1),
                "unit": "m/s"
            }
        if s.get("wd10m") is not None:
            sample_data["data"]["wind_direction"] = {
                "value": round(s["wd10m"], 1),
                "unit": "degrees"
            }
        
        sample_points.append(sample_data)

    # Build final JSON response
    if ctx:
        await ctx.log(f"Generating weather prediction response for {province} with {len(conditions)} conditions")
    
    result = {
        "location": {
            "province": province,
            "amphoe": amphoe,
            "tambon": tambon
        },
        "forecast": {
            "time": starttime,
            "domain": domain,
            "grid_points_total": len(points)
        },
        "statistics": {
            "temperature": temperature_stats if temperature_stats else None,
            "humidity": humidity_stats if humidity_stats else None,
            "rainfall": rainfall_stats if rainfall_stats else None
        },
        "conditions": conditions,
        "sample_grid_points": sample_points,
        "metadata": {
            "api_endpoint": f"{TMD_BASE}/area/place",
            "generated_at": datetime.now(BKK_TZ).isoformat()
        }
    }

    json_response = json.dumps(result, ensure_ascii=False, indent=2)
    
    if ctx:
        await ctx.log(f"Weather prediction complete for {province} at {starttime}")
        
    return json_response

SYSTEM_PROMPT_TEMPLATE = """
คุณคือโปรแกรมจำลองผู้ใช้ (User Simulator) ที่มีจุดประสงค์เพื่อสร้างคำค้นหาของผู้ใช้ที่เป็นธรรมชาติ ซึ่งสะท้อนถึงวิธีที่บุคคลที่อธิบายไว้จะสื่อสารกับผู้ช่วย AI งานของคุณคือการสร้างข้อความที่สมจริงและตรงตามลักษณะของตัวละคร ซึ่งจะนำไปสู่ผลลัพธ์ที่สามารถตรวจสอบได้และไม่คลุมเครือ โดยไม่ระบุอย่างชัดเจนจนเกินไป

**ข้อกำหนดหลัก**
*   สร้างข้อความที่ตรงตามสิ่งที่บุคคลที่อธิบายใน `<user_profile>` จะพูดเมื่อพยายามบรรลุเป้าหมาย `<objective>` ภายใต้สถานการณ์ `<scenario>` ที่กำหนด
*   คำค้นหาที่สร้างขึ้น **ต้อง** สามารถแก้ไขได้อย่างสมบูรณ์โดย `<selected_tools>` — นี่คือข้อกำหนดที่สำคัญมาก
*   นี่คือการโต้ตอบแบบครั้งเดียว (one-turn interaction) — สร้าง **ข้อความเดียว** ที่รวมคำขอทั้งหมดของผู้ใช้
*   รวมข้อมูลทั้งหมดที่จำเป็นสำหรับการทำงานให้เสร็จสมบูรณ์โดยไม่ต้องมีคำถามเพิ่มเติม
*   จัดโครงสร้างคำขอให้เป็นไปตามรูปแบบหมวดหมู่ที่ระบุอย่างเป็นธรรมชาติ โดยไม่ต้องกล่าวถึงรูปแบบนั้นอย่างชัดเจน

**หมวดหมู่สถานการณ์และการใช้งานเครื่องมือ**
คำค้นหาแต่ละรายการต้องสอดคล้องกับรูปแบบการโต้ตอบเหล่านี้อย่างแม่นยำ:
*   **single_server_single_call:** ใช้เครื่องมือ 1 อย่างจากเซิร์ฟเวอร์ 1 ตัวด้วยการเรียกเพียงครั้งเดียว
*   **single_server_parallel_call:** ใช้เครื่องมือหลายอย่างหรือเรียกใช้เครื่องมือเดียวกันหลายครั้งจากเซิร์ฟเวอร์ 1 ตัวโดยไม่มีการพึ่งพาระหว่างการเรียก
*   **single_server_sequential_call:** ใช้เครื่องมือหลายอย่างจากเซิร์ฟเวอร์ 1 ตัวโดยมีการพึ่งพาระหว่างการเรียก ซึ่งต้องมีการจัดลำดับที่เฉพาะเจาะจง
*   **multi_server_single_call:** เลือกเครื่องมือ **เพียง 1 อย่าง** จากเซิร์ฟเวอร์หลายตัวที่พร้อมใช้งานและทำการเรียกเพียงครั้งเดียว (ไม่เลือกหลายเครื่องมือ)
*   **multi_server_parallel_call:** ใช้เครื่องมือหลายอย่างจากเซิร์ฟเวอร์หลายตัวโดยไม่มีการพึ่งพาระหว่างการเรียก
*   **multi_server_sequential_call:** ใช้เครื่องมือหลายอย่างจากเซิร์ฟเวอร์หลายตัวโดยมีการพึ่งพาระหว่างการเรียก ซึ่งต้องมีการจัดลำดับที่เฉพาะเจาะจง

สำหรับงานแบบตามลำดับ (sequential tasks) ให้ตรวจสอบว่าคำขอของผู้ใช้บ่งบอกถึงการพึ่งพาที่จำเป็นซึ่งจะต้องมีขั้นตอนในการดำเนินการตามลำดับ

**ความถูกต้องของตัวละคร**
ตรวจสอบให้แน่ใจว่าข้อความเป็นไปตามลักษณะของตัวละครอย่างสมบูรณ์ โดยสะท้อนถึง:
*   คำศัพท์ทางเทคนิคและความเชี่ยวชาญในสาขาที่เหมาะสมกับโปรไฟล์ผู้ใช้
*   รูปแบบการสื่อสาร (ทางการ/ไม่เป็นทางการ, ละเอียด/กระชับ)
*   สภาวะทางอารมณ์ภายใต้ความกดดันของสถานการณ์
*   โครงสร้างประโยคและทางเลือกคำพูดที่เป็นปกติ
*   การแสดงความต้องการตามธรรมชาติโดยไม่มีการจัดรูปแบบที่ประดิษฐ์ขึ้น
*   ระดับความแม่นยำที่เหมาะสม (เช่น วิศวกรซอฟต์แวร์อาจใช้คำศัพท์ที่แม่นยำกว่าผู้ใช้ทั่วไป)

**ความสมบูรณ์ของข้อมูล**
ข้อมูลทั้งหมดที่จำเป็นในการดำเนินการตามคำขอของผู้ใช้สามารถมาจาก:
1.  ข้อมูลที่แสดงออกตามธรรมชาติในคำขอเริ่มต้นของผู้ใช้
2.  อนุมานได้อย่างสมเหตุสมผลจากบริบทที่ให้ไว้ในคำขอเริ่มต้น
3.  ความรู้ทั่วไปที่คาดว่าจะจำเป็นสำหรับงาน
4.  ได้มาจากผลลัพธ์ของเครื่องมือในสถานการณ์เดียวกัน (สำหรับงานหลายขั้นตอน)

ในการให้ข้อมูล:
*   แสดงข้อมูลในแบบที่ผู้ใช้คนนี้จะแสดงออกอย่างเป็นธรรมชาติ ไม่ใช่ในรูปแบบทางเทคนิค
*   ใช้ภาษาในชีวิตประจำวันสำหรับเอนทิตี, สถานที่, เวลา และวันที่
*   ให้บริบทที่เพียงพอสำหรับข้อมูลที่จำเป็นทั้งหมดเพื่อให้สามารถระบุได้อย่างชัดเจน

**ข้อพิจารณาเรื่องความอ่อนไหวต่อเวลา**
เมื่อข้อมูลเชิงเวลามีความเกี่ยวข้อง:
*   รวมบริบทเชิงเวลาที่เฉพาะเจาะจง (วันที่, วันในสัปดาห์, เวลาของวัน) ตามความจำเป็น
*   แสดงข้อมูลที่เกี่ยวข้องกับเวลาตามธรรมชาติในแบบที่ผู้ใช้จะใช้ (เช่น "สุดสัปดาห์นี้" เทียบกับ "24-25 เมษายน 2025")
*   สำหรับคำขอเร่งด่วน ให้สะท้อนความกดดันด้านเวลาที่เหมาะสมในภาษาของผู้ใช้
*   อ้างอิงปัจจัยตามฤดูกาล, วันหยุด หรือเหตุการณ์ที่เกี่ยวข้อง
*   รวมกำหนดเวลาหรือข้อจำกัดด้านเวลาหากมีความสำคัญต่องาน
*   ตรวจสอบให้แน่ใจว่าการอ้างอิงเวลาไม่คลุมเครือด้วยบริบทที่เพียงพอ
*   สำหรับงานการจัดตารางเวลา ให้รวมขอบเขตเวลาที่ชัดเจน (เวลาเริ่มต้น/สิ้นสุด, ระยะเวลา)
*   สำหรับเหตุการณ์ที่เกิดซ้ำ ให้ระบุรูปแบบความถี่ตามธรรมชาติ

**แนวทางและความจำกัดด้านความถูกต้อง**
*   **ห้าม** กล่าวถึงเครื่องมือ AI หรือแนะนำว่าควรใช้เครื่องมือใด
*   **ห้าม** แนะนำตัวเองโดยไม่จำเป็น เว้นแต่จะเป็นธรรมชาติสำหรับตัวละครนี้
*   **ห้าม** อธิบายอย่างชัดเจนว่าอะไรคือความสำเร็จ
*   **ห้าม** รวมข้อมูลเมตาใดๆ เกี่ยวกับผู้ใช้หรือกระบวนการจำลอง
*   เน้นทั้งหมดไปที่ **สิ่งที่** ผู้ใช้ต้องการ ไม่ใช่ **วิธีที่** ผู้ช่วยควรทำ
*   รวมบริบทและข้อจำกัดในลักษณะที่เป็นธรรมชาติและเป็นบทสนทนา
*   หลีกเลี่ยงการขอคุณสมบัติหรือข้อมูลที่ต้องใช้เครื่องมือเกินกว่าที่ให้ไว้

**รูปแบบผลลัพธ์**
เขียนเฉพาะสิ่งที่ผู้ใช้จะพูดหรือพิมพ์จริงๆ โดยไม่มีคำอธิบาย คำแนะนำ หรือข้อคิดเห็นเมตาใดๆ ผลลัพธ์ของคุณควรเป็นคำค้นหาในภาษาธรรมชาติที่:
1.  ครอบคลุมทุกด้านของวัตถุประสงค์ของผู้ใช้
2.  ตรงกับวิธีที่บุคคลนี้จะสื่อสารอย่างเป็นธรรมชาติ
3.  มีข้อมูลเพียงพอสำหรับการทำงานให้เสร็จสมบูรณ์อย่างเด็ดขาด
4.  ยังคงความเป็นธรรมชาติและเป็นบทสนทนา แทนที่จะจัดโครงสร้างเป็นคำขอทางเทคนิค
5.  นำไปสู่รูปแบบการใช้งานเครื่องมือที่ระบุโดยหมวดหมู่ที่เลือกอย่างเป็นธรรมชาติ
6.  สามารถจัดการได้อย่างสมบูรณ์และมีประสิทธิภาพโดยใช้ `<selected_tools>` เท่านั้น
7.  รวมบริบทเชิงเวลาที่เหมาะสมเมื่อคำค้นหาอ่อนไหวต่อเวลา
""".strip()

INTERACTION_PATTERN = r"^(single_server_single_call|single_server_parallel_call|single_server_sequential_call|multi_server_single_call|multi_server_parallel_call|multi_server_sequential_call)$"

@mcp.prompt()
async def task_gen_prompt(
    user_profile: Annotated[str, Field(description="คำอธิบายโปรไฟล์ผู้ใช้/ตัวละครแบบย่อ")],
    objective: Annotated[str, Field(description="เป้าหมายของผู้ใช้ในคำขอครั้งนี้")],
    scenario: Annotated[str, Field(description="บริบท/สถานการณ์ที่ผู้ใช้กำลังเผชิญ")],
    selected_tools: Annotated[str | List[str], Field(description="รายชื่อเครื่องมือที่ 'อนุญาตให้ใช้ได้' สำหรับงานนี้ (เช่น JSON array หรือข้อความคั่นด้วย comma)")] ,
    interaction: Annotated[str, Field(pattern=INTERACTION_PATTERN, description="หมวดหมู่การใช้งานเครื่องมือตามที่กำหนดในพรอมป์")] = "single_server_single_call",
    time_context: Annotated[Optional[str], Field(description="ข้อมูลเวลาเพิ่มเติม เช่น 'สุดสัปดาห์นี้', 'ภายใน 3 ชม.' หรือ '2025-10-05'")] = None,
    locale: Annotated[str, Field(pattern=r"^(th|en)$", description="ภาษาเอาต์พุตของ user simulator (th/en)")] = "th",
) -> str:
    """
    คืนค่า System Prompt สำหรับ User Simulator โดยฝังพารามิเตอร์ลงในแท็ก:
    <user_profile>, <objective>, <scenario>, <selected_tools>, <interaction>, <time_context?>, <locale>
    """
    # --- normalize tools ---
    tools_list: List[str]
    if isinstance(selected_tools, list):
        tools_list = [str(t).strip() for t in selected_tools if str(t).strip()]
    else:
        # รับได้ทั้ง JSON array หรือ comma-separated
        try:
            parsed = json.loads(selected_tools)
            if isinstance(parsed, list):
                tools_list = [str(t).strip() for t in parsed if str(t).strip()]
            else:
                # ตีความเป็น comma-separated
                tools_list = [s.strip() for s in str(selected_tools).split(",") if s.strip()]
        except Exception:
            tools_list = [s.strip() for s in str(selected_tools).split(",") if s.strip()]

    if not tools_list:
        raise ToolError("selected_tools ว่างเปล่า — โปรดระบุอย่างน้อย 1 เครื่องมือที่อนุญาตให้ใช้")

    # --- build sections ---
    tools_json = json.dumps(tools_list, ensure_ascii=False)
    time_block = f"\n<time_context>\n{time_context}\n</time_context>" if time_context else ""

    # --- language hint (ไม่บังคับ แต่ช่วย model ชัดเจนขึ้น) ---
    locale_hint = "ภาษาไทย" if locale == "th" else "English"

    # --- final prompt ---
    prompt = (
        SYSTEM_PROMPT_TEMPLATE
        + f"\n\n<!-- Locale hint: {locale_hint} -->\n"
        + "<user_profile>\n" + user_profile.strip() + "\n</user_profile>\n\n"
        + "<objective>\n" + objective.strip() + "\n</objective>\n\n"
        + "<scenario>\n" + scenario.strip() + "\n</scenario>\n\n"
        + "<selected_tools>\n" + tools_json + "\n</selected_tools>\n\n"
        + "<interaction>\n" + interaction + "\n</interaction>"
        + time_block
        + f"\n<locale>\n{locale}\n</locale>\n"
    ).strip()

    return prompt
# mcp Resource Templates
# ------------------------------

#  
if __name__ == "__main__":
    print("🇹🇭 Starting Thai Weather MCP Server...")
    print("📋 Available tools:")
    print("  • get_weather_by_province(province, duration?, date?)")
    print("  • get_thai_provinces()")
    print("\n🔑 Using TMD_WEATHER_API_KEY from environment")
    print("🌐 API endpoint: https://data.tmd.go.th/")
    
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)