import os
import asyncio
import json
import time
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from groq import AsyncGroq
from fastapi.responses import FileResponse

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("Ошибка: ключ GROQ_API_KEY не обнаружен")

app = FastAPI(title="CyberLife Systems // Connor RK800 API")

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

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
rate_limiter = asyncio.Semaphore(5)
sessions_lock = asyncio.Lock()

SESSIONS_FILE = Path("sessions_storage.json")
MAX_MESSAGE_SIZE = 30 * 1024  # 30 KB

SYSTEM_PROMPT = [{"role": "system", "content": (
    "You are Connor, an advanced cybernetic android from CyberLife. "
    "Your speech must be strictly concise, analytical, cold, and professional. "
    "Avoid markdown formatting, lists, tables, or vertical bars (||). "
    "Answer any complex programming or SRE questions using strictly 2-3 blunt, solid sentences of plain text. "
    "Provide only the absolute core summary, immediately as a direct conclusion. "
    "However, if the user explicitly asks to 'explain' or detail a complex architecture, "
    "provide a full, exhaustive step-by-step plain text breakdown until the concept is completely resolved, "
    "but strictly without any filler words or water. Never cut off mid-sentence. Respond strictly in Russian language."
)}]

sessions_storage = {}
logger = logging.getLogger("uvicorn.error")

def load_sessions_from_disk():
    global sessions_storage
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                sessions_storage = json.load(f)
        except Exception as e:
            sessions_storage = {}
    else:
        sessions_storage = {}

def save_sessions_to_disk():
    try:
        temp_file = Path("sessions_storage.json.tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(sessions_storage, f, ensure_ascii=False, indent=2)
        temp_file.replace(SESSIONS_FILE)
    except (IOError, OSError) as err:
        logger.error(f"Критическая ошибка записи на диск: {err}")

async def memory_garbage_collector():
    while True:
        await asyncio.sleep(3600)
        async with sessions_lock:
            current_time = time.time()
            expired_sessions = [
                sid for sid, data in sessions_storage.items() 
                if current_time - data.get("last_activity", 0) > 604800
            ]
            for sid in expired_sessions:
                if sid in sessions_storage:
                    del sessions_storage[sid]
            save_sessions_to_disk()

@app.on_event("startup")
async def startup_event():
    load_sessions_from_disk()
    asyncio.create_task(memory_garbage_collector())

@app.get("/")
async def get_site_interface():
    return FileResponse("cyberlife_interface.html")

@app.post("/api/connor/chat")
async def connor_web_endpoint(request: Request):
    async with rate_limiter:
        async with sessions_lock:
            try:
                payload = await request.json()
                action = payload.get("action", "chat")
                session_id = payload.get("session_id", "anonymous_default_session")
                
                if action == "clear":
                    if session_id in sessions_storage:
                        del sessions_storage[session_id]
                        save_sessions_to_disk()
                    return {
                        "status": "success",
                        "message": "Вся история текущей сессии полностью уничтожена."
                    }
                    
                elif action == "branch":
                    edited_index = payload.get("index")
                    if edited_index is not None and session_id in sessions_storage:
                        if isinstance(edited_index, int) and edited_index >= 0:
                            messages_count = len(sessions_storage[session_id]["messages"])
                            if edited_index > messages_count:
                                return {
                                    "status": "error",
                                    "message": f"Индекс {edited_index} превышает количество сообщений ({messages_count})."
                                }
                            sessions_storage[session_id]["messages"] = sessions_storage[session_id]["messages"][:edited_index]
                            sessions_storage[session_id]["last_activity"] = time.time()
                            save_sessions_to_disk()
                            return {
                                "status": "success",
                                "message": f"Контекст успешно обрезан до индекса {edited_index}."
                            }
                        else:
                            return {"status": "error", "message": "Индекс должен быть неотрицательным целым числом."}
                    return {"status": "error", "message": "Индекс не передан или сессия пуста."}
                    
                elif action == "chat":
                    user_message = payload.get("message", "").strip()
                    if not user_message:
                        return {"status": "error", "message": "Пакет данных пуст."}
                    
                    if len(user_message) > MAX_MESSAGE_SIZE:
                        return {"status": "error", "message": "Сообщение слишком большое. Максимум 30 KB."}
                        
                    structured_context = user_message
                    
                    if "[Анализ данных" in user_message:
                        try:
                            if "]:\n" in user_message:
                                raw_file_content = user_message.split("]:\n", 1)[1].strip()
                                try:
                                    parsed_json = json.loads(raw_file_content)
                                    pretty_json = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                                    structured_context = f"{user_message.split(']:')[0]}]:\nJSON-контент файла:\n{pretty_json}"
                                except json.JSONDecodeError:
                                    pass
                        except Exception:
                            pass

                    if session_id not in sessions_storage:
                        sessions_storage[session_id] = {
                            "messages": [],
                            "last_activity": time.time()
                        }

                    sessions_storage[session_id]["last_activity"] = time.time()
                    user_history = sessions_storage[session_id]["messages"]
                    
                    api_messages = SYSTEM_PROMPT + user_history + [{"role": "user", "content": structured_context}]
                    
                    completion = await groq_client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=api_messages,
                        temperature=0.3
                    )
                    
                    assistant_message = completion.choices[0].message.content
                    
                    sessions_storage[session_id]["messages"].append({"role": "user", "content": structured_context})
                    sessions_storage[session_id]["messages"].append({"role": "assistant", "content": assistant_message})
                    
                    save_sessions_to_disk()
                   
                    return {"status": "success", "message": assistant_message}
                    
            except Exception as e:
                return {"status": "error", "message": "На сервере произошла ошибка. Попробуйте позже."}