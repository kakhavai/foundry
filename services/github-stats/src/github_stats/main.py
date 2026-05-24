import httpx
from fastapi import FastAPI, HTTPException

from .github import get_events, get_stats_data

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/activity/{username}")
async def activity(username: str):
    async with httpx.AsyncClient() as client:
        try:
            events = await get_events(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return {"username": username, "events": events}


@app.get("/stats/{username}")
async def stats(username: str):
    async with httpx.AsyncClient() as client:
        try:
            data = await get_stats_data(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return data
