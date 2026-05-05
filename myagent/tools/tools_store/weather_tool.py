"""
天气查询工具示例。
调用 wttr.in 免费 API，无需 API Key。
"""
import json
import urllib.request


async def query_weather(city: str = "Beijing") -> str:
    """查询指定城市的天气信息。

    Args:
        city: 城市名称（英文），如 Beijing, Shanghai, London
    """
    url = f"https://wttr.in/{city}?format=j1"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        current = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])[0]

        result = (
            f"🌍 城市: {area.get('areaName', [{}])[0].get('value', city)}\n"
            f"🌡️ 温度: {current.get('temp_C', 'N/A')}°C "
            f"(体感 {current.get('FeelsLikeC', 'N/A')}°C)\n"
            f"💧 湿度: {current.get('humidity', 'N/A')}%\n"
            f"🌬️ 风速: {current.get('windspeedKmph', 'N/A')} km/h\n"
            f"☁️ 天气: {current.get('weatherDesc', [{}])[0].get('value', 'N/A')}"
        )
        return result

    except Exception as e:
        return f"查询天气失败: {e}"