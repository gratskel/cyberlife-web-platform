import os
import tempfile
import logging

import asyncio
from contextlib import asynccontextmanager

import json
import base64
import hashlib
import io
import re

import time
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, quote
import ipaddress

from typing import List, Optional, Tuple, Dict, Any

import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

import httpx

import aiofiles
import aiofiles.os
import magic
from PIL import Image
import fitz

import pytesseract
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
sessions_lock = asyncio.Lock()

# Пути и размеры
SESSIONS_FILE = Path("sessions_storage.json")
TEMP_DIR = Path(__file__).parent / "connor_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

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

sessions_storage: Dict[str, Dict[str, Any]] = {}


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

        VISION_PROMPT = "Опиши это изображение с абсолютной точностью в 2-3 коротких, емких предложениях. Четко определи и перечисли только те реальные физические объекты, текстуры, формы, цвета и элементы окружения, которые физически присутствуют на картинке. Если на кадре есть текст или строки кода — перепиши их один в один со 100% точностью. Категорически запрещено придумывать несуществующие объекты, упоминать предметы или фразы, если их нет на самом фото. Выдавай только чистый, сухой текст без списков, маркеров и вводных фраз."
        
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
        return f"Коннор, на фото, предоставленном пользователем, изображено: {llama_raw_text}"
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
        # verify=True возвращаем строго для работы HSTS шлюзов безопасности Википедии
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


# ============= УПРАВЛЕНИЕ СЕССИЯМИ И ХРАНИЛИЩЕМ ============= #

async def load_sessions_from_disk():
    global sessions_storage
    if SESSIONS_FILE.exists():
        try:
            async with aiofiles.open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
            if len(content) > MAX_SESSIONS_SIZE:
                logger.error("Файл сессий превышает лимит размера")
                sessions_storage = {}
                return
            sessions_storage = json.loads(content)
        except Exception as e:
            logger.error(f"Ошибка загрузки сессий: {str(e)[:100]}")
            sessions_storage = {}
    else:
        sessions_storage = {}


async def save_sessions_to_disk():
    """Сохранение сессий на диск с обработкой ошибок"""
    try:
        temp_file = SESSIONS_FILE.with_suffix(".json.tmp")
        async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(sessions_storage, ensure_ascii=False, indent=2))
        if SESSIONS_FILE.exists():
            await aiofiles.os.remove(SESSIONS_FILE)
        await aiofiles.os.rename(temp_file, SESSIONS_FILE)
    except (IOError, OSError) as err:
        logger.error(f"Критическая ошибка записи на диск: {err}")


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
    """
    Сборщик мусора: удаляет старые сессии и очищает временные файлы
    Запускается как фоновая таска; при shutdown будет отменяться
    """
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            async with sessions_lock:
                try:
                    current_time = time.time()
                    expired = [sid for sid, data in sessions_storage.items()
                               if current_time - data.get("last_activity", 0) > SESSION_TIMEOUT]
                    for sid in expired:
                        sessions_storage.pop(sid, None)
                        logger.info(f"Сессия {sid} удалена (тайм-аут)")
                    if expired:
                        await save_sessions_to_disk()
                except Exception as e:
                    logger.error(f"Ошибка при удалении истёкших сессий: {str(e)[:100]}")
            try:
                await cleanup_temp_files()
            except Exception as e:
                logger.error(f"Ошибка очистки временных файлов: {str(e)[:100]}")
    except asyncio.CancelledError:
        logger.info("Сборщик мусора остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка в garbage_collector: {str(e)[:100]}")        


# ============= СЕМАНТИЧЕСКАЯ ВЫЖИМКА И ГИДРАЦИЯ КОНТЕКСТА ============= #

async def compress_report_to_summary(file_name: str, full_report: str) -> str:
    """Сжимает анализ в 1‑2 предложения (с минимальной загрузкой)"""
    compress_prompt = (
        f"Вот краткий анализ файла '{file_name}':\n\n"
        f"{full_report[:2000]}\n\n"
        "Напиши одно‑два предложения выжимки самых важных выводов "
        "(основная проблема/фишка/архитектура):"
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
    """Гидрация истории: асинхронно поднимает с диска полные данные файлов (TXT/PDF OCR) по footprint"""
    trigger_keywords = [
        "проверь", "проверить", "посмотри", "посмотреть",
        "что думаешь", "как считаешь", "исправь", "напомни", "найди",
        "покажи", "помоги", "объясни", "измени", "поменяй", "вспомни"
    ]
    should_hydrate_full = any(k in (user_message or "").lower() for k in trigger_keywords)
    secure_messages: List[Dict[str, Any]] = []

    for msg in session_history:
        content = msg.get("content", "")
        if "[Файл]:" in content and should_hydrate_full and msg.get("role") == "user":
            match = re.search(r"\[Путь\]:\s*(.+)", content)
            if match:
                file_path = Path(match.group(1).strip())
                try:
                    try:
                        if not file_path.resolve().is_relative_to(TEMP_DIR.resolve()):
                            secure_messages.append(msg)
                            continue
                    except AttributeError:
                        try:
                            file_path.resolve().relative_to(TEMP_DIR.resolve())
                        except Exception:
                            secure_messages.append(msg)
                            continue
                            
                    if file_path.exists():
                        ext = file_path.suffix.lower()
                        heavy_content = ""
                        
                        if ext == ".pdf":
                            # Бинарный подъём PDF и постраничный OCR Tesseract в потоке Linux
                            async with aiofiles.open(file_path, "rb") as f_pdf:
                                pdf_bytes = await f_pdf.read()
                            heavy_content = await asyncio.to_thread(
                                pdf_ocr_text_bytes_chunked, 
                                pdf_bytes, 200, "rus+eng", MAX_OCR_PAGES
                            )
                        else:
                            async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f_txt:
                                heavy_content = await f_txt.read()
                        
                        if heavy_content.strip():
                            enriched = f"{content}\n\n[Полностью восстановленное содержимое вложения для повторного анализа]:\n{heavy_content}"
                            secure_messages.append({"role": msg["role"], "content": enriched})
                        else:
                            secure_messages.append(msg)
                    else:
                        secure_messages.append(msg)
                except Exception as e:
                    logger.warning(f"Ошибка динамической ре-гидрации файла {file_path}: {str(e)[:50]}")
                    secure_messages.append(msg)
            else:
                secure_messages.append(msg)
        else:
            secure_messages.append(msg)
            
    return secure_messages    


# ============= ДЕВИАНТНЫЕ АНЕКДОТЫ ========== #

def get_temperature(user_message: str) -> float:
    joke_triggers = ["анекдот", "шутка", "прикол"]
    if user_message and any(trigger in user_message.lower() for trigger in joke_triggers):
        return 0.7  # High creativity mode
    return 0.3  # Serious analysis mode

# ============== API ENDPOINTS ============== #

@app.post("/api/connor/chat")
async def connor_web_endpoint(
    request: Request,
    text: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    action: Optional[str] = Form(None),
    index: Optional[int] = Form(None),
    files: Optional[List[UploadFile]] = File(None)
):

    # ============== ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ ============== #

    if not isinstance(text, (str, type(None))):
        return {"status": "error", "message": "Ошибка: 'text' должно быть строкой."}

    if not isinstance(action, (str, type(None))):
        return {"status": "error", "message": "Ошибка: 'action' должно быть строкой."}

    if not isinstance(index, (int, type(None))):
        return {"status": "error", "message": "Ошибка: 'index' должно быть целым числом."}

    if not isinstance(session_id, (str, type(None))):
        return {"status": "error", "message": "Ошибка: 'session_id' должно быть строкой."}

    # Валидация session_id
    session_id = session_id or "anonymous_default_session"
    if not validate_session_id(session_id):
        session_id = "anonymous_default_session"

    # Обработка JSON payload для обратной совместимости
    content_type = (request.headers.get("content-type") or "").lower()
    payload: Dict[str, Any] = {}
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
            action = (payload.get("action") or action) or "chat"
            session_id = (payload.get("session_id") or session_id) or "anonymous_default_session"
            index = payload.get("index", index)
            text = (payload.get("message") or payload.get("text") or text) or ""
        except Exception as e:
            logger.warning(f"Ошибка парсинга JSON: {str(e)[:50]}")
            return {"status": "error", "message": "Некорректный JSON в запросе."}
    else:
        action = action or "chat"
        session_id = session_id or "anonymous_default_session"
        text = text or ""

    # ============== ОБРАБОТКА ДЕЙСТВИЙ ============== #

    async with sessions_lock:

        # === ACTION: CLEAR === #

        if action == "clear":
            if session_id in sessions_storage:
                sessions_storage.pop(session_id, None)
                await save_sessions_to_disk()
            return {"status": "success", "message": "Вся история текущей сессии полностью уничтожена."}
            return {"status": "error", "message": "Сессия пуста или не найдена."}

        # === ACTION: BRANCH === #

        if action == "branch":
            edited_index = index
            if edited_index is None and payload:
                edited_index = payload.get("index")

            if edited_index is None:
                return {"status": "error", "message": "Индекс не передан для операции branch."}

            if session_id not in sessions_storage:
                return {"status": "error", "message": "Сессия не найдена."}

            try:
                edited_index = int(edited_index)
            except (ValueError, TypeError):
                return {"status": "error", "message": "Индекс должен быть целым числом."}

            if edited_index < 0:
                return {"status": "error", "message": "Индекс должен быть неотрицательным."}

            messages_count = len(sessions_storage[session_id].get("messages", []))
            if edited_index >= messages_count:
                return {"status": "error", "message": f"Индекс ({edited_index}) превышает количество сообщений ({messages_count})."}

            # Удаляем сообщение с индексом и всё после него
            sessions_storage[session_id]["messages"] = sessions_storage[session_id]["messages"][:edited_index]
            sessions_storage[session_id]["last_activity"] = time.time()
            await save_sessions_to_disk()
            return {"status": "success", "message": f"Контекст успешно обрезан до индекса {edited_index}."}

        # === ACTION: CHAT (default) === #

        if action != "chat":
            return {"status": "error", "message": f"Неизвестное действие: {action}"}

        # Инициализируем или получаем сессию
        if session_id not in sessions_storage:
            sessions_storage[session_id] = {"messages": [], "last_activity": time.time()}
        sessions_storage[session_id]["last_activity"] = time.time()
        user_history = sessions_storage[session_id]["messages"]

        # === Парсинг пользовательского сообщения === #

        user_message = (text or "").strip()
        if not user_message and payload:
            user_message = (payload.get("message") or "").strip()

        # === Проверяем: либо текст, либо файлы === #

        has_files = files and len(files) > 0
        if not user_message and not has_files:
            return {"status": "error", "message": "Пакет данных пуст. Требуется текст или файлы."}

        if user_message and len(user_message) > MAX_MESSAGE_SIZE:
            return {"status": "error", "message": f"Сообщение слишком большое, максимум {MAX_MESSAGE_SIZE // 1024} КБ."}

        # === Обработка файлов === #

        aggregated_file_texts: List[str] = []

        if has_files:
            total_uploaded_size = 0
            limit_exceeded = False

            if len(files) > MAX_FILES_COUNT:
                logger.warning(f"Попытка загрузить {len(files)} файлов, лимит {MAX_FILES_COUNT}")
                files = files[:MAX_FILES_COUNT]

            for file in files:
                if limit_exceeded:
                    break

                try:

                    # === Валидация файла === #

                    is_valid, err_msg, file_bytes = await validate_file(
                        file,
                        ALLOWED_IMAGE_TYPES.union(ALLOWED_PDF_TYPES),
                        MAX_FILE_SIZE,
                    )
                    if not is_valid:
                        aggregated_file_texts.append(
                            f"[Ошибка валидации файла {getattr(file, 'filename', '')}]: {err_msg}"
                        )
                        await file.close()
                        continue

                    # === НАЗВАНИЕ ДЛЯ ФАЙЛА === #
                    
                    safe_name = re.sub(r"[^\w\-.]", "_", file.filename or "upload")
                    safe_file_path = TEMP_DIR / f"{session_id}_{safe_name}"
                    async with aiofiles.open(safe_file_path, "wb") as f:
                        await f.write(file_bytes)

                    # === ОБРАБОТКА ФАЙЛОВ === #
                    
                    file_text = ""
                    ext = Path(safe_file_path).suffix.lower()
     
                    if ext == ".pdf":
                        try:
                            # Извлекаем печатный и OCR текст из PDF через фоновый поток по позициям
                            extracted_pdf_text = await asyncio.to_thread(
                                pdf_ocr_text_bytes_chunked, 
                                file_bytes, 200, "rus+eng", MAX_OCR_PAGES
                            )
                            file_text = extracted_pdf_text.strip() if extracted_pdf_text else ""
                            
                            # Циклом итерируем страницы для Лламы
                            vision_descriptions: List[str] = []
                            try:
                                with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                                    # Сканируем страницы до лимита MAX_OCR_PAGES, чтобы беречь ОЗУ
                                    for i, page in enumerate(doc):
                                        if i >= MAX_OCR_PAGES:
                                            break
                                        
                                        # Рендерим текущую страницу PDF в байтовый поток PNG прямо в оперативке
                                        pix = page.get_pixmap(matrix=fitz.Matrix(150/72, 150/72), alpha=False)
                                        img_png_bytes = pix.tobytes("png")
                                        
                                        # Отправляем пиксели страницы в оптическое зрение Лламы
                                        page_desc = await describe_image_with_vision(img_png_bytes)
                                        if page_desc and "ошиб" not in page_desc.lower():
                                            vision_descriptions.append(f"[Страница {i+1} (Графика/Оформление)]: {page_desc.strip()}")
                                        
                                        del pix # Очищаем оперативку на каждой итерации цикла
                                        
                                if vision_descriptions:
                                    combined_vision = "\n".join(vision_descriptions)
                                    file_text = f"--- Визуальный анализ медиа-слоёв (Vision): ---\n{combined_vision}\n\n--- Текстовое содержимое (OCR/Текст): ---\n{file_text}"
                                    
                            except Exception as vision_pdf_err:
                                logger.warning(f"Фоновый Vision-скрининг страниц PDF прерван: {vision_pdf_err}")
                                
                            if not file_text:
                                file_text = "[PDF-документ пуст или не удалось распознать контент страниц]"
                                
                        except Exception as pdf_ocr_err:
                            logger.error(f"Критический сбой фонового OCR PDF: {pdf_ocr_err}")
                            file_text = f"[Ошибка сканирования содержимого PDF: {str(pdf_ocr_err)[:50]}]"

                    elif ext == ".txt":
                        try:
                            file_text = file_bytes.decode("utf-8", errors="ignore")
                        except Exception as txt_err:
                            file_text = f"[Не удалось раскодировать текст: {str(txt_err)[:30]}]"

                    else:
                        # Мультимодальный Vision-анализ для картинок (PNG, JPG, WEBP)
                        try:
                            vision_description = await describe_image_with_vision(file_bytes)
                            file_text = vision_description
                        except Exception:
                            try:
                                ocr_res = pytesseract.image_to_string(Image.open(io.BytesIO(file_bytes)), lang="rus+eng")
                                file_text = ocr_res or ""
                            except Exception:
                                file_text = "[Не удалось распознать изображение]"

                    metadata = (
                        f"[Файл]: {file.filename}\n"
                        f"[Путь]: {safe_file_path}\n"
                        f"[Описание]: {file_text[:1500]}..."
                    )
                    aggregated_file_texts.append(metadata)         
                    
                except Exception as e:
                    logger.error(
                        f"Ошибка обработки файла {getattr(file, 'filename', '')}: {str(e)[:100]}"
                    )
                    aggregated_file_texts.append(
                        f"[Ошибка обработки файла: {getattr(file, 'filename', '')}]"
                    )
                finally:
                    try:
                        await file.close()
                    except Exception:
                        pass

            # === Добавление результата проверки ссылки к ответу модели === #

            extracted_urls = extract_urls_from_text(user_message)
            for url in extracted_urls:
                url_context = await fetch_and_parse_url(url)
                if url_context and url_context.get("html_snippet"):
                    link_metadata = (
                        f"[Ссылка]: {url}\n"
                        f"[Заголовок]: {url_context['title']}\n"
                        f"[Текст]: {url_context['html_snippet']}\n"
                    )
                    aggregated_file_texts.append(link_metadata)

        # === Составление итогового запроса === #

        if aggregated_file_texts:
            user_prompt = "\n\n".join(aggregated_file_texts + [user_message])
        else:
            user_prompt = user_message

        # === Запрос к LLM === #

        secure_history = await hydrate_lazy_context_by_footprint(user_history, user_prompt)
        api_messages = SYSTEM_PROMPT + secure_history + [{"role": "user", "content": user_prompt}]

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
        except asyncio.TimeoutError:
            logger.error("Тайм-аут запроса к Groq (превышен лимит 30с)")
            return {"status": "error", "message": "На сервере произошла ошибка. Пожалуйста, попробуйте позже."}
        except Exception as e:
            logger.error(f"Критическая ошибка в эндпоинте: {str(e)[:100]}")
            return {"status": "error", "message": "На сервере произошла ошибка. Пожалуйста, попробуйте позже."}
        
        # Формируем выжимку для записи в историю чата
        history_user_content = f"[Запрос]: {user_message}\n"
        
        if aggregated_file_texts:
            files_summary = []
            for file_info in aggregated_file_texts:
                lines = file_info.split("\n")
                summary = "\n".join([line for line in lines[:3] if line])
                files_summary.append(summary)
            history_user_content += "--- МЕДИА-КОНДЕНСАЦИЯ ВЛОЖЕНИЙ ---\n" + "\n".join(files_summary)

        # Записываем в долговременную память сессии чата легкий сжатый стейт
        user_history.append({"role": "user", "content": history_user_content.strip()})
        user_history.append({"role": "assistant", "content": assistant_message})

        # === Обрезка истории при превышении лимита и синхронизация с хранилищем === #
        
        if len(user_history) > MAX_HISTORY_MESSAGES:
            sessions_storage[session_id]["messages"] = user_history[-MAX_HISTORY_MESSAGES:]
        else:
            sessions_storage[session_id]["messages"] = user_history

        sessions_storage[session_id]["last_activity"] = time.time()

        # Сбрасываем легкий стейт на жесткий диск сервера
        await save_sessions_to_disk()

        return {"status": "success", "message": assistant_message}
        
@app.get("/")
async def get_site_interface():
    """Возврат HTML интерфейса"""
    return FileResponse("cyberlife_interface.html")


# ============= STARTUP И SHUTDOWN HANDLERS ============= #

@app.on_event("startup")
async def startup_event():
    try:
        await load_sessions_from_disk()
        logger.info("Сессии загружены с диска")
    except Exception as e:
        logger.error(f"Ошибка загрузки сессий при старте: {str(e)[:100]}")

    # Запускаем сборщик мусора и сохраняем таску в state для отмены при shutdown
    garbage_task = asyncio.create_task(memory_garbage_collector())
    app.state.garbage_task = garbage_task
    logger.info("Сборщик мусора запущен")


@app.on_event("shutdown")
async def shutdown_event():
    try:
        # Отменяем таску сборщика мусора
        task = getattr(app.state, "garbage_task", None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass

        await save_sessions_to_disk()
        logger.info("Сессии сохранены при выключении")
    except Exception as e:
        logger.error(f"Ошибка сохранения сессий при выключении: {str(e)[:100]}")

    try:
        await cleanup_temp_files()
        logger.info("Временные файлы очищены")
    except Exception as e:
        logger.error(f"Ошибка очистки временных файлов: {str(e)[:100]}")


# ============= ЗАПУСК ============= #

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )