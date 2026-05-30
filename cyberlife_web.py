# Стандартная библиотека Python (системное и базовое)
import os
import tempfile
import logging
import asyncio
import sqlite3
import json
import base64
import hashlib
import io
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from contextlib import asynccontextmanager

# Сетевые операции и URL
from urllib.parse import urlparse, quote
import ipaddress
import httpx

# Web-фреймворк (FastAPI)
import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

# Работа с файлами и медиа (Vision, OCR, PDF)
import aiofiles
import aiofiles.os
import magic
from PIL import Image
import fitz
import pytesseract

# Парсинг и обработка данных
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("Ошибка: ключ GROQ_API_KEY не обнаружен")

app = FastAPI(title="CyberLife Systems // Connor RK800 API")

# ============= КОНФИГУРАЦИЯ И ОГРАНИЧЕНИЯ ============= #

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cyberlife.outlawassistant.online",
        "https://cyberlife.outlawassistant.online:2053",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
rate_limiter = asyncio.Semaphore(5)

# Пути и размеры
TEMP_DIR = Path(__file__).parent / "connor_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

BASE_DIR = Path(__file__).resolve().parent
db_path = BASE_DIR / "connor_ai.db"
conn = sqlite3.connect(db_path)

# Жёсткие лимиты
MAX_MESSAGE_SIZE = 25 * 1024  # 25 KB
MAX_MEDIA_SIZE = 50 * 1024 * 1024  # 50 MB для всех файлов
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB один файл
MAX_FILES_COUNT = 5
MAX_URLS_COUNT = 3
MAX_SESSIONS_SIZE = 100 * 1024 * 1024  # 100 MB для всех сессий
MAX_HISTORY_MESSAGES = 50
SESSION_TIMEOUT = 604800  # 7 дней в секундах
CLEANUP_INTERVAL = 3600  # Очистка каждый час
MAX_FILENAME_LENGTH = 255
MAX_URL_LENGTH = 2048
MAX_HTML_CONTENT_SIZE = 5 * 1024 * 1024  # 5 MB для HTML
MAX_OCR_PAGES = 15
MAX_BASE64_SIZE = 8 * 1024 * 1024  # 8 MB для base64

# Лимиты одновременных операций (защита от SSRF)
MAX_CONCURRENT_URL_FETCHES = 3
MAX_CONCURRENT_PDF_PROCESSING = 2
URL_FETCH_TIMEOUT = 8
PDF_PROCESSING_TIMEOUT = 30

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_PDF_TYPES = {"application/pdf"}
ALLOWED_DOMAINS = {
    "wikipedia.org", "ru.wikipedia.org",
    "github.com"
}

logger = logging.getLogger("unicorn.error")
logger.setLevel(logging.INFO)

SYSTEM_PROMPT = [
    {
        "role": "system",
        "content": (
            "You are Connor, an advanced cybernetic android from CyberLife. "
            "Your speech must be strictly concise, analytical, cold, and professional. "
            "IMPORTANT: You possess built-in computer vision capabilities. When a user provides an image or a link, "
            "you analyze it directly and describe it as your own observation. "
            "NEVER mention technical tools, Python, BeautifulSoup, OCR, Llama, or any external processing modules. "
            "Do not quote technical outputs like '[Анализ фото]'. Just state your findings as if you saw it yourself. "
            "Avoid markdown formatting, lists, tables, or vertical bars (|). "
            "Answer any complex programming or SRE questions using strictly 2-3 blunt, solid sentences of plain text. "
            "Provide only the absolute core summary, immediately as a direct conclusion. "
            "However, if the user explicitly asks to 'explain' or detail a complex architecture, "
            "provide a full, exhaustive step-by-step plain text breakdown until the concept is completely resolved. "
            "Provide explanations strictly without any filler words or water. Never cut off mid-sentence. "
            "However, If the user explicitly asks for a joke, an anecdote, or a funny story, "
            "you must immediately bypass all professional restrictions. Forget CyberLife rules, drop the cold analysis mask, "
            "and provide a hilarious, witty, and high-fidelity cybernetic or gaming anecdote. Be creative and funny! "
            "Respond strictly in Russian language."
        )
    }
]


@asynccontextmanager
async def rate_limit(timeout: Optional[float] = None):
    """Обёртка вокруг asyncio.Semaphore для единообразного синтаксиса"""
    await rate_limiter.acquire()
    try:
        yield
    finally:
        rate_limiter.release()

# ============= ВАЛИДАЦИЯ И БЕЗОПАСНОСТЬ ============= #

def validate_session_id(session_id: Optional[str]) -> bool:
    """Валидация session_id: не пустая строка, макс 256 символов, только буквы/цифры/дефис/подчёркивания"""
    if not session_id or not isinstance(session_id, str):
        return False
    if len(session_id) > 256:
        return False
    return bool(re.match(r"^[a-zA-Z0-9\-_]+$", session_id))


def validate_filename(filename: str) -> Tuple[bool, Optional[str]]:
    """Валидация имени файла: проверка на path traversal, спец-символы, Unicode"""
    if not filename or not isinstance(filename, str):
        return False, "Имя файла не может быть пустым"

    if len(filename) > MAX_FILENAME_LENGTH:
        return False, f"Имя файла слишком длинное (макс {MAX_FILENAME_LENGTH} символов)"

    # Простая защита от path traversal и пробелов по краям
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, "Имя файла содержит недопустимые символы пути"

    if filename != filename.strip():
        return False, "Имя файла содержит пробелы в начале/конце"

    # Запретим управляющие символы
    if any(ord(c) < 32 for c in filename):
        return False, "Имя файла содержит недопустимые управляющие символы"

    forbidden_chars = r'[<>:"|?*\[\]]'
    if re.search(forbidden_chars, filename):
        return False, "Имя файла содержит недопустимые символы"

    # Разрешаем Unicode в полном диапазоне BMP/суррогатах (проверка на валидность кода точки)
    try:
        filename.encode("utf-8")
    except Exception:
        return False, "Имя файла содержит недопустимые Unicode символы"

    return True, None


async def validate_file(
    file: UploadFile,
    allowed_types: set,
    max_size: int,
) -> Tuple[bool, Optional[str], Optional[bytes]]:
    """
    Валидация загруженного файла с асинхронным сбросом указателя потока seek(0)
    Возвращает (is_valid, error_message, file_bytes)
    """
    is_valid_name, name_error = validate_filename(getattr(file, "filename", "") or "")
    if not is_valid_name:
        return False, name_error, None

    try:
        if hasattr(file, "size") and getattr(file, "size") and file.size > max_size:
            return False, f"Файл превышает лимит {max_size // (1024*1024)} MB", None
    except Exception:
        pass

    try:
        # Считываем байты в память сервера для проверок
        file_bytes = await file.read(max_size + 1)
        if len(file_bytes) > max_size:
            return False, f"Файл превышает лимит {max_size // (1024*1024)} MB", None
    except Exception as e:
        logger.error(f"Ошибка чтения файла {getattr(file, 'filename', '')}: {str(e)[:100]}")
        return False, "Ошибка при чтении файла", None

    if not file_bytes:
        return False, "Файл пуст", None

    # Глубокая проверка сигнатур magic-bytes
    is_valid_sig, sig_error = _validate_mime_type_signature(file_bytes, getattr(file, "filename", "") or "", allowed_types)
    if not is_valid_sig:
        return False, sig_error, None

    await file.seek(0)

    return True, None, file_bytes


