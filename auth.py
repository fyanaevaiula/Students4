import secrets

import redis
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from repository import UserRepository, User

# ---------- Redis-клиент для сессий ----------

SESSION_PREFIX = "session:"
SESSION_TTL = 86400  # 24 часа

redis_sessions = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


def _create_session(user_id: int) -> str:
    """Создаёт новую сессию и сохраняет в Redis."""
    token = secrets.token_hex(32)
    redis_sessions.setex(f"{SESSION_PREFIX}{token}", SESSION_TTL, str(user_id))
    return token


def _get_user_id(token: str) -> int | None:
    """Возвращает user_id по токену или None, если сессия истекла/не найдена."""
    try:
        user_id = redis_sessions.get(f"{SESSION_PREFIX}{token}")
        return int(user_id) if user_id else None
    except redis.ConnectionError:
        return None


def _delete_session(token: str) -> None:
    """Удаляет сессию из Redis."""
    try:
        redis_sessions.delete(f"{SESSION_PREFIX}{token}")
    except redis.ConnectionError:
        pass

# ---------- Схемы ----------

class AuthCredentials(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)

class TokenOut(BaseModel):
    user_id: int
    token: str

class MessageOut(BaseModel):
    detail: str

# ---------- Зависимости ----------

# В Swagger появляется кнопка Authorize с полем X-Auth-Token
token_header = APIKeyHeader(name="X-Auth-Token", auto_error=False)

user_repo = UserRepository()

def get_user_repo() -> UserRepository:
    return user_repo

def get_token(x_auth_token: str | None = Depends(token_header)) -> str:
    if not x_auth_token:
        raise HTTPException(
            status_code=401,
            detail="Не передан заголовок X-Auth-Token. Сначала выполните вход через /auth/login",
        )
    return x_auth_token

def get_current_user(
    token: str = Depends(get_token),
    repo: UserRepository = Depends(get_user_repo),
) -> User:
    """По токену определяет идентификатор пользователя и возвращает его из БД.
    Это и есть авторизация по идентификатору: токен — ключ к user.id."""
    user_id = _get_user_id(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Сессия не найдена или завершена")
    user = repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user

auth_router = APIRouter(prefix="/auth", tags=["auth"])

# ---------- Эндпоинты ----------

@auth_router.post("/register", response_model=MessageOut, status_code=201)
def register(
    credentials: AuthCredentials,
    repo: UserRepository = Depends(get_user_repo),
):
    """Регистрация нового пользователя."""
    try:
        user = repo.create_user(credentials.username, credentials.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return MessageOut(detail=f"Пользователь '{user.username}' зарегистрирован, id = {user.id}")

@auth_router.post("/login", response_model=TokenOut)
def login(
    credentials: AuthCredentials,
    repo: UserRepository = Depends(get_user_repo),
):
    """Вход: проверяет логин и пароль, выдаёт сессионный токен."""
    user = repo.verify_credentials(credentials.username, credentials.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = _create_session(user.id)
    return TokenOut(user_id=user.id, token=token)

@auth_router.post("/logout", response_model=MessageOut)
def logout(
    token: str = Depends(get_token),
    user: User = Depends(get_current_user),
):
    """Выход: сессия по идентификатору пользователя удаляется."""
    _delete_session(token)
    return MessageOut(detail=f"Пользователь '{user.username}' вышел из системы")
