import csv
import hashlib
import secrets

from sqlalchemy import create_engine, String, Integer, select, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

# ---------- 1. Модель данных ----------

class Base(DeclarativeBase):
    pass

class StudentGrade(Base):
    """Запись таблицы students.csv: студент, факультет, курс и оценка."""
    __tablename__ = "student_grades"
    __table_args__ = (
        UniqueConstraint(
            "last_name", "first_name", "faculty", "course",
            name="uq_student_faculty_course"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    faculty: Mapped[str] = mapped_column(String(100), nullable=False)
    course: Mapped[str] = mapped_column(String(200), nullable=False)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return (f"<StudentGrade {self.last_name} {self.first_name}, "
                f"{self.faculty}, {self.course}: {self.grade}>")


class User(Base):
    """Пользователь сервиса: уникальный логин и хеш пароля."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    def __repr__(self) -> str:
        return f"<User {self.id}: {self.username}>"


# ---------- 2. Репозиторий студентов: INSERT, SELECT, UPDATE, DELETE ----------

class StudentRepository:
    def __init__(self, db_url: str = "sqlite:///students.db"):
        self.engine = create_engine(db_url)
        Base.metadata.create_all(self.engine)

    # --- INSERT ---
    def insert(self, record: dict) -> StudentGrade:
        """Добавляет одну запись. Если студент уже проходил этот курс
        на этом факультете — обновляет оценку (чтобы избежать дублей)."""
        with Session(self.engine) as session:
            existing = session.execute(
                select(StudentGrade).where(
                    StudentGrade.last_name == record["last_name"],
                    StudentGrade.first_name == record["first_name"],
                    StudentGrade.faculty == record["faculty"],
                    StudentGrade.course == record["course"],
                )
            ).scalar_one_or_none()
            if existing:
                existing.grade = record["grade"]
                obj = existing
            else:
                obj = StudentGrade(**record)
                session.add(obj)
            session.commit()
            session.refresh(obj)
            return obj

    def insert_many(self, records: list[dict]) -> None:
        for record in records:
            self.insert(record)

    # --- SELECT ---
    def select_all(self) -> list[StudentGrade]:
        with Session(self.engine) as session:
            return list(session.scalars(select(StudentGrade)))

    def select_by_id(self, record_id: int) -> StudentGrade | None:
        """Возвращает запись по id или None, если её нет."""
        with Session(self.engine) as session:
            return session.get(StudentGrade, record_id)

    # --- UPDATE ---
    def update(self, record_id: int, updates: dict) -> StudentGrade | None:
        """Обновляет поля существующей записи. Возвращает обновлённый
        объект или None, если записи с таким id нет."""
        with Session(self.engine) as session:
            obj = session.get(StudentGrade, record_id)
            if obj is None:
                return None
            for field, value in updates.items():
                setattr(obj, field, value)
            session.commit()
            session.refresh(obj)
            return obj

    # --- DELETE ---
    def delete(self, record_id: int) -> bool:
        """Удаляет запись по id. Возвращает True, если запись была
        найдена и удалена, иначе False."""
        with Session(self.engine) as session:
            obj = session.get(StudentGrade, record_id)
            if obj is None:
                return False
            session.delete(obj)
            session.commit()
            return True

    # ---------- 4. Методы для списков и статистики ----------

    def students_by_faculty(self, faculty: str) -> list[str]:
        """Список студентов (фамилия + имя) по названию факультета."""
        with Session(self.engine) as session:
            stmt = (
                select(StudentGrade.last_name, StudentGrade.first_name)
                .where(StudentGrade.faculty == faculty)
                .distinct()
                .order_by(StudentGrade.last_name, StudentGrade.first_name)
            )
            return [f"{ln} {fn}" for ln, fn in session.execute(stmt)]

    def unique_courses(self) -> list[str]:
        """Список уникальных курсов."""
        with Session(self.engine) as session:
            stmt = select(StudentGrade.course).distinct().order_by(StudentGrade.course)
            return list(session.scalars(stmt))

    def students_failing_course(self, course: str, threshold: int = 30) -> list[dict]:
        """Студенты по выбранному курсу с оценкой ниже threshold."""
        with Session(self.engine) as session:
            stmt = (
                select(StudentGrade.last_name, StudentGrade.first_name, StudentGrade.grade)
                .where(StudentGrade.course == course, StudentGrade.grade < threshold)
                .order_by(StudentGrade.grade)
            )
            return [
                {"last_name": ln, "first_name": fn, "grade": g}
                for ln, fn, g in session.execute(stmt)
            ]

    def average_grade_by_faculty(self, faculty: str) -> float | None:
        """Средний балл по факультету."""
        with Session(self.engine) as session:
            avg = session.scalar(
                select(func.avg(StudentGrade.grade))
                .where(StudentGrade.faculty == faculty)
            )
            return round(avg, 2) if avg is not None else None


# ---------- 3. Заполнение модели из students.csv ----------

def load_students_from_csv(repo: StudentRepository, path: str = "students.csv") -> None:
    """Читает CSV и вставляет записи в базу через репозиторий."""
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=",")
        records = [
            {
                "last_name": row["Фамилия"].strip(),
                "first_name": row["Имя"].strip(),
                "faculty": row["Факультет"].strip(),
                "course": row["Курс"].strip(),
                "grade": int(row["Оценка"]),
            }
            for row in reader
        ]
    repo.insert_many(records)
    print(f"Загружено строк: {len(records)}")


# ---------- 4. Хеширование паролей ----------

def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    return secrets.compare_digest(_hash_password(password, salt), stored)


# ---------- 5. Репозиторий пользователей ----------

class UserRepository:
    def __init__(self, db_url: str = "sqlite:///users.db"):
        self.engine = create_engine(db_url)
        Base.metadata.create_all(self.engine)

    def create_user(self, username: str, password: str) -> User:
        """Создаёт пользователя. Если логин занят — бросает ValueError."""
        with Session(self.engine) as session:
            existing = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if existing:
                raise ValueError("Пользователь с таким именем уже существует")
            user = User(username=username, password_hash=_hash_password(password))
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def verify_credentials(self, username: str, password: str) -> User | None:
        """Возвращает пользователя, если логин и пароль совпадают, иначе None."""
        with Session(self.engine) as session:
            user = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
        if user is None or not _verify_password(password, user.password_hash):
            return None
        return user

    def get_by_id(self, user_id: int) -> User | None:
        with Session(self.engine) as session:
            return session.get(User, user_id)
