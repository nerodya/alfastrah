import json
import os
import time
import threading
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator, ConfigDict

AB_BASE = (os.getenv("AB_BASE", "http://5.35.66.24:3010").rstrip("/"))
AB_API = AB_BASE + "/api"

AB_LOGIN = os.getenv("AB_LOGIN", "Administrator")
AB_PASSWORD = os.getenv("AB_PASSWORD", "1")  # вы зададите сами

# Как формировать query-параметр user для /request/*
# token_uuid (по вашей схеме): user = apitoken.result.uuid
# user_uuid (по спецификации): user = UUID пользователя (если вы его где-то получите/задате)
USER_MODE = os.getenv("AB_REQUEST_USER_MODE", "token_uuid")  # token_uuid | user_uuid
AB_USER_UUID = os.getenv("AB_USER_UUID", "")  # нужно только если USER_MODE=user_uuid

REQUEST_OUTPUT_INDEX = int(os.getenv("AB_OUTPUT_INDEX", "0"))

AI_WEBHOOK_URL = os.getenv("AI_WEBHOOK_URL", "").strip()
AI_REFRESH_WEBHOOK_URL = os.getenv("AI_REFRESH_WEBHOOK_URL", AI_WEBHOOK_URL).strip()
AI_WEBHOOK_TIMEOUT = float(os.getenv("AI_WEBHOOK_TIMEOUT", "600"))

TABS = {
    "underwriter": {"title": "Помощник Андеррайтера", "alias": "альфастр_1"},
    "claims": {"title": "Классификация страховых случаев", "alias": "альфастр_2"},
}

def build_parameters():
    return [{
        "name": "Последовательность чисел",
        "params": {
            "type": "DataSource/NUMERICALSEQ",
            "settings": [{"Column name": "1", "Number of rows": "1"}]
        }
    }]

