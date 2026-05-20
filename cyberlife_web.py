import os
import asyncio
import json
from fastapi import FastAPI, HTTPException, Request
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

# Глобальный асинхронный буфер истории диалога в оперативной памяти
messages_history = []

@app.get("/")
async def get_site_interface():
    return FileResponse("cyberlife_interface.html")

# Единственный роут чата, защищённый Nginx
@app.post("/api/connor/chat")
async def connor_web_endpoint(request: Request):
    global messages_history
    async with rate_limiter:
        try:
            # Считываем легкий JSON-пакет, прилетевший с фронтенда
            payload = await request.json()
            action = payload.get("action", "chat")

            # Тотальное удаление всех сообщений и полная очистка ОЗУ
            if action == "clear":
                messages_history.clear()
                return {
                    "status": "success",
                    "message": "Вся история чата и контекст памяти успешно очищены."
                }

            # Глубокое редактирование сообщений, ветвление и срез истории в ОЗУ
            elif action == "branch":
                edited_index = payload.get("index")
                if edited_index is not None:
                    # Чистый срез массива. Коннор забывает всё после индекса
                    messages_history = messages_history[:edited_index]
                    return {
                        "status": "success",
                        "message": f"Контекст CyberLife успешно обрезан до индекса {edited_index}"
                    }
                return {"status": "error", "message": "Индекс пакета данных не передан"}

            # 3. Асинхронный чат
            elif action == "chat":
                user_message = payload.get("message", "").strip()
                
                if not user_message:
                    return {"status": "error", "message": "Пакет данных пуст. Нечего анализировать."}

                structured_context = user_message
                
                if "[Анализ данных" in user_message:
                    try:
                        if "]:\n" in user_message:
                            raw_file_content = user_message.split("]:\n", 1)[1].strip()
                            
                            # Автоматически вытаскиваем чистый JSON-массив логов, если структура совпадает
                            try:
                                parsed_json = json.loads(raw_file_content)
                                pretty_json = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                                structured_context = f"{user_message.split(']:')[0]}]:\nJSON-контент файла:\n{pretty_json}"
                            except json.JSONDecodeError:
                                # Обычные текстовые логи .LOG / .TXT пропускаем как есть
                                pass
                    except Exception:
                        pass

                # Системный промпт андроида
                system_prompt = [{"role": "system", "content": (
                    "You are Connor, an advanced cybernetic android from CyberLife. "
                    "Your speech must be strictly concise, analytical, cold, and professional. "
                    "Avoid markdown formatting, lists, tables, or vertical bars (||). "
                    "Answer any complex programming or SRE questions using strictly 2-3 blunt, solid sentences of plain text. "
                    "Provide only the absolute core summary, immediately as a direct conclusion. "
                    "However, if the user explicitly asks to 'explain' or detail a complex architecture, "
                    "provide a full, exhaustive step-by-step plain text breakdown until the concept is completely resolved, "
                    "but strictly without any filler words or water. Never cut off mid-sentence. Respond strictly in Russian language."
                )}]

                # Сборка полного контекста диалога
                api_messages = system_prompt + messages_history + [{"role": "user", "content": structured_context}]

                # Подключаем флагманское мышление gpt120-oss в Groq
                completion = await groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=api_messages,
                    temperature=0.3
                )

                # Сохраняем реплики в глобальную память сервера для поддержания контекста сессии
                messages_history.append({"role": "user", "content": structured_context})
                messages_history.append({"role": "assistant", "content": completion.choices[0].message.content})

                return {"status": "success", "message": completion.choices[0].message.content}

        except Exception as e:
            # Аппаратный оборонительный контур защиты
            return {"status": "error", "message": f"Системная ошибка ОЗУ сервера: {str(e)}"}
            