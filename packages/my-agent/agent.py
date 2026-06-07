import asyncio
from playwright.async_api import async_playwright
import httpx
import chromadb
import json

db = chromadb.Client()
col = db.get_or_create_collection("agent_memory")

async def ask_ollama(prompt):
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post("http://localhost:11434/api/generate", json={
            "model": "qwen3.5-9b-agentic",
            "prompt": prompt,
            "stream": False
        })
        return r.json()["response"]

def get_memory(goal):
    try:
        r = col.query(query_texts=[goal], n_results=1)
        if r["documents"][0]:
            return r["documents"][0][0]
    except:
        pass
    return ""

def save_memory(goal, result):
    try:
        col.add(documents=[result], ids=[goal[:40].replace(" ", "_")])
    except:
        col.update(documents=[result], ids=[goal[:40].replace(" ", "_")])

async def browse(goal, url, page):
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(3000)
    text = await page.inner_text("body")
    text = text[:3000]
    past = get_memory(goal)
    memory_hint = f"\nPast session on this goal: {past}\n" if past else ""
    answer = await ask_ollama(f"Goal: {goal}{memory_hint}\n\nPage content:\n{text}\n\nComplete the goal based on the page content.")
    save_memory(goal, answer[:300])
    return answer

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"})

        print("=== Agentic Browser ===")
        print("Type 'quit' to exit\n")

        while True:
            goal = input("Goal: ").strip()
            if goal.lower() == "quit":
                break
            url = input("URL: ").strip()
            if not url.startswith("http"):
                url = "https://" + url
            print("\nBrowsing...\n")
            result = await browse(goal, url, page)
            print("\n=== RESULT ===")
            print(result)
            print("\n" + "="*40 + "\n")

        await browser.close()

asyncio.run(run())