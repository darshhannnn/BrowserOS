"""
AgentBrowser backend — Flask + Playwright + Ollama + ChromaDB
=============================================================
Architecture:
 - Flask runs with threads=True so /models and other routes respond instantly.
 - Playwright MUST run on one dedicated thread (sync API is not thread-safe).
   We use a single BrowserThread with a queue to forward work from Flask threads.
 - ChromaDB PersistentClient survives server restarts.
"""

import os
import sys
import atexit
import queue
import threading
from flask import Flask, request, jsonify, send_from_directory
import httpx
import chromadb
from playwright.sync_api import sync_playwright

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "chroma_db")

# ──────────────────────────────────────────────
# Flask app
# ──────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────
# ChromaDB — persistent, process-global
# ──────────────────────────────────────────────
_db = chromadb.PersistentClient(path=DB_PATH)
_col = _db.get_or_create_collection("agent_memory")


# ──────────────────────────────────────────────
# Playwright — single dedicated thread + queue
# ──────────────────────────────────────────────
_browser_queue: queue.Queue = queue.Queue()
_browser_thread: threading.Thread | None = None


def _browser_worker():
    """Runs forever on its own thread, owns the single Playwright browser."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)

    def new_page():
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            )
        )
        return ctx.new_page()

    # Use a list so inner closures can mutate the reference
    page_ref = [new_page()]

    def page_alive():
        try:
            page_ref[0].title()
            return True
        except Exception:
            return False

    while True:
        try:
            task = _browser_queue.get(timeout=1)
        except queue.Empty:
            continue

        if task is None:          # sentinel — shutdown
            break

        fn, result_box = task
        try:
            # Resurrect page if browser/context was closed
            if not page_alive():
                try:
                    page_ref[0].close()
                except Exception:
                    pass
                page_ref[0] = new_page()
            result_box["result"] = fn(page_ref[0])
        except Exception as exc:
            result_box["error"] = exc
        finally:
            _browser_queue.task_done()

    try:
        browser.close()
        pw.stop()
    except Exception:
        pass


def _start_browser_thread():
    global _browser_thread
    t = threading.Thread(target=_browser_worker, daemon=True, name="playwright-worker")
    t.start()
    _browser_thread = t


def run_on_browser(fn, timeout=90):
    """
    Submit fn(page) → result to the browser worker thread.
    Blocks the calling Flask thread until done or timeout.
    """
    result_box = {}
    evt = threading.Event()

    def wrapped(page):
        r = fn(page)
        evt.set()
        return r

    _browser_queue.put((wrapped, result_box))
    evt.wait(timeout=timeout)

    if "error" in result_box:
        raise result_box["error"]
    if "result" not in result_box:
        raise TimeoutError("Browser operation timed out")
    return result_box["result"]


def _shutdown_browser():
    _browser_queue.put(None)          # send sentinel


atexit.register(_shutdown_browser)


# ──────────────────────────────────────────────
# Ollama
# ──────────────────────────────────────────────
def ask_ollama(prompt: str, model: str = "qwen3.5-9b-agentic") -> str:
    try:
        r = httpx.post(
            "http://localhost:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["response"]
    except httpx.TimeoutException:
        return "⚠️ Ollama timed out. The model may still be loading — try again."
    except httpx.HTTPStatusError as e:
        return f"⚠️ Ollama HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return f"⚠️ Ollama error: {e}"


# ──────────────────────────────────────────────
# Memory helpers
# ──────────────────────────────────────────────
def get_memory(goal: str) -> str:
    try:
        r = _col.query(query_texts=[goal], n_results=1)
        docs = r.get("documents", [[]])[0]
        if docs:
            return docs[0]
    except Exception:
        pass
    return ""


def save_memory(goal: str, result: str):
    safe_id = (
        goal[:60]
        .replace(" ", "_")
        .replace("/", "-")
        .replace("?", "")
        .replace("!", "")
        .replace(":", "")
    )
    try:
        _col.add(documents=[result], ids=[safe_id])
    except Exception as e:
        if "already exists" in str(e) or "duplicate" in str(e).lower():
            try:
                _col.update(documents=[result], ids=[safe_id])
            except Exception as ue:
                print(f"[memory] update failed: {ue}", file=sys.stderr)
        else:
            print(f"[memory] add failed: {e}", file=sys.stderr)


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/models", methods=["GET"])
def list_models():
    """Return locally-installed Ollama models (size > 10 KB = real models)."""
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=5)
        models = [
            m["name"]
            for m in r.json().get("models", [])
            if m.get("size", 0) > 10_000
        ]
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": ["qwen3.5-9b-agentic"], "error": str(e)})


@app.route("/run", methods=["POST"])
def run():
    data = request.json or {}
    goal = data.get("goal", "").strip()
    url  = data.get("url",  "").strip()
    model = data.get("model", "qwen3.5-9b-agentic")

    if not goal:
        return jsonify({"error": "Goal is required"}), 400

    if url and not url.startswith("http"):
        url = "https://" + url

    nav_error = None
    text = ""
    visited_url = ""

    # ── Browser step (runs on the dedicated Playwright thread) ──
    def browser_task(page):
        nonlocal nav_error, visited_url
        if url:
            try:
                page.goto(url, timeout=60_000)
                page.wait_for_timeout(3000)
            except Exception as e:
                nav_error = str(e)
        visited_url = page.url
        try:
            return page.inner_text("body")[:4000]
        except Exception:
            return "(Could not extract page text)"

    try:
        text = run_on_browser(browser_task, timeout=75)
    except TimeoutError:
        text = "(Browser task timed out)"
        nav_error = "Browser operation timed out after 75 s"
    except Exception as e:
        text = "(Browser error)"
        nav_error = str(e)

    # ── Memory recall ──
    past = get_memory(goal)
    memory_hit = bool(past)
    hint = f"\n\n[Past session on this goal]:\n{past}\n" if past else ""

    # ── Ollama ──
    prompt = (
        f"Goal: {goal}{hint}\n\n"
        f"Current page content:\n{text}\n\n"
        f"Complete the goal based on the page content. Be concise and direct."
    )
    answer = ask_ollama(prompt, model=model)

    # ── Save memory ──
    save_memory(goal, answer[:400])

    response: dict = {
        "result": answer,
        "memory_hit": memory_hit,
        "memory_snippet": past[:120] if past else "",
        "url_visited": visited_url or url,
    }
    if nav_error:
        response["nav_warning"] = nav_error

    return jsonify(response)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print(f"ChromaDB store : {DB_PATH}")
    print("Starting Playwright browser thread…")
    _start_browser_thread()
    # threaded=True is safe now because Playwright work goes through the queue
    app.run(port=5000, threaded=True)