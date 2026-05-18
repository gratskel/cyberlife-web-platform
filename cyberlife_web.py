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
    
# Инициализация FastAPI сервера Киберлайф
app = FastAPI(title="CyberLife Systems // Connor RK800 API")

# Разрешение браузеру беспрепятственно общаться с сервером
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

# Семафор на 5 одновременных потоков для защиты от блокировок Groq
rate_limiter = asyncio.Semaphore(5)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

class ChatRequest(BaseModel):
    message: str
    history: list

# Текстовый асинхронный шлюз чата
@app.get("/")
async def get_site_interface():
    return FileResponse("cyberlife_interface.html")
@app.post("/api/connor/chat")
async def connor_web_endpoint(payload: ChatRequest):
    async with rate_limiter:
        try:
            messages = [{"role": "system", "content": "You are Connor, an advanced cybernetic android from CyberLife. Your speech must be strictly concise, analytical, cold, and professional. Avoid markdown formatting, lists, tables, or vertical bars (||). Answer any complex programming or SRE questions using strictly 2-3 blunt, solid sentences of plain text. Provide only the absolute core summary, immediately as a direct conclusion. However, if the user explicitly asks to 'explain' or detail a complex architecture, provide a full, exhaustive step-by-step plain text breakdown until the concept is completely resolved, but strictly without any filler words or water. Never cut off mid-sentence. Respond strictly in Russian language."}]            
            
            # Подтягиваем скользящую историю из LocalStorage браузера (последние 15 реплик)
            for msg in payload.history[-15:]:
                messages.append(msg)
                
            messages.append({"role": "user", "content": payload.message})

            # Подключаем ультимативную флагманскую модель gpt120-oss
            completion = await groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.3
            )
            return {"status": "success", "message": completion.choices[0].message.content}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# Мультимодальный шлюз
@app.post("/api/connor/upload")
async def connor_upload_endpoint(file: UploadFile = File(...)):
    async with rate_limiter:
        try:
            file_bytes = await file.read()
            file_extension = file.filename.split(".")[-1].lower()
            extracted_text = ""
            
            # Конвейер обработки документов
            if file_extension == "pdf":
                with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                    # Извлекаем текст первых 25 страниц
                    for page in doc[:25]:
                        extracted_text += page.get_text()
                        
            elif file_extension in ["png", "jpg", "jpeg"]:
                img = Image.open(BytesIO(file_bytes))
                # Пиксельный скан накладных через Tesseract
                extracted_text = pytesseract.image_to_string(img, lang="rus+eng")
            else:
                raise ValueError("Допустимы только PDF и изображения.")

            if not extracted_text.strip():
                raise ValueError("Не удалось извлечь печатный текст из документа.")

            # Отправка извлечённого массива на суммаризацию в Groq
            messages = [
                {"role": "system", "content": "You are Connor, an advanced cybernetic android from CyberLife. Summarize the following extracted document text analytically, strictly, and concisely using your maximum reasoning capabilities. Respond strictly in Russian language."},                
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