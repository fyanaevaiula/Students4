import json
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from repository import StudentRepository, load_students_from_csv, StudentGrade
from auth import auth_router, get_current_user

# ---------- Схемы Pydantic ----------

class StudentCreate(BaseModel):
    last_name: str = Field(..., max_length=100)
    first_name: str = Field(..., max_length=100)
    faculty: str = Field(..., max_length=100)
    course: str = Field(..., max_length=200)
    grade: int = Field(..., ge=0, le=100)

class StudentUpdate(BaseModel):
    last_name: str | None = Field(None, max_length=100)
    first_name: str | None = Field(None, max_length=100)
    faculty: str | None = Field(None, max_length=100)
    course: str | None = Field(None, max_length=200)
    grade: int | None = Field(None, ge=0, le=100)

class StudentOut(StudentCreate):
    id: int

class DeleteRequest(BaseModel):
    ids: list[int]

# ---------- Подключение репозитория к FastAPI ----------

repo = StudentRepository()

def get_repo() -> StudentRepository:
    return repo

# ---------- Подключение Redis ----------

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# ---------- Кеширование ----------

def get_cache(key: str):
    """Получает данные из Redis-кэша. Возвращает None, если ключ не найден."""
    try:
        data = redis_client.get(key)
        if data is not None:
            return json.loads(data)
    except redis.ConnectionError:
        pass
    return None

def set_cache(key: str, data: dict | list, expire: int = 300):
    """Сохраняет данные в Redis-кэш с TTL (по умолчанию 5 минут)."""
    try:
        redis_client.setex(key, expire, json.dumps(data, ensure_ascii=False))
    except redis.ConnectionError:
        pass

def invalidate_cache(pattern: str):
    """Сбрасывает кэш по маске ключей."""
    try:
        keys = redis_client.keys(pattern)
        if keys:
            redis_client.delete(*keys)
    except redis.ConnectionError:
        pass

def make_cache_key(prefix: str, **kwargs) -> str:
    """Формирует ключ кэша из префикса и параметров."""
    parts = [f"{k}={v}" for k, v in sorted(kwargs.items())]
    return f"{prefix}:{','.join(parts)}" if parts else prefix

# ---------- Lifespan ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if Path("students.csv").exists():
        load_students_from_csv(repo, "students.csv")
    yield

app = FastAPI(title="Students API", lifespan=lifespan)
app.include_router(auth_router)

def to_out(obj: StudentGrade) -> StudentOut:
    return StudentOut(
        id=obj.id,
        last_name=obj.last_name,
        first_name=obj.first_name,
        faculty=obj.faculty,
        course=obj.course,
        grade=obj.grade,
    )

# ---------- Фоновые задачи ----------

def _load_csv_background(file_path: str):
    """Фоновая задача: наполнение БД данными из CSV."""
    load_students_from_csv(repo, file_path)

def _delete_records_background(ids: list[int]):
    """Фоновая задача: удаление записей из БД по списку ID."""
    for record_id in ids:
        repo.delete(record_id)
    invalidate_cache("students:*")

# ---------- CRUD-эндпоинты (только для авторизованных) ----------

@app.post("/students", response_model=StudentOut, status_code=201,
          dependencies=[Depends(get_current_user)])
def create_student(data: StudentCreate, repository: StudentRepository = Depends(get_repo)):
    obj = repository.insert(data.model_dump())
    return to_out(obj)

@app.get("/students", response_model=list[StudentOut],
         dependencies=[Depends(get_current_user)])
def read_students(repository: StudentRepository = Depends(get_repo)):
    cache_key = make_cache_key("students:all")
    cached = get_cache(cache_key)
    if cached is not None:
        return cached
    result = [to_out(o) for o in repository.select_all()]
    set_cache(cache_key, [s.model_dump() for s in result])
    return result

@app.get("/students/{record_id}", response_model=StudentOut,
         dependencies=[Depends(get_current_user)])
def read_student(record_id: int, repository: StudentRepository = Depends(get_repo)):
    cache_key = make_cache_key("students:by_id", id=record_id)
    cached = get_cache(cache_key)
    if cached is not None:
        return StudentOut(**cached)
    obj = repository.select_by_id(record_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    result = to_out(obj)
    set_cache(cache_key, result.model_dump())
    return result

@app.put("/students/{record_id}", response_model=StudentOut,
         dependencies=[Depends(get_current_user)])
def update_student(
    record_id: int,
    data: StudentUpdate,
    repository: StudentRepository = Depends(get_repo),
):
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Пустое тело запроса")
    obj = repository.update(record_id, updates)
    if obj is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    result = to_out(obj)
    invalidate_cache("students:by_id")
    invalidate_cache("students:all")
    return result

@app.delete("/students/{record_id}",
            dependencies=[Depends(get_current_user)])
def delete_student(record_id: int, repository: StudentRepository = Depends(get_repo)):
    if not repository.delete(record_id):
        raise HTTPException(status_code=404, detail="Запись не найдена")
    invalidate_cache("students:by_id")
    invalidate_cache("students:all")
    return {"detail": "Запись удалена"}

# ---------- Эндпоинт наполнения БД из CSV (фоновая задача) ----------

@app.post("/students/import-csv", dependencies=[Depends(get_current_user)])
def import_students_from_csv(
    file_path: str,
    background_tasks: BackgroundTasks,
):
    """Запускает фоновую задачу наполнения БД данными из CSV-файла."""
    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail=f"Файл не найден: {file_path}")
    background_tasks.add_task(_load_csv_background, file_path)
    return {"detail": f"Загрузка данных из '{file_path}' запущена в фоновом режиме"}

# ---------- Эндпоинт удаления записей по списку (фоновая задача) ----------

@app.post("/students/delete-many", dependencies=[Depends(get_current_user)])
def delete_many_students(
    request: DeleteRequest,
    background_tasks: BackgroundTasks,
):
    """Запускает фоновую задачу удаления записей по переданному списку ID."""
    if not request.ids:
        raise HTTPException(status_code=400, detail="Список ID не может быть пустым")
    background_tasks.add_task(_delete_records_background, request.ids)
    return {"detail": f"Удаление {len(request.ids)} записей запущено в фоновом режиме"}
