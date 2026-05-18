# CyberLife RK800 - Async Predictive Fullstack Web Platform [STATUS: IN WORK]

An advanced, production-ready asynchronous fullstack web terminal built on the `FastAPI` framework and secured via `Nginx Reverse Proxy`. The platform simulates the deterministic protocol of the RK800 'Connor' android, serving as an automated business intelligence terminal, document automation assistant, and DevOps utility core.

## CURRENT STATUS (DONE & IN WORK)

* **Universal API Routing Proxy (`[DONE] Universal proxy_pass`):** Implements a unified, scalable reverse proxy structure over a secure service port (`2053`). Nginx dynamically routes complex REST actions seamlessly into the local loopback process running inside memory without CORS blockages.
* **Deterministic LLM Context Control (`[DONE] Context Protection`):** Utilizes `asyncio.Semaphore` management to safely govern concurrent LLM completions, throttling peak traffic and optimizing token consumption under high load via customized model layer (`openai/gpt-oss-120b`).
* **Multi-Format ETL Pipeline (PDF & OCR):** Features advanced backend integration of `PyMuPDF (fitz)` for structural multi-page PDF processing (up to 25 pages) and pixel-level character recognition utilizing a customized `Tesseract OCR` array (`lang="rus+eng"`).
* **Rigid Exception Handling & Sandbox UI:** Nested `try-except` wrappers ensure absolute state isolation. Backend runtime execution errors are safely captured and serialized into server logs, keeping the frontend interface resilient and responsive.

---

## AGENT ROADMAP & FEATURE PIPELINE (PLANNED)

### 1. Advanced Chat UX/UI Mutation
* **Message Deletion Engine:** Dedicated secure backend endpoints to dynamically purge structural chat message nodes from memory log buffers.
* **Last Message Editing:** Real-time mutation of the latest user prompt node to instantly re-trigger AI responses without context state corruption.

### 2. Live Web Scraping Core
* **URL Analysis Engine:** Implementation of an HTTP parsing layer (via `BeautifulSoup4` and `httpx`) allowing Connor to directly crawl external web links, extract structured text content, and summarize remote documentation arrays.

### 3. Smart Infrastructure Plan (From Corporate Blueprint)
* **AI Data Engineering Component:** Natural language-to-SQL automated compiler. Translates raw business queries like *"Extract all contractors from Moscow with debt over 50k"* into raw `SELECT` database sequences.
* **System Health Monitor (DevOps Utility):** Direct backend system access allowing operators to execute diagnostic sequences to retrieve live CPU, memory consumption, and Nginx traffic metrics formatted in clean Markdown tables.
* **Smart Copywriter Core:** Formal commercial typography converter. Automatically transforms unstructured text arrays into strict, canonized legal notices, business correspondence, and corporate responses.

---

## DEPLOYMENT ARCHITECTURE
* `Port 443` — Entry HTTPS endpoint secured via Let's Encrypt SSL certificates.
* `Port 2053` — Dedicated secure API service channel processing structural asynchronous POST JSON packets.
