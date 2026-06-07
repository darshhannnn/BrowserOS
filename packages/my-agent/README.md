# my-agent

A Python-based agentic browser powered by **Flask + Playwright + Ollama + ChromaDB**.

## Overview

This package provides a local AI agent that can:
- **Browse the web** using Playwright (Chromium)
- **Reason & answer** using a locally-running Ollama LLM
- **Remember past sessions** via ChromaDB vector store
- **Expose a REST API + Web UI** via Flask

## Architecture

```
Flask (threaded)
  │
  ├── /          → Serves index.html (Web UI)
  ├── /models    → Lists available Ollama models
  └── /run       → Triggers browser + LLM pipeline
        │
        ├── BrowserThread (Playwright, single dedicated thread)
        ├── Ollama (http://localhost:11434)
        └── ChromaDB (persistent vector memory)
```

## Files

| File | Description |
|------|-------------|
| `app.py` | Flask backend — main server |
| `agent.py` | Standalone CLI agent (async Playwright + Ollama) |
| `index.html` | Web UI frontend |

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) running locally
- Dependencies: `flask`, `playwright`, `httpx`, `chromadb`

```bash
pip install flask playwright httpx chromadb
playwright install chromium
```

## Usage

```bash
# Start the web server
python app.py

# Or run the CLI agent directly
python agent.py
```

The web UI will be available at `http://localhost:5000`.