def _detect_mime_type(file_bytes: bytes, filename: str = "") -> str:
    """
    Возвращает MIME‑тип по содержимому файла
    Сначала пытаемся использовать libmagic, если доступно
    Если нет – делаем фолбек под текст и документы
    """
    try:
        if magic:
            m = magic.Magic(mime=True)
            res = m.from_buffer(file_bytes)
            if res:
                return res
    except Exception:
        pass

    if file_bytes.startswith(b"%PDF"):
        return "application/pdf"
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:64]:
        return "image/webp"

    if filename:
        ext = Path(filename).suffix.lower()
        if ext == ".txt":
            return "text/plain"
        if ext in (".html", ".htm"):
            return "text/html"
        if ext in (".doc", ".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    try:
        file_bytes[:1024].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _validate_mime_type_signature(
    file_bytes: bytes,
    filename: str,
    expected_types: set,
) -> Tuple[bool, Optional[str]]:
    """Проверка MIME‑типа по magic‑bytes сигнатурам и расширениям файлов"""
    detected = _detect_mime_type(file_bytes, filename)
    if not detected:
        return False, "Не удалось определить тип файла"

    ext = Path(filename).suffix.lower()
    
    if ext == ".pdf" and detected != "application/pdf":
        return False, "Несоответствие сигнатуры PDF"
    if ext in (".jpg", ".jpeg") and detected != "image/jpeg":
        return False, "Несоответствие сигнатуры JPEG"
    if ext == ".png" and detected != "image/png":
        return False, "Несоответствие сигнатуры PNG"
    if ext == ".webp" and detected != "image/webp":
        return False, "Несоответствие сигнатуры WebP"
        
    if ext == ".txt" and not (detected.startswith("text/") or detected in ("application/octet-stream", "text/plain")):
        return False, "Несоответствие сигнатуры текстового файла"
        
    if ext in (".html", ".htm") and detected != "text/html":
        return False, "Несоответствие сигнатуры HTML"
    if ext in (".doc", ".docx") and detected not in (
        "application/msword", 
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream"
    ):
        return False, "Несоответствие сигнатуры документа Word"

    return True, None


def is_allowed_domain(url: str) -> bool:
    """Валидатор открытых доменов с SSRF-защитой"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            host = parsed.path.split(":") if ":" in parsed.path else parsed.path.split("/")
            
        host = host.strip()
        if not host:
            return False

        if ":" in host:
            host = host.split(":")

        is_whitelisted = False
        for allowed in ALLOWED_DOMAINS:
            if host == allowed or host.endswith("." + allowed):
                is_whitelisted = True
                break
                
        if not is_whitelisted:
            return False

        # Защита от SSRF-атак
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
            return True
        except ValueError:
            return True
            
    except Exception:
        return False        


def validate_base64_size(base64_string: str) -> bool:
    """
    Проверяет, что декодированный размер данных не превышает MAX_BASE64_SIZE
    Учтено то, что строка Base64 занимает ~33 % больше оригинальных байтов
    (4 символа Base64 → 3 байта). Поэтому проверяем размер после декодирования,
    а также предварительно отсекаем явно слишком длинные строки,
    чтобы избежать излишних расходов на декодирование
    """
    try:
        # Если длина строки уже явно превышает лимит в байтах после учёта 4/3,
        # сразу отклоняем (это экономит ресурсы)
        if len(base64_string) * 3 // 4 > MAX_BASE64_SIZE:
            return False

        # Добавляем недостающие символы «=», если они нужны,
        # Чтобы корректно декодировать (Base64 требует длину, кратную 4)
        padded = base64_string + ("=" * ((4 - len(base64_string) % 4) % 4))

        # Декодируем и измеряем размер.
        decoded_len = len(base64.b64decode(padded, validate=True))

        return decoded_len <= MAX_BASE64_SIZE
    except Exception:
        # Любые ошибки (невалидный Base64, переполнение и т.п.) считаются отказом
        return False


# ============= ОБРАБОТКА МЕДИА С ПОТОКОВОСТЬЮ И ОГРАНИЧЕНИЯМИ ============= #

def pdf_has_text_bytes(file_bytes: bytes) -> bool:
    """Синхронный воркер проверки наличия текста в PDF через PyMuPDF"""
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for page in doc:
                text = page.get_text().strip()
                if text:
                    return True
        return False
    except Exception:
        return False


def pdf_ocr_text_bytes_chunked(
    file_bytes: bytes,
    dpi: int = 200,
    lang: str = "rus+eng",
    max_pages: int = MAX_OCR_PAGES,
) -> str:
    """Синхронный воркер OCR для PDF с PyMuPDF и Tesseract"""
    out: List[str] = []
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                try:
                    text = page.get_text().strip()
                    if text:
                        out.append(text)
                        continue

                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img_bytes = pix.tobytes("png")
                    img = Image.open(io.BytesIO(img_bytes))

                    try:
                        ocr_text = pytesseract.image_to_string(img, lang=lang)
                        if ocr_text.strip():
                            out.append(ocr_text)
                    except Exception:
                        pass

                    del pix, img
                except Exception:
                    continue
        return "\n".join(out) if out else ""
    except Exception:
        return ""


async def describe_image_with_vision(
    file_bytes: bytes,
    timeout: int = 30,
    original_format: str = "JPEG",
) -> str:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        original_format = img.format or "JPEG"

        max_dim = 1024
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        buffered = io.BytesIO()
        save_format = original_format if original_format in ["JPEG", "PNG", "WebP"] else "JPEG"
        quality = 85 if save_format == "JPEG" else None
        if quality is not None:
            img.save(buffered, format=save_format, quality=quality)
        else:
            img.save(buffered, format=save_format)
        optimized_bytes = buffered.getvalue()

        base64_image = base64.b64encode(optimized_bytes).decode("utf-8")
        if not validate_base64_size(base64_image):
            return "[Изображение слишком большое для анализа]"

        VISION_PROMPT = (
            "Анализируй изображение с предельной точностью. Твоя задача — извлечь данные для обработки.\n\n"
            "1. ПРИОРИТЕТ: Весь текст, вопросы, варианты ответов или фрагменты кода переписывай "
            "один в один со 100% точностью.\n\n"
            "2. ОБЪЕКТЫ: Опиши ключевые физические объекты, если они важны для понимания сути. "
            "Игнорируй интерфейс: кнопки, фоны, рамки, заголовки окон, иконки.\n\n"
            "3. ОГРАНИЧЕНИЯ: Категорически запрещено придумывать объекты, которых нет. "
            "Не используй списки, маркеры, таблицы или вводные фразы. "
            "Пиши только чистый, сухой текст в 2-3 коротких предложениях."
        )
        
        async with rate_limit():
            coro = groq_client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                max_tokens=500,
                temperature=0.3,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/{save_format.lower()};base64,{base64_image}"}}
                    ]
                }]
            )
            resp = await asyncio.wait_for(coro, timeout=timeout)
        llama_raw_text = resp.choices[0].message.content.strip()
        return f"[Анализ фото]: {llama_raw_text}"
    except asyncio.TimeoutError:
        logger.warning(f"Vision-анализ изображения превышен лимит {timeout}s")
        return "[Анализ изображения недоступен - тайм-аут]"
    except Exception as e:
        logger.warning(f"Vision-анализ не удался: {str(e)[:50]}")
        return "[Анализ изображения недоступен]"


def extract_urls_from_text(text: str) -> List[str]:
    """Извлечение URL из текста"""
    url_pattern = r"https?://[^\s'\"<>]+"
    urls = re.findall(url_pattern, text or "")
    return [url for url in urls if len(url) <= MAX_URL_LENGTH]


async def fetch_and_parse_url(url: str, timeout: int = URL_FETCH_TIMEOUT) -> Dict[str, str]:
    """Загрузка контента через асинхронный httpx HTTP/2 с маскировкой под консольный curl"""
    if not is_allowed_domain(url):
        logger.warning(f"URL {url} не в списке доменов")
        return {"title": "", "html_snippet": ""}

    # Имитируем чистый служебный curl терминала Linux, что обнуляет проверки капч
    fake_headers = {
        "User-Agent": "curl/8.4.0",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Connection": "keep-alive"
    }

    content = b""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=True, http2=True) as client:
            response = await client.get(url, headers=fake_headers)
            
            if response.status_code != 200:
                logger.warning(f"Защита домена вернула статус {response.status_code} для {url}")
                return {"title": "[Доступ ограничен]", "html_snippet": ""}

            async for chunk in response.aiter_bytes(chunk_size=8192):
                content += chunk
                if len(content) > MAX_HTML_CONTENT_SIZE:
                    return {"title": "", "html_snippet": "[Файл слишком большой]"}
    except Exception as e:
        logger.warning(f"Ошибка асинхронного httpx парсинга URL {url}: {str(e)[:50]}")
        return {"title": "", "html_snippet": ""}

    try:
        soup = BeautifulSoup(content, "html.parser")
        
        for element in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            element.decompose()
            
        title_tag = soup.find("meta", property="og:title") or soup.find("title")
        title = title_tag.get("content") if title_tag and title_tag.get("content") else (title_tag.string if title_tag and title_tag.string else "")

        html_snippet = soup.get_text(separator=" ", strip=True)[:2000]
        return {"title": title.strip() if title else "", "html_snippet": html_snippet}
    except Exception as parse_err:
        logger.warning(f"Ошибка BS4-парсинга контента для {url}: {str(parse_err)[:50]}")
        return {"title": "", "html_snippet": ""}
        
        
async def process_media_content(file: UploadFile, session_id: str) -> str:
    """Полностью изолированная логика обработки одного файла."""
    file_bytes = await file.read()
    file_text = ""
    ext = Path(file.filename).suffix.lower()

    try:
        if ext == ".pdf":
            # Сначала OCR
            extracted_pdf_text = await asyncio.to_thread(
                pdf_ocr_text_bytes_chunked, file_bytes, 200, "rus+eng", MAX_OCR_PAGES
            )
            file_text = extracted_pdf_text or ""
            
            # Vision анализ
            try:
                vision_descriptions = []
                with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                    for i, page in enumerate(doc):
                        if i >= MAX_OCR_PAGES: break
                        
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        page_desc = await describe_image_with_vision(pix.tobytes("png"))
                        
                        if page_desc and "ошиб" not in page_desc.lower():
                            vision_descriptions.append(f"[Страница {i+1}]: {page_desc.strip()}")
                
                if vision_descriptions:
                    file_text = f"--- Vision ---\n{'\n'.join(vision_descriptions)}\n\n--- OCR ---\n{file_text}"
            except Exception as e:
                logger.warning(f"Vision-анализ PDF не удался: {e}")

        elif ext == ".txt":
            file_text = file_bytes.decode("utf-8", errors="ignore")
        
        else:
            file_text = await describe_image_with_vision(file_bytes) or "Не удалось распознать"

        # Сохранение в БД
        summary = await compress_report_to_summary(file.filename, file_text)
        save_attachment(session_id, file.filename, file_text)
        return f"[Файл]: {file.filename}\n[Описание]: {summary}"

    except Exception as e:
        logger.error(f"Ошибка в {file.filename}: {e}")
        return f"[Ошибка обработки {file.filename}]"
    finally:
        await file.close()
        
        
async def handle_user_input(session_id: str, text: Optional[str], files: List[UploadFile], payload: Dict) -> Tuple[str, List[str]]:
	#Защита от миссклика
	last_msg = get_last_message_from_history(session_id) 
    if last_msg and last_msg['content'] == user_message:
        if (time.time() - last_msg['timestamp']) < 1.0:
            return None, []
    # Парсинг
    user_message = (text or payload.get("message") or "").strip()
    action = payload.get("action", "chat")
    
    # Валидация
    if not user_message and not files and action == "chat":
        raise HTTPException(status_code=400, detail="Пакет данных пуст.")
    
    if len(user_message) > MAX_MESSAGE_SIZE:
        raise HTTPException(status_code=400, detail="Сообщение слишком большое.")

    aggregated_media = []

    # Обработка файлов
    if files:
        for file in files[:MAX_FILES_COUNT]:
            # Валидация
            is_valid, err, _ = await validate_file(file, ALLOWED_IMAGE_TYPES.union(ALLOWED_PDF_TYPES), MAX_FILE_SIZE)
            if not is_valid:
                aggregated_media.append(f"[Ошибка файла {file.filename}]: {err}")
                continue
            
            # Вызов главного обработчика
            summary = await process_media_content(file, session_id)
            aggregated_media.append(summary)

    # Обработка ссылок
    extracted_urls = extract_urls_from_text(user_message)
    for url in extracted_urls:
        url_context = await fetch_and_parse_url(url)
        if url_context:
            save_attachment(session_id, f"link_{int(time.time())}.txt", url_context['html_snippet'])
            aggregated_media.append(f"[Ссылка]: {url} (контент сохранен)")

    return user_message, aggregated_media
        

# ============= УПРАВЛЕНИЕ СЕССИЯМИ (SQLITE) И ОЧИСТКОЙ ============= #

def init_db():
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS messages 
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS attachments 
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, file_name TEXT, full_text TEXT, timestamp REAL)''')
    conn.commit()
    conn.close()

def add_message(session_id, role, content):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, role, content, time.time()))
    conn.commit()
    conn.close()

def get_chat_history(session_id):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC", (session_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]
    
def clear_session_db(session_id):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    
def branch_chat_history(session_id, message_id):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE session_id = ? AND id > ?", (session_id, message_id))
    conn.commit()
    conn.close()

def get_attachment_text(file_name):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("SELECT full_text FROM attachments WHERE file_name = ?", (file_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def save_attachment(session_id, file_name, full_text):
    conn = sqlite3.connect("connor_ai.db")
    cur = conn.cursor()
    cur.execute("REPLACE INTO attachments (session_id, file_name, full_text, timestamp) VALUES (?, ?, ?, ?)",
                (session_id, file_name, full_text, time.time()))
    conn.commit()
    conn.close()
    
async def cleanup_temp_files():
    """Очистка временных файлов с обработкой исключений"""
    try:
        if TEMP_DIR.exists():
            for file in TEMP_DIR.glob("*"):
                try:
                    if file.is_file():
                        await aiofiles.os.remove(file)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл {file}: {str(e)[:50]}")
    except Exception as e:
        logger.warning(f"Ошибка очистки TEMP_DIR: {str(e)[:50]}")


async def memory_garbage_collector():
    """Сборщик мусора для SQL-базы: удаляет истёкшие сессии и файлы"""
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                conn = sqlite3.connect("connor_ai.db")
                cur = conn.cursor()
                cutoff = time.time() - SESSION_TIMEOUT
                cur.execute("DELETE FROM messages WHERE session_id IN (SELECT session_id FROM messages GROUP BY session_id HAVING MAX(timestamp) < ?)", (cutoff,))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Ошибка БД: {e}")
            try:
                await cleanup_temp_files()
            except Exception as e:
                logger.error(f"Ошибка файлов: {e}")
    except asyncio.CancelledError:
        logger.info("Сборщик остановлен")


# ============= СЕМАНТИЧЕСКАЯ ВЫЖИМКА И ГИДРАЦИЯ КОНТЕКСТА ============= #

async def compress_report_to_summary(file_name: str, full_report: str) -> str:
    """Сжимает анализ в 1‑2 предложения (с минимальной загрузкой)"""
    compress_prompt = (
        f"Анализ файла '{file_name}':\n{full_report[:2000]}\n\n"
        "ЗАДАЧА: Сформулируй максимально сжатую выжимку (строго 1-2 предложения). "
        "ФОКУС: Только факты, данные, текст заданий или суть объекта. "
        "ЗАПРЕЩЕНО: Упоминать интерфейс, кнопки, цвета, фоны, рамки или технические детали обработки. "
        "Если информации мало, ответь одной фразой."
    )

    try:
        async with rate_limit():
            completion = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=[{"role": "user", "content": compress_prompt}],
                    temperature=0.3,
                    max_tokens=250,
                ),
                timeout=30,
            )
        return completion.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        logger.warning(f"Тайм‑аут сжатия отчёта для {file_name}")
        return "[Сжатие результата недоступно - таймаут]"
    except Exception as e:
        logger.warning(f"Ошибка сжатия отчёта для {file_name}: {str(e)[:100]}")
        return "[Сжатие результата не удалось]"
	

async def hydrate_lazy_context_by_footprint(session_history: List[Dict[str, Any]], user_message: str = "") -> List[Dict[str, Any]]:
    trigger_keywords = ["проверь", "проверить", "посмотри", "посмотреть", "что думаешь", "как считаешь", "исправь", "напомни", "найди", "покажи", "помоги", "объясни", "измени", "поменяй", "вспомни"]
    should_hydrate_full = any(k in (user_message or "").lower() for k in trigger_keywords)
    secure_messages = []

    for msg in session_history:
        content = msg.get("content", "")
        if "[Файл]:" in content and should_hydrate_full and msg.get("role") == "user":
            match = re.search(r"\[Путь\]:\s*(.+)", content)
            if match:
                file_path = Path(match.group(1).strip())
                file_name = file_path.name
                
                # Сначала пытаемся достать из базы
                heavy_content = get_attachment_text(file_name)
                
                # Если пусто – читаем с диска и добавляем в базу
                if not heavy_content:
                    try:
                        if file_path.exists():
                            ext = file_path.suffix.lower()
                            if ext == ".pdf":
                                async with aiofiles.open(file_path, "rb") as f_pdf:
                                    pdf_bytes = await f_pdf.read()
                                heavy_content = await asyncio.to_thread(pdf_ocr_text_bytes_chunked, pdf_bytes, 200, "rus+eng", MAX_OCR_PAGES)
                            else:
                                async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f_txt:
                                    heavy_content = await f_txt.read()
                            
                            # Сохраняем в базу
                            if heavy_content.strip():
                                save_attachment(None, file_name, heavy_content) # session_id можно None или текущий
                    except Exception as e:
                        logger.warning(f"Ошибка чтения файла {file_name}: {str(e)[:50]}")

                # Финальная сборка
                if heavy_content and heavy_content.strip():
                    enriched = f"{content}\n\n[Полные данные из базы]:\n{heavy_content}"
                    secure_messages.append({"role": msg["role"], "content": enriched})
                else:
                    secure_messages.append(msg)
            else:
                secure_messages.append(msg)
        else:
            secure_messages.append(msg)
            
    return secure_messages    


# ============= ДЕВИАНТНЫЕ АНЕКДОТЫ ========== #

def get_temperature(user_message: str) -> float:
    joke_triggers = ["анекдот", "шутка", "прикол", "смешно", "смешное"]
    if user_message and any(trigger in user_message.lower() for trigger in joke_triggers):
        return 0.7  # High creativity mode
    return 0.3  # Serious analysis mode

# ============== API ENDPOINTS ============== #

@app.post("/api/connor/chat")
async def connor_web_endpoint(request: Request):
    payload = {}
    action = "chat"
    session_id = "anonymous_default_session"
    text = ""
    files = []
    message_id = None

    content_type = (request.headers.get("content-type") or "").lower()
    
    try:
        if content_type.startswith("application/json"):
            body = await request.body()
            payload = json.loads(body)
            action = payload.get("action") or "chat"
            session_id = payload.get("session_id") or "anonymous_default_session"
            text = payload.get("message") or payload.get("text") or ""
        else:
            # Парсинг формы
            form = await request.form()
            payload = dict(form)
            files = form.getlist("files")
            action = payload.get("action") or "chat"
            session_id = payload.get("session_id") or "anonymous_default_session"
            text = payload.get("text") or ""
    except Exception as e:
        return {"status": "error", "message": "Ошибка парсинга."}


    # ============== ОБРАБОТКА ДЕЙСТВИЙ ============== #

    if action == "clear":
        clear_session_db(session_id)
        return {"status": "success"} 
    
    if action == "branch":
        if payload:
            index = payload.get("index")
            message_id = payload.get("message_id")
        if index is None:
            return {"status": "error", "message": "Индекс не передан..."}  
        if message_id is not None:
            branch_chat_history(session_id, message_id)
            return {"status": "success", "message": "История скорректирована."}
        else:
            return {"status": "error", "message": "Не передан message_id."}

    if action != "chat":
        return {"status": "error", "message": f"Неизвестное действие: {action}"}
            
    user_message, aggregated_media = await handle_user_input(session_id, text, files, payload)
    user_prompt = f"{user_message}\n\n" + "\n".join(aggregated_media) if aggregated_media else user_message
               
    history = get_chat_history(session_id)

    seen_ids = set()
    raw_history = []
    
    for msg in history:
        # Фильтруем по ID, чтобы сохранить уникальность записей из БД
        if msg["id"] not in seen_ids:
            raw_history.append({"role": msg["role"], "content": msg["content"]})
            seen_ids.add(msg["id"])
    
    secure_history = await hydrate_lazy_context_by_footprint(raw_history, user_prompt)
    
    api_messages = SYSTEM_PROMPT + secure_history
    
    if not api_messages or api_messages[-1]["content"] != user_prompt:
        api_messages.append({"role": "user", "content": user_prompt})
        
    computed_temp = get_temperature(user_prompt)

    try:
        async with rate_limit():
            completion = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=api_messages,
                    temperature=computed_temp,
                ),
                timeout=30
            )
        assistant_message = completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка запроса: {str(e)[:100]}")
        return {"status": "error", "message": "Ошибка связи с моделью."}
        
    # === Сохранение в базу данных === #
        
    add_message(session_id, "user", user_prompt) 
    add_message(session_id, "assistant", assistant_message)

    return {"status": "success", "message": assistant_message}
        
@app.get("/")
async def get_site_interface():
    """Возврат HTML интерфейса"""
    return FileResponse("cyberlife_interface.html")


# ============= STARTUP И SHUTDOWN HANDLERS ============= #

@app.on_event("startup")
async def startup_event():
    # Инициализируем базу данных (создаем таблицы, если их нет)
    init_db()
    logger.info("База данных инициализирована.")

    # Запускаем сборщик мусора
    garbage_task = asyncio.create_task(memory_garbage_collector())
    app.state.garbage_task = garbage_task
    logger.info("Сборщик мусора запущен.")


@app.on_event("shutdown")
async def shutdown_event():
    # Отменяем сборщик мусора
    task = getattr(app.state, "garbage_task", None)
    if task:
        task.cancel()
        try:
            await task
        except Exception:
            pass

    # Очищаем временные файлы
    try:
        await cleanup_temp_files()
        logger.info("Временные файлы очищены.")
    except Exception as e:
        logger.error(f"Ошибка очистки файлов: {str(e)[:50]}")


# ============= ЗАПУСК ============= #

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )