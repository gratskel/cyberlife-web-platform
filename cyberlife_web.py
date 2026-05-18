import os
import asyncio
import fitz 
import pytesseract 
from PIL import Image
from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from groq import AsyncGroq
from fastapi.responses import FileResponse

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("АВАРИЯ: Ключ GROQ_API_KEY не обнаружен")
    
# 2. Инициализация FastAPI сервера Киберлайф
app = FastAPI(title="CyberLife Systems // Connor RK800 API")

# Разрешаем браузеру Самсунга беспрепятственно общаться с сервером
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cyberlife.outlawassistant.online",
        "https://cyberlife.outlawassistant.online:2053"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Твой калёный семафор на 5 одновременных потоков для защиты от блокировок Groq
rate_limiter = asyncio.Semaphore(5)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

class ChatRequest(BaseModel):
    message: str
    history: list

# 3. Текстовый асинхронный шлюз чата (Ядро GPT120-OSS)
@app.get("/")
async def get_site_interface():
    return FileResponse("cyberlife_interface.html")
@app.post("/api/connor/chat")
async def connor_web_endpoint(payload: ChatRequest):
    async with rate_limiter:
        try:
            messages = [{"role": "system", "content": "You are Connor, the android from CyberLife. Tone: analytical, professional, strict, ultra-intelligent RK800 core. Designed by Gratskel."}]
            
            # Подтягиваем скользящую историю из LocalStorage браузера (последние 15 реплик)
            for msg in payload.history[-15:]:
                messages.append(msg)
                
            messages.append({"role": "user", "content": payload.message})

            # Подключаем ультимативную флагманскую модель gpt120-oss
            completion = await groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.3  # Жёсткий детерминированный тон андроида без галлюцинаций
            )
            return {"status": "success", "message": completion.choices[0].message.content}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# 4. МУЛЬТИМОДАЛЬНЫЙ ШЛЮЗ: Твоя вчерашняя логика PDF + OCR (Ядро GPT120-OSS)
@app.post("/api/connor/upload")
async def connor_upload_endpoint(file: UploadFile = File(...)):
    async with rate_limiter:
        try:
            file_bytes = await file.read()
            file_extension = file.filename.split(".")[-1].lower()
            extracted_text = ""
            
            # Твой пуленепробиваемый конвейер обработки документов
            if file_extension == "pdf":
                with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                    # Извлекаем текст первых 25 страниц, как ты ювелирно настроила
                    for page in doc[:25]:
                        extracted_text += page.get_text()
                        
            elif file_extension in ["png", "jpg", "jpeg"]:
                img = Image.open(BytesIO(file_bytes))
                # Твой калёный пиксельный скан накладных через Tesseract
                extracted_text = pytesseract.image_to_string(img, lang="rus+eng")
            else:
                raise ValueError("Допустимы только PDF и изображения.")

            if not extracted_text.strip():
                raise ValueError("Не удалось извлечь печатный текст из документа.")

            # Отправка извлечённого массива на суммаризацию в Грок
            messages = [
                {"role": "system", "content": "You are Connor, an android from CyberLife. Summarize the following extracted document text analytically, strictly, and concisely using your maximum reasoning capabilities."},
                {"role": "user", "content": f"Document content:\n{extracted_text[:12000]}"}  # Расширили лимит благодаря gpt120-oss!
            ]

            # Перевод суммаризации на ультимативное мышление gpt120-oss
            completion = await groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.3
            )
            return {"status": "success", "message": completion.choices.message.content}

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Ошибка шлюза: {str(e)}")