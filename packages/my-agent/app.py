"""
AgentBrowser backend — Flask + Playwright + Ollama (streaming) + ChromaDB
=========================================================================
Speed design:
 - Ollama responses are STREAMED via SSE so the UI shows tokens as they arrive.
 - Browser navigation and memory lookup run concurrently (threads).
 - Page text extraction is smart: strips scripts/styles, caps at 3 000 chars.
 - Browser wait reduced to domcontentloaded + 1.5s settle (was 3s).
 - Ollama keep-alive ping on startup to pre-warm the model in VRAM.
 - Single persistent Playwright browser (context reused per request).
"""

import os, sys, atexit, queue, threading, json, time
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
import httpx
import chromadb
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH        = os.path.join(os.path.dirname(__file__), "chroma_db")
OLLAMA_BASE    = "http://localhost:11434"
PAGE_TEXT_LIMIT = 3_000   # chars sent to LLM
NAV_TIMEOUT    = 30_000   # ms — browser navigation timeout
SETTLE_MS      = 1_500    # ms — wait after page load (was 3 000)
LLM_TIMEOUT    = 120      # seconds — max time for Ollama to respond

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── ChromaDB ──────────────────────────────────────────────────────────────────
_db  = chromadb.PersistentClient(path=DB_PATH)
_col = _db.get_or_create_collection("agent_memory")


# ── Memory helpers ────────────────────────────────────────────────────────────
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
        .replace(" ", "_").replace("/", "-")
        .replace("?", "").replace("!", "").replace(":", "")
    )
    try:
        _col.add(documents=[result], ids=[safe_id])
    except Exception as e:
        if "already exists" in str(e) or "duplicate" in str(e).lower():
            try:
                _col.update(documents=[result], ids=[safe_id])
            except Exception:
                pass


# ── Playwright — single dedicated thread + queue ───────────────────────────────
_browser_queue: queue.Queue = queue.Queue()
_browser_thread = None


def _smart_extract(page) -> str:
    """Extract meaningful text — strip script/style nodes, cap length."""
    try:
        # Remove noise nodes first
        page.evaluate("""
            document.querySelectorAll(
                'script,style,noscript,nav,footer,header,[role=banner],[role=navigation]'
            ).forEach(el => el.remove())
        """)
    except Exception:
        pass
    try:
        text = page.inner_text("body")
        # Collapse excessive whitespace
        import re
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text[:PAGE_TEXT_LIMIT]
    except Exception:
        return "(Could not extract page text)"


def _browser_worker():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)  # headless = faster startup

    def new_context():
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        return ctx.new_page()

    page_ref = [new_context()]

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
        if task is None:
            break
        fn, result_box, done_evt = task
        try:
            if not page_alive():
                try:
                    page_ref[0].close()
                except Exception:
                    pass
                page_ref[0] = new_context()
            result_box["result"] = fn(page_ref[0])
        except Exception as exc:
            result_box["error"] = exc
        finally:
            _browser_queue.task_done()
            done_evt.set()

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


def run_on_browser(fn, timeout=60):
    result_box = {}
    done_evt = threading.Event()
    _browser_queue.put((fn, result_box, done_evt))
    done_evt.wait(timeout=timeout)
    if "error" in result_box:
        raise result_box["error"]
    if "result" not in result_box:
        raise TimeoutError("Browser operation timed out")
    return result_box["result"]


def _shutdown_browser():
    _browser_queue.put(None)


atexit.register(_shutdown_browser)


# ── Ollama helpers ─────────────────────────────────────────────────────────────
def list_ollama_models():
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=4)
        return [m["name"] for m in r.json().get("models", []) if m.get("size", 0) > 10_000]
    except Exception:
        return []


def warmup_model(model: str):
    """Send a tiny request to load the model into VRAM (non-blocking)."""
    def _ping():
        try:
            httpx.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1}},
                timeout=30,
            )
        except Exception:
            pass
    threading.Thread(target=_ping, daemon=True, name="ollama-warmup").start()


def stream_ollama(prompt: str, model: str):
    """Generator: yields text chunks from Ollama streaming API."""
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            with client.stream(
                "POST",
                f"{OLLAMA_BASE}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": True,
                    "options": {
                        "temperature": 0.3,   # lower = faster + more focused
                        "num_predict": 512,    # cap output length for speed
                    },
                },
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        if token:
                            yield token
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue
    except httpx.TimeoutException:
        yield "\n\n⚠️ Ollama timed out — the model may still be loading."
    except Exception as e:
        yield f"\n\n⚠️ Ollama error: {e}"


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/models", methods=["GET"])
def get_models():
    models = list_ollama_models()
    return jsonify({"models": models or ["qwen3.5-9b-agentic"]})


@app.route("/warmup", methods=["POST"])
def warmup():
    """Pre-warm a model so first inference is fast."""
    model = (request.json or {}).get("model", "qwen3.5-9b-agentic")
    warmup_model(model)
    return jsonify({"status": "warming up", "model": model})


@app.route("/run", methods=["POST"])
def run():
    data  = request.json or {}
    goal  = data.get("goal", "").strip()
    url   = data.get("url",  "").strip()
    model = data.get("model", "qwen3.5-9b-agentic")

    if not goal:
        return jsonify({"error": "Goal is required"}), 400
    if url and not url.startswith("http"):
        url = "https://" + url

    # ── Phase 1: browser + memory IN PARALLEL ──────────────────────────────────
    nav_error  = None
    page_text  = ""
    visited_url = ""
    past_memory = ""

    memory_done = threading.Event()
    memory_box  = {}

    def fetch_memory():
        memory_box["past"] = get_memory(goal)
        memory_done.set()

    mem_thread = threading.Thread(target=fetch_memory, daemon=True)
    mem_thread.start()

    def browser_task(page):
        nonlocal nav_error, visited_url
        if url:
            try:
                page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(SETTLE_MS)
            except Exception as e:
                nav_error = str(e)
        visited_url = page.url
        return _smart_extract(page)

    try:
        page_text = run_on_browser(browser_task, timeout=45)
    except TimeoutError:
        page_text = "(Browser timed out)"
        nav_error = "Browser timed out"
    except Exception as e:
        page_text = "(Browser error)"
        nav_error = str(e)

    memory_done.wait(timeout=3)
    past_memory = memory_box.get("past", "")

    # ── Phase 2: Build prompt ──────────────────────────────────────────────────
    memory_hint = f"\n\n[Past session on this goal]:\n{past_memory}\n" if past_memory else ""
    prompt = (
        f"Goal: {goal}{memory_hint}\n\n"
        f"Page content:\n{page_text}\n\n"
        f"Complete the goal concisely based on the page content."
    )

    # ── Phase 3: Stream Ollama response via SSE ────────────────────────────────
    full_response = []

    def generate():
        # Send metadata first as a special SSE event
        meta = {
            "memory_hit":     bool(past_memory),
            "memory_snippet": past_memory[:120] if past_memory else "",
            "url_visited":    visited_url or url,
            "nav_warning":    nav_error or "",
        }
        yield f"event: meta\ndata: {json.dumps(meta)}\n\n"

        for token in stream_ollama(prompt, model):
            full_response.append(token)
            yield f"data: {json.dumps(token)}\n\n"

        yield "event: done\ndata: {}\n\n"

        # Save memory after streaming
        if full_response:
            answer = "".join(full_response)
            threading.Thread(
                target=save_memory,
                args=(goal, answer[:400]),
                daemon=True,
            ).start()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"ChromaDB   : {DB_PATH}")
    print("Starting Playwright browser thread (headless)…")
    _start_browser_thread()
    # Warm up default model
    models = list_ollama_models()
    if models:
        print(f"Warming up : {models[0]}")
        warmup_model(models[0])
    app.run(port=5000, threaded=True)