class AbSession:
    """
    Держим одну shared-сессию (cookie jar) + api token.
    Без БД. Можно расширить до per-user сессий, но вам не нужно.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=60, follow_redirects=True)
        self._token_uuid: Optional[str] = None
        self._token_value: Optional[str] = None

    def _auth_user(self):
        if not AB_PASSWORD:
            raise HTTPException(500, "AB_PASSWORD is not set")

        print("DEBUG: Requesting new cookies from /auth/user...")
        r = self._client.post(
            f"{AB_API}/auth/user",
            data={"login": AB_LOGIN, "password": AB_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            # Если логин не удался, очищаем куки, чтобы при следующем запросе снова попробовать
            self._client.cookies.clear()
            raise HTTPException(r.status_code, f"AB auth failed: {r.text}")

        print(f"DEBUG: Cookies received. Count: {len(self._client.cookies)}")

    def _get_user_info(self) -> dict:
        # /user/info [1]
        r = self._client.get(f"{AB_API}/user/info")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"AB user/info failed: {r.text}")
        return r.json()

    def _list_tokens(self) -> list:
        # /apitoken/list [1]
        r = self._client.get(f"{AB_API}/apitoken/list")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"AB apitoken/list failed: {r.text}")
        data = r.json()
        return data.get("result", []) or []

    def _get_token(self, token_id: int) -> dict:
        # /apitoken/get?id= [1]
        r = self._client.get(f"{AB_API}/apitoken/get", params={"id": token_id})
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"AB apitoken/get failed: {r.text}")
        return r.json()

    def _create_token(self, user_id: int) -> dict:
        # /apitoken/create [1]
        payload = {"userID": user_id, "description": "Service session token"}
        r = self._client.post(f"{AB_API}/apitoken/create", json=payload)
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"AB apitoken/create failed: {r.text}")
        return r.json()

    def _ensure_token(self):
        if self._token_uuid and self._token_value:
            return

        user_info = self._get_user_info()
        user_id = user_info.get("result", {}).get("id")
        if not user_id:
            raise HTTPException(500, "AB user/info did not return result.id")

        tokens = self._list_tokens()
        chosen = None
        for t in tokens:
            if t.get("isDefault") and not t.get("disabled"):
                chosen = t
                break
        if chosen is None:
            for t in tokens:
                if not t.get("disabled"):
                    chosen = t
                    break

        if chosen is None:
            token_resp = self._create_token(user_id)  # /apitoken/create [1]
        else:
            token_resp = self._get_token(chosen["id"])  # /apitoken/get?id=... [1]

        result = token_resp.get("result", {})
        self._token_uuid = result.get("uuid")  # uuid токена [1]
        self._token_value = result.get("token")  # 32-символьный токен [1]

        if not self._token_uuid or not self._token_value:
            raise HTTPException(500, f"AB apitoken response missing uuid/token: {token_resp}")

    def ensure_ready(self):
        """
        Проверяет физическое наличие кук в клиенте и наличие токенов в памяти.
        """
        with self._lock:
            # 1. Проверяем, есть ли хоть одна кука в клиенте
            has_cookies = len(self._client.cookies) > 0

            # 2. Проверяем, есть ли токены (UUID и Значение)
            has_tokens = self._token_uuid and self._token_value

            if has_cookies and has_tokens:
                # Все на месте, ничего не делаем
                return

            print(f"DEBUG: Session incomplete. Cookies: {has_cookies}, Tokens: {has_tokens}")

            # Если кук нет — логинимся
            if not has_cookies:
                self._auth_user()

            # Если токенов нет (даже если куки появились) — получаем их
            if not (self._token_uuid and self._token_value):
                self._ensure_token()

    def call(self, method: str, path: str, *, params=None, json=None, data=None, headers=None, timeout=60):
        self.ensure_ready()

        def do_call():
            return self._client.request(
                method,
                f"{AB_API}{path}",
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=timeout,
            )

        r = do_call()

        # Если AB вернул 401, значит куки стали невалидными (истекли или удалены на сервере)
        if (r.status_code == 401 or r.status_code == 400) and path != "/auth/user":
            print("DEBUG: 401 Unauthorized. Clearing cookies and retrying...")
            with self._lock:
                self._client.cookies.clear()  # Явно удаляем старые куки
                self._auth_user()
                self._ensure_token()
            r = do_call()

        return r

    def _request_user_param(self) -> str:
        """
        Определяет, какой UUID использовать в качестве параметра 'user' для /request/*
        """
        if USER_MODE == "user_uuid":
            if not AB_USER_UUID:
                raise HTTPException(
                    500,
                    "AB_USER_UUID is not set but AB_REQUEST_USER_MODE=user_uuid"
                )
            return AB_USER_UUID

        # USER_MODE == "token_uuid" (ваш вариант)
        if not self._token_uuid:
            raise HTTPException(500, "Token UUID not initialized. Session is not ready.")
        return self._token_uuid

    def request_params_with_token(self, extra: dict) -> dict:
        # Теперь метод _request_user_param существует, и эта строка будет работать
        return {
            "user": self._request_user_param(),
            "token": self._token_value,
            **extra
        }

class ChatSendRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sessionId: str = Field(..., min_length=1)
    chatInput: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def resolve_chat_input(self):
        text = (self.chatInput or self.message or "").strip()
        if not text:
            raise ValueError("chatInput is required")
        self.chatInput = text
        return self


class ChatRefreshRequest(BaseModel):
    force: bool = False


def _call_webhook(
    url: str,
    payload: dict[str, Any] | list[Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    if not url:
        raise HTTPException(500, "AI_WEBHOOK_URL is not set")

    read_timeout = timeout if timeout is not None else AI_WEBHOOK_TIMEOUT
    http_timeout = httpx.Timeout(
        connect=30.0,
        read=read_timeout,
        write=30.0,
        pool=30.0,
    )

    try:
        with httpx.Client(timeout=http_timeout, follow_redirects=True) as client:
            r = client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Webhook request failed: {exc}") from exc

    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Webhook error: {r.text}")

    content_type = r.headers.get("content-type", "")
    if "application/json" in content_type:
        data = r.json()
        return data if isinstance(data, dict) else {"result": data}

    text = r.text.strip()
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {"result": parsed}
        except json.JSONDecodeError:
            return {"reply": text}

    return {"reply": ""}


def _extract_reply(data: dict[str, Any]) -> str:
    for key in ("output", "reply", "answer", "message", "text", "content", "response"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            text = str(value).strip()
            if text:
                return text

    result = data.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    if isinstance(result, dict):
        nested = _extract_reply(result)
        if nested:
            return nested

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

    return ""


ab = AbSession()
app = FastAPI(title="AB UI+Proxy")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/run/{tab_key}")
def run(tab_key: str):
    if tab_key not in TABS:
        raise HTTPException(404, "Unknown tab")

    # 1. СНАЧАЛА принудительно инициализируем сессию и токены
    ab.ensure_ready()

    payload = {
        "application": TABS[tab_key]["alias"],
        "parameters": build_parameters(),
    }

    # 2. Теперь, когда токены в памяти, формируем параметры
    params = ab.request_params_with_token({})

    r = ab.call(
        "POST",
        "/request/add",
        params=params,
        json=payload
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/status/{request_id}")
def status(request_id: int):
    ab.ensure_ready()  # <-- важно
    r = ab.call(
        "GET",
        "/request/get",
        params=ab.request_params_with_token({"id": request_id}),
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()  # result.status, result.progress [1]

@app.get("/api/result/{request_id}")
def result_csv(request_id: int, output: int):
    ab.ensure_ready()  # <-- важно
    r = ab.call(
        "GET",
        "/request/download/csv",
        params=ab.request_params_with_token({"id": request_id, "output": output}),
        timeout=120
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return Response(content=r.content, media_type="text/csv; charset=utf-8")  # [1]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)


@app.post("/api/chat/send")
def chat_send(body: ChatSendRequest):
    payload = {
        "sessionId": body.sessionId,
        "action": "sendMessage",
        "chatInput": body.chatInput,
    }
    data = _call_webhook(AI_WEBHOOK_URL, payload)
    reply = _extract_reply(data)
    if not reply:
        raise HTTPException(502, "Webhook did not return a reply")
    return {"reply": reply, "output": reply, "raw": data}


@app.post("/api/chat/refresh")
def chat_refresh(body: ChatRefreshRequest):
    # Заглушка: имитация обновления документов в базе.
    time.sleep(2)
    return {
        "message": "Документы в базе успешно обновлены.",
        "raw": {"stub": True, "force": body.force},
    }