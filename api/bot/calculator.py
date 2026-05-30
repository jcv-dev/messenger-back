import httpx
from django.conf import settings

CALCULATOR_BASE = settings.DOMII_CALCULATOR_URL


async def calculate_price(profile, segments, tools=None, payment_method="efectivo", acompanante=False):
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=15) as client:
        resp = await client.post("/api/calculate-price", json={
            "profile": profile,
            "segments": segments,
            "tools": tools or [],
            "payment_method": payment_method,
            "acompanante": acompanante,
        })
        resp.raise_for_status()
        return resp.json()


async def geocode_search(query: str):
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/geocode/search", params={"q": query})
        resp.raise_for_status()
        return resp.json()


async def geocode_details(place_id: str):
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/geocode/details", params={"place_id": place_id})
        resp.raise_for_status()
        return resp.json()


async def get_tools():
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/tools")
        resp.raise_for_status()
        return resp.json()


async def get_whatsapp_config():
    async with httpx.AsyncClient(base_url=CALCULATOR_BASE, timeout=10) as client:
        resp = await client.get("/api/config/whatsapp")
        resp.raise_for_status()
        return resp.json()
