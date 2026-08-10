# -*- coding: utf-8 -*-
"""
ДОНОЯК TJ — Онлайн-платформа тестов и подготовки
Python 3.12 + Flask
"""

import os
import json
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    send_file,
    Flask, render_template, request, redirect, url_for, flash,
    session, jsonify, send_from_directory, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# ==================== КОНФИГУРАЦИЯ ====================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hgu-test-secret-key-change-in-production-2026")
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "doc", "docx", "txt", "mp3", "ogg", "webm", "wav", "m4a"}

# Railway / Render / любой reverse-proxy: корректные HTTPS, scheme, host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# За продакшеном (Railway) — cookie только по HTTPS
_IS_CLOUD = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("RENDER")
)
if _IS_CLOUD:
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PREFERRED_URL_SCHEME"] = "https"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)

# Реквизиты карты администратора (замените на реальные)
ADMIN_CARD = {
    "dc": "5058 XXXX XXXX XXXX",
    "eskhata": "5058 XXXX XXXX XXXX",
    "alif": "5058 XXXX XXXX XXXX",
    "holder": "Администратор ДОНОЯК TJ",
    "phone": "+992 XX XXX XX XX"
}

# Ссылка на Instagram (замените)
INSTAGRAM_URL = "https://www.instagram.com/"

# Пакеты Pro (без VPS — оплата вручную, админ одобряет)
PRO_PACKAGES = {
    "1m": {"days": 30, "price": 7, "hints": 3, "label": {"ru": "1 месяц — 3 подсказки", "en": "1 month — 3 hints", "tg": "1 моҳ — 3 ишора"}},
    "2m": {"days": 60, "price": 10, "hints": 5, "label": {"ru": "2 месяца — 5 подсказок", "en": "2 months — 5 hints", "tg": "2 моҳ — 5 ишора"}},
    "6m": {"days": 180, "price": 25, "hints": 10, "label": {"ru": "6 месяцев — 10 подсказок", "en": "6 months — 10 hints", "tg": "6 моҳ — 10 ишора"}},
}
HINTS_PACK = {"price": 20, "hints": 10, "label": {"ru": "10 подсказок — 20 сомони", "en": "10 hints — 20 TJS", "tg": "10 ишора — 20 сомонӣ"}}
POINTS_CORRECT = 2
POINTS_WRONG = 0


def get_payment_settings():
    """Реквизиты из БД (редактирует админ), иначе значения по умолчанию."""
    defaults = dict(ADMIN_CARD)
    defaults.update({
        "link_dc": "", "link_eskhata": "", "link_alif": "",
        "pay_mode": "manual", "auto_approve": "0",
    })
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings WHERE key LIKE 'pay_%'").fetchall()
            for r in rows:
                k = r["key"].replace("pay_", "", 1)
                defaults[k] = r["value"]
    except Exception:
        pass
    return defaults


def set_payment_settings(data: dict):
    with get_db() as conn:
        for k, v in data.items():
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (f"pay_{k}", str(v).strip(), datetime.now().isoformat())
            )



def get_setting(key, default=""):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row:
                return row["value"]
    except Exception:
        pass
    return default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, str(value), datetime.now().isoformat())
        )


def count_attempts_today(user_id, test_id=None):
    """Сколько попыток экзамена сегодня (mode=exam)."""
    day = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        if test_id:
            row = conn.execute(
                """SELECT COUNT(*) FROM test_results
                   WHERE user_id = ? AND test_id = ?
                   AND (mode IS NULL OR mode = 'exam')
                   AND created_at LIKE ?""",
                (user_id, test_id, day + "%")
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM test_results
                   WHERE user_id = ?
                   AND (mode IS NULL OR mode = 'exam')
                   AND created_at LIKE ?""",
                (user_id, day + "%")
            ).fetchone()
        return int(row[0] if row else 0)


def max_exam_attempts_per_day():
    """Лимит попыток экзамена: строго 1 раз на тест."""
    return 1


# Оценки: балл всегда из 100 (50 вопросов × 2 балла)
# A 90–100, B 70–89, C 60–69, D 50–59, F 45–49, Fx 0–44
GRADE_BANDS = [
    # Только A B C D, балл 0–100
    ("A", 75, 100, 90),
    ("B", 50, 74, 62),
    ("C", 25, 49, 37),
    ("D", 0, 24, 12),
]
GRADE_LETTERS = [g[0] for g in GRADE_BANDS]

# Часовые пояса стран СНГ (обязательно при регистрации)
CIS_TIMEZONES = [
    ("Asia/Dushanbe", "Таджикистан (Dushanbe, UTC+5)"),
    ("Asia/Tashkent", "Узбекистан (Tashkent, UTC+5)"),
    ("Asia/Bishkek", "Кыргызстан (Bishkek, UTC+6)"),
    ("Asia/Almaty", "Казахстан (Almaty, UTC+5/6)"),
    ("Asia/Aqtobe", "Казахстан (Aqtobe, UTC+5)"),
    ("Asia/Ashgabat", "Туркменистан (Ashgabat, UTC+5)"),
    ("Europe/Moscow", "Россия (Москва, UTC+3)"),
    ("Asia/Yekaterinburg", "Россия (Екатеринбург, UTC+5)"),
    ("Asia/Novosibirsk", "Россия (Новосибирск, UTC+7)"),
    ("Asia/Vladivostok", "Россия (Владивосток, UTC+10)"),
    ("Europe/Minsk", "Беларусь (Минск, UTC+3)"),
    ("Europe/Kyiv", "Украина (Киев, UTC+2/3)"),
    ("Asia/Yerevan", "Армения (Ереван, UTC+4)"),
    ("Asia/Baku", "Азербайджан (Баку, UTC+4)"),
    ("Asia/Tbilisi", "Грузия (Тбилиси, UTC+4)"),
    ("Europe/Chisinau", "Молдова (Кишинёв, UTC+2/3)"),
]


def letter_grade(score_or_percent):
    """Оценка по баллу/проценту 0–100. Возвращает A/B/C/D."""
    s = float(score_or_percent or 0)
    if s < 0:
        s = 0
    if s > 100:
        s = 100
    for letter, lo, hi, _rep in GRADE_BANDS:
        if lo <= s <= hi:
            return letter
    return "D"


def grade_to_score(letter):
    """Представительный балл (из 100) для буквенной оценки."""
    letter = (letter or "D").strip()
    for L, lo, hi, rep in GRADE_BANDS:
        if L == letter:
            return float(rep)
    return 40.0  # D


def score_percent(score, max_score):
    try:
        ms = float(max_score or 0)
        if ms <= 0:
            return 0.0
        p = float(score or 0) / ms * 100.0
        return max(0.0, min(100.0, round(p, 1)))
    except Exception:
        return 0.0


def format_dt(value, tz_name="Asia/Dushanbe"):
    """Форматирование даты/времени в выбранном поясе (по умолчанию Таджикистан)."""
    if value is None:
        return ""
    try:
        from zoneinfo import ZoneInfo
        if isinstance(value, str):
            raw = value.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(raw)
            except Exception:
                return value[:16] if len(value) >= 16 else value
        else:
            dt = value
        if getattr(dt, "tzinfo", None) is None:
            # считаем наивные метки временем Таджикистана
            try:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Dushanbe"))
            except Exception:
                pass
        try:
            dt = dt.astimezone(ZoneInfo(tz_name or "Asia/Dushanbe"))
        except Exception:
            pass
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        s = str(value)
        return s[:16] if len(s) >= 16 else s


def now_tj():
    """Текущее время Asia/Dushanbe (UTC+5)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Dushanbe"))
    except Exception:
        return datetime.utcnow() + timedelta(hours=5)


EXAM_QUESTION_COUNT = 50  # вопросов в одном экзамене


# Web Push (VAPID). На Render задайте VAPID_PRIVATE_KEY и VAPID_PUBLIC_KEY
# или ключи сгенерируются в /tmp при первом запуске.
import base64 as _b64
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@dnk.tj")

def _ensure_vapid():
    global VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY
    if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
        return
    key_file = "/tmp/hgu_vapid_keys.json"
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            VAPID_PUBLIC_KEY = data.get("public", "")
            VAPID_PRIVATE_KEY = data.get("private", "")
            if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
                return
        except Exception:
            pass
    try:
        from py_vapid import Vapid01
        v = Vapid01()
        v.generate_keys()
        # private as PEM, public as urlsafe
        priv = v.private_pem().decode("utf-8") if hasattr(v.private_pem(), "decode") else str(v.private_pem())
        pub = v.public_key.urlsafe_private if False else None
    except Exception:
        pass
    # Fallback: use cryptography to make simple keys via pywebpush util if available
    try:
        from pywebpush import webpush  # noqa
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        private_key = ec.generate_private_key(ec.SECP256R1())
        priv_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        pub_numbers = private_key.public_key().public_numbers()
        x = pub_numbers.x.to_bytes(32, "big")
        y = pub_numbers.y.to_bytes(32, "big")
        uncompressed = b"\x04" + x + y
        VAPID_PRIVATE_KEY = priv_bytes.decode("utf-8")
        VAPID_PUBLIC_KEY = _b64.urlsafe_b64encode(uncompressed).decode("utf-8").rstrip("=")
        try:
            with open(key_file, "w", encoding="utf-8") as f:
                json.dump({"public": VAPID_PUBLIC_KEY, "private": VAPID_PRIVATE_KEY}, f)
        except Exception:
            pass
    except Exception as e:
        print("VAPID generate failed:", e)


def send_push_to_user(user_id, title, body, url="/"):
    """Отправить web-push пользователю (все его устройства)."""
    _ensure_vapid()
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        app.logger.warning("pywebpush not installed")
        return 0
    sent = 0
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        dead = []
        for row in rows:
            sub = {
                "endpoint": row["endpoint"],
                "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            }
            payload = json.dumps({
                "title": title,
                "body": body,
                "url": url,
            }, ensure_ascii=False)
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                )
                sent += 1
            except Exception as ex:
                app.logger.info("push fail: %s", ex)
                dead.append(row["id"])
        for did in dead:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (did,))
    return sent



PRO_PRICE = 10
PRO_DURATION_DAYS = 60
FREE_PRO_DAYS = 2

# ==================== БАЗА ДАННЫХ ====================
# На Railway/Render бесплатный диск стирается при перезапуске.
# Чтобы аккаунты сохранялись — задайте DATABASE_URL (PostgreSQL).
# Railway: Add → Database → PostgreSQL (DATABASE_URL подставится сам).
# Альтернатива: https://supabase.com или https://neon.tech

import sqlite3
from contextlib import contextmanager

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if (
    os.environ.get("RENDER")
    or os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("DATABASE_DIR")
):
    _data_dir = os.environ.get("DATABASE_DIR") or os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_data_dir, exist_ok=True)
    DB_PATH = os.path.join(_data_dir, "hgu_test.db")
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hgu_test.db")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)



class _Row:
    """Единый доступ row['col'] / row[0] для SQLite и PostgreSQL."""
    __slots__ = ("_d", "_keys")

    def __init__(self, data):
        if data is None:
            self._d = {}
            self._keys = []
        elif isinstance(data, dict):
            # нижний регистр ключей — PostgreSQL
            self._d = {str(k).lower(): v for k, v in data.items()}
            self._keys = list(self._d.keys())
        else:
            self._d = {}
            self._keys = []
            try:
                for k in data.keys():
                    self._d[str(k).lower()] = data[k]
                    self._keys.append(str(k).lower())
            except Exception:
                pass

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._d[self._keys[key]]
        k = str(key).lower()
        if k in self._d:
            return self._d[k]
        raise KeyError(key)

    def __contains__(self, key):
        return str(key).lower() in self._d

    def get(self, key, default=None):
        try:
            return self[key]
        except Exception:
            return default

    def keys(self):
        return self._d.keys()


class _DBCursor:
    def __init__(self, cursor, kind):
        self._cursor = cursor
        self._kind = kind
        self.lastrowid = None

    def _wrap(self, row):
        if row is None:
            return None
        if self._kind == "pg":
            if isinstance(row, dict):
                return _Row(row)
            desc = self._cursor.description or []
            keys = [d[0] for d in desc]
            return _Row(dict(zip(keys, row)))
        if isinstance(row, dict):
            return _Row(row)
        return row

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall() or []
        return [self._wrap(r) for r in rows]

    def __iter__(self):
        for r in self.fetchall():
            yield r


class _DBConn:
    def __init__(self, raw, kind):
        self._raw = raw
        self._kind = kind
        self.lastrowid = None

    def _adapt_sql(self, sql):
        if self._kind != "pg":
            return sql
        s = sql
        s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        # только плейсхолдеры, не трогаем ? в тексте
        s = s.replace("?", "%s")
        if "INSERT OR REPLACE INTO push_subscriptions" in s:
            s = """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id, endpoint) DO UPDATE
                   SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth"""
        s = s.replace("INSERT OR REPLACE INTO", "INSERT INTO")
        s = s.replace("SELECT last_insert_rowid()", "SELECT lastval()")
        # SQLite ON CONFLICT для presence
        if "ON CONFLICT(user_id) DO UPDATE" in s or "ON CONFLICT (user_id) DO UPDATE" in s:
            pass  # PG supports this
        return s

    def execute(self, sql, params=None):
        params = tuple(params) if params is not None else ()
        sql2 = self._adapt_sql(sql)
        if self._kind == "pg":
            cur = self._raw.cursor()
            cur.execute(sql2, params)
            self.lastrowid = None
            return _DBCursor(cur, "pg")
        cur = self._raw.execute(sql2, params)
        self.lastrowid = getattr(cur, "lastrowid", None)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


def _open_sqlite():
    raw = sqlite3.connect(DB_PATH, timeout=30)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return raw


def _is_cloud_host():
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RENDER")
    )


def _connect_postgres(url):
    """Подключение к PostgreSQL через pg8000. Возвращает raw connection."""
    import pg8000.dbapi as pgdb
    from urllib.parse import urlparse, unquote, parse_qs
    import ssl as _ssl

    u = urlparse(url)
    user = unquote(u.username or "postgres")
    password = unquote(u.password or "")
    host = u.hostname or "localhost"
    port = int(u.port or 5432)
    database = (u.path or "/postgres").lstrip("/") or "postgres"
    qs = parse_qs(u.query or "")
    sslmode = (qs.get("sslmode") or [""])[0].lower()

    internal = (
        "railway.internal" in (host or "").lower()
        or "localhost" in (host or "").lower()
        or host in ("127.0.0.1", "::1")
    )
    need_ssl = (not internal) and (
        sslmode in ("require", "verify-ca", "verify-full")
        or "sslmode=require" in url
        or "rlwy.net" in (host or "")
        or "railway.app" in (host or "")
        or "render.com" in url
        or "supabase" in url
        or "neon.tech" in url
        or _is_cloud_host()
    )

    kwargs = dict(
        user=user,
        password=password,
        host=host,
        port=port,
        database=database,
        timeout=30,
    )
    if need_ssl:
        ctx = _ssl.create_default_context()
        try:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        except Exception:
            pass
        kwargs["ssl_context"] = ctx

    return pgdb.connect(**kwargs)


@contextmanager
def get_db():
    """Подключение к БД. PostgreSQL если есть DATABASE_URL, иначе SQLite."""
    url = (DATABASE_URL or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    if url:
        raw = None
        try:
            raw = _connect_postgres(url)
            conn = _DBConn(raw, "pg")
            try:
                yield conn
                raw.commit()
            except Exception:
                try:
                    raw.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            return
        except Exception as e:
            print("ERROR PostgreSQL connection/query:", e)
            if _is_cloud_host():
                # В облаке без рабочей Postgres — ошибка (не молча SQLite)
                raise RuntimeError("PostgreSQL: " + str(e))
            print("Fallback SQLite (локально)")

    raw = _open_sqlite()
    conn = _DBConn(raw, "sqlite")
    try:
        yield conn
        raw.commit()
    except Exception:
        try:
            raw.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            raw.close()
        except Exception:
            pass



def init_db():
    """Создание таблиц. Каждая команда изолирована — Postgres не «ломает» всё после одной ошибки."""

    def run(sql, params=None):
        try:
            with get_db() as conn:
                conn.execute(sql, params)
            return True
        except Exception as e:
            print("init_db stmt:", str(e)[:200])
            return False

    # --- users (обязательно) ---
    run("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_pro INTEGER DEFAULT 0,
            pro_until TEXT,
            free_pro_used INTEGER DEFAULT 0,
            language TEXT DEFAULT 'ru',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        )
    """) if DATABASE_URL else run("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_pro INTEGER DEFAULT 0,
            pro_until TEXT,
            free_pro_used INTEGER DEFAULT 0,
            language TEXT DEFAULT 'ru',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        )
    """)

    # optional columns on users
    for col_sql in [
        "ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'",
        "ALTER TABLE users ADD COLUMN sound_enabled INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN hints_left INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN device_type TEXT DEFAULT 'unknown'",
        "ALTER TABLE users ADD COLUMN password_plain TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Asia/Dushanbe'",
    ]:
        run(col_sql)

    def mk(name, body_pg, body_sqlite=None):
        if DATABASE_URL:
            run(f"CREATE TABLE IF NOT EXISTS {name} ({body_pg})")
        else:
            run(f"CREATE TABLE IF NOT EXISTS {name} ({body_sqlite or body_pg.replace('SERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')})")

    mk("test_results", """
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        test_id TEXT NOT NULL,
        score REAL NOT NULL,
        max_score REAL NOT NULL,
        correct INTEGER NOT NULL,
        incorrect INTEGER NOT NULL,
        answers_json TEXT,
        suggested_faculties TEXT,
        duration_seconds INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        mode TEXT DEFAULT 'exam'
    """)
    run("ALTER TABLE test_results ADD COLUMN mode TEXT DEFAULT 'exam'")

    run("ALTER TABLE pro_requests ADD COLUMN payment_method TEXT DEFAULT ''")
    mk("pro_requests", """
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        package TEXT DEFAULT '2m',
        duration_days INTEGER DEFAULT 60,
        screenshot_path TEXT,
        status TEXT DEFAULT 'pending',
        admin_note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        processed_at TEXT
    """)
    run("ALTER TABLE pro_requests ADD COLUMN payment_method TEXT DEFAULT ''")

    mk("notifications", """
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    """)

    mk("global_notifications", """
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    """)

    mk("faculties", """
        id SERIAL PRIMARY KEY,
        name_ru TEXT NOT NULL,
        name_en TEXT DEFAULT '',
        name_tg TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    """)

    mk("content_tests", """
        id SERIAL PRIMARY KEY,
        code TEXT UNIQUE NOT NULL,
        title_ru TEXT NOT NULL,
        title_en TEXT DEFAULT '',
        title_tg TEXT DEFAULT '',
        time_limit INTEGER DEFAULT 600,
        pro_only INTEGER DEFAULT 0,
        faculty_ids TEXT DEFAULT '[]',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        test_type TEXT DEFAULT 'mcq',
        exam_start TEXT DEFAULT '',
        exam_end TEXT DEFAULT '',
        published INTEGER DEFAULT 0,
        subject_name TEXT DEFAULT ''
    """)
    for col_sql in [
        "ALTER TABLE content_tests ADD COLUMN test_type TEXT DEFAULT 'mcq'",
        "ALTER TABLE content_tests ADD COLUMN exam_start TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN exam_end TEXT DEFAULT ''",
        "ALTER TABLE content_tests ADD COLUMN published INTEGER DEFAULT 0",
        "ALTER TABLE content_tests ADD COLUMN subject_name TEXT DEFAULT ''",
    ]:
        run(col_sql)

    mk("content_questions", """
        id SERIAL PRIMARY KEY,
        test_id INTEGER NOT NULL,
        q_ru TEXT NOT NULL,
        q_en TEXT DEFAULT '',
        q_tg TEXT DEFAULT '',
        opt_a TEXT NOT NULL,
        opt_b TEXT NOT NULL,
        opt_c TEXT DEFAULT '',
        opt_d TEXT DEFAULT '',
        correct_index INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER DEFAULT 0,
        q_type TEXT DEFAULT 'mcq',
        correct_multi TEXT DEFAULT '',
        match_json TEXT DEFAULT ''
    """)
    for col_sql in [
        "ALTER TABLE content_questions ADD COLUMN q_type TEXT DEFAULT 'mcq'",
        "ALTER TABLE content_questions ADD COLUMN correct_multi TEXT DEFAULT ''",
        "ALTER TABLE content_questions ADD COLUMN match_json TEXT DEFAULT ''",
    ]:
        run(col_sql)

    mk("social_links", """
        id SERIAL PRIMARY KEY,
        network TEXT NOT NULL,
        title TEXT DEFAULT '',
        url TEXT NOT NULL,
        is_promo INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        ends_at TEXT DEFAULT ''
    """)
    run("ALTER TABLE social_links ADD COLUMN ends_at TEXT DEFAULT ''")

    mk("app_settings", """
        key TEXT PRIMARY KEY,
        value TEXT
    """)

    mk("push_subscriptions", """
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        endpoint TEXT NOT NULL,
        p256dh TEXT,
        auth TEXT
    """)

    mk("chat_messages", """
        id SERIAL PRIMARY KEY,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        message TEXT DEFAULT '',
        is_read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        msg_type TEXT DEFAULT 'text',
        media_path TEXT DEFAULT ''
    """)
    run("ALTER TABLE chat_messages ADD COLUMN msg_type TEXT DEFAULT 'text'")
    run("ALTER TABLE chat_messages ADD COLUMN media_path TEXT DEFAULT ''")

    mk("user_presence", """
        user_id INTEGER PRIMARY KEY,
        last_seen TEXT NOT NULL
    """)

    # Админ admin@dnk.tj / rm2006$
    ADMIN_EMAIL = "admin@dnk.tj"
    ADMIN_PASS = "rm2006$"
    admin_hash = generate_password_hash(ADMIN_PASS, method="pbkdf2:sha256")
    try:
        with get_db() as conn:
            admin = conn.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
            if not admin:
                conn.execute(
                    "INSERT INTO users (full_name, email, password_hash, language) VALUES (?, ?, ?, ?)",
                    ("Администратор", ADMIN_EMAIL, admin_hash, "ru"),
                )
            else:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE email = ?",
                    (admin_hash, ADMIN_EMAIL),
                )
        with get_db() as conn:
            try:
                conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (ADMIN_EMAIL,))
            except Exception:
                pass
            try:
                conn.execute("UPDATE users SET password_plain = ? WHERE email = ?", (ADMIN_PASS, ADMIN_EMAIL))
            except Exception:
                pass
    except Exception as ae:
        print("admin ensure failed:", ae)


def ensure_users_table():
    """Если таблица users пропала — создать на лету."""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1 FROM users LIMIT 1")
        return True
    except Exception:
        print("ensure_users_table: recreating schema...")
        try:
            init_db()
            return True
        except Exception as e:
            print("ensure_users_table failed:", e)
            return False




# ==================== МОДЕЛЬ ПОЛЬЗОВАТЕЛЯ ====================


@app.context_processor
def inject_grade_helpers():
    return {
        "letter_grade": letter_grade,
        "score_percent": score_percent,
        "format_dt": format_dt,
        "GRADE_LETTERS": GRADE_LETTERS,
        "CIS_TIMEZONES": CIS_TIMEZONES,
    }

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Войдите в аккаунт"

_schema_ready = False

@app.before_request
def _ensure_schema_once():
    global _schema_ready
    if _schema_ready:
        return
    # только для login/register/health — не на каждый static
    try:
        if request.endpoint in (None, "static"):
            return
    except Exception:
        pass
    try:
        ensure_users_table()
        _schema_ready = True
    except Exception as e:
        print("before_request schema:", e)




class User(UserMixin):
    def __init__(self, row):
        def g(key, default=None):
            try:
                val = row[key]
                return default if val is None else val
            except (KeyError, IndexError, TypeError):
                return default

        self.id = g("id")
        self.full_name = g("full_name", "") or ""
        self.email = g("email", "") or ""
        self.password_hash = g("password_hash", "") or ""
        self.is_admin = bool(g("is_admin", 0))
        self.is_pro = bool(g("is_pro", 0))
        self.pro_until = g("pro_until")
        self.free_pro_used = bool(g("free_pro_used", 0))
        self.language = g("language", "ru") or "ru"
        self.created_at = g("created_at")
        self.last_login = g("last_login")
        self.theme = g("theme", "light") or "light"
        se = g("sound_enabled", 1)
        self.sound_enabled = True if se is None else bool(se)
        self.hints_left = int(g("hints_left", 0) or 0)
        self.device_type = g("device_type", "unknown") or "unknown"
        self.password_plain = g("password_plain", "") or ""
        self.timezone = g("timezone", "Asia/Dushanbe") or "Asia/Dushanbe"

    def check_pro(self):
        """Проверяет и обновляет статус Pro"""
        if not self.pro_until:
            return False
        try:
            until = datetime.fromisoformat(self.pro_until)
            if datetime.now() > until:
                with get_db() as conn:
                    conn.execute("UPDATE users SET is_pro = 0, pro_until = NULL WHERE id = ?", (self.id,))
                self.is_pro = False
                self.pro_until = None
                return False
            return True
        except Exception:
            return False


@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            return User(row)
    return None


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Доступ только для администратора", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


# ==================== ТЕСТЫ И ФАКУЛЬТЕТЫ ====================

FACULTIES = {
    "math": {
        "ru": "Математический факультет",
        "en": "Faculty of Mathematics",
        "tg": "Факултети математика"
    },
    "physics": {
        "ru": "Физико-технический факультет",
        "en": "Faculty of Physics and Technology",
        "tg": "Факултети физикаву техника"
    },
    "it": {
        "ru": "Факультет телекоммуникаций и ИТ",
        "en": "Faculty of Telecommunications and IT",
        "tg": "Факултети телекоммуникатсия ва ТИ"
    },
    "finance": {
        "ru": "Факультет финансов и рыночной экономики",
        "en": "Faculty of Finance and Market Economy",
        "tg": "Факултети молия ва иқтисоди бозор"
    },
    "foreign_lang": {
        "ru": "Факультет иностранных языков",
        "en": "Faculty of Foreign Languages",
        "tg": "Факултети забонҳои хориҷӣ"
    },
    "oriental": {
        "ru": "Факультет восточных языков",
        "en": "Faculty of Oriental Languages",
        "tg": "Факултети забонҳои шарқӣ"
    },
    "history_law": {
        "ru": "Факультет истории и права",
        "en": "Faculty of History and Law",
        "tg": "Факултети таърих ва ҳуқуқ"
    },
    "tajik_phil": {
        "ru": "Факультет таджикской филологии",
        "en": "Faculty of Tajik Philology",
        "tg": "Факултети филологияи тоҷикӣ"
    },
    "russian_phil": {
        "ru": "Факультет русской филологии",
        "en": "Faculty of Russian Philology",
        "tg": "Факултети филологияи русӣ"
    },
    "pedagogy": {
        "ru": "Педагогический факультет",
        "en": "Faculty of Pedagogy",
        "tg": "Факултети омӯзгорӣ"
    },
    "geo_eco": {
        "ru": "Факультет геоэкологии",
        "en": "Faculty of Geo-Ecology",
        "tg": "Факултети геоэкология"
    },
    "arts": {
        "ru": "Факультет искусств",
        "en": "Faculty of Arts",
        "tg": "Факултети санъат"
    },
    "chem_bio": {
        "ru": "Факультет химии и биологии",
        "en": "Faculty of Chemistry and Biology",
        "tg": "Факултети химия ва биология"
    },
    "physical": {
        "ru": "Факультет физической культуры",
        "en": "Faculty of Physical Education",
        "tg": "Факултети тарбияи ҷисмонӣ"
    },
    "uzbek_phil": {
        "ru": "Факультет узбекской филологии",
        "en": "Faculty of Uzbek Philology",
        "tg": "Факултети филологияи ӯзбекӣ"
    }
}

# Вопросы тестов (базовый набор + расширенный для Pro)
# Каждый тест связан с одним или несколькими факультетами

TESTS = {
    "math_basic": {
        "title": {"ru": "Математика (базовый)", "en": "Mathematics (Basic)", "tg": "Математика (асосӣ)"},
        "faculties": ["math", "it", "physics"],
        "time_limit": 600,  # секунд
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Чему равно 2 + 2 * 2?", "en": "What is 2 + 2 * 2?", "tg": "2 + 2 * 2 баробар ба чист?"},
                "options": {"ru": ["6", "8", "4", "10"], "en": ["6", "8", "4", "10"], "tg": ["6", "8", "4", "10"]},
                "correct": 0
            },
            {
                "q": {"ru": "Корень из 144 равен?", "en": "Square root of 144 is?", "tg": "Решаи 144 баробар ба?"},
                "options": {"ru": ["10", "12", "14", "16"], "en": ["10", "12", "14", "16"], "tg": ["10", "12", "14", "16"]},
                "correct": 1
            },
            {
                "q": {"ru": "Решите: 5x = 20. x = ?", "en": "Solve: 5x = 20. x = ?", "tg": "Ҳал кунед: 5x = 20. x = ?"},
                "options": {"ru": ["2", "4", "5", "10"], "en": ["2", "4", "5", "10"], "tg": ["2", "4", "5", "10"]},
                "correct": 1
            },
            {
                "q": {"ru": "Площадь квадрата со стороной 5?", "en": "Area of square with side 5?", "tg": "Масоҳати квадрат бо тарафи 5?"},
                "options": {"ru": ["10", "20", "25", "30"], "en": ["10", "20", "25", "30"], "tg": ["10", "20", "25", "30"]},
                "correct": 2
            },
            {
                "q": {"ru": "Сколько градусов в прямом угле?", "en": "How many degrees in a right angle?", "tg": "Дар кунҷи рост чанд дараҷа?"},
                "options": {"ru": ["45", "90", "180", "360"], "en": ["45", "90", "180", "360"], "tg": ["45", "90", "180", "360"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что больше: 3/4 или 0.7?", "en": "Which is larger: 3/4 or 0.7?", "tg": "Кадом калонтар: 3/4 ё 0.7?"},
                "options": {"ru": ["3/4", "0.7", "равны", "нельзя сравнить"], "en": ["3/4", "0.7", "equal", "cannot compare"], "tg": ["3/4", "0.7", "баробар", "муқоиса кардан мумкин нест"]},
                "correct": 0
            },
            {
                "q": {"ru": "Сумма углов треугольника?", "en": "Sum of angles in a triangle?", "tg": "Ҷамъи кунҷҳои секунҷа?"},
                "options": {"ru": ["90", "180", "270", "360"], "en": ["90", "180", "270", "360"], "tg": ["90", "180", "270", "360"]},
                "correct": 1
            },
            {
                "q": {"ru": "10% от 200?", "en": "10% of 200?", "tg": "10% аз 200?"},
                "options": {"ru": ["10", "20", "30", "40"], "en": ["10", "20", "30", "40"], "tg": ["10", "20", "30", "40"]},
                "correct": 1
            },
            {
                "q": {"ru": "Если a=3, b=4, то a² + b² = ?", "en": "If a=3, b=4, then a² + b² = ?", "tg": "Агар a=3, b=4, он гоҳ a² + b² = ?"},
                "options": {"ru": ["7", "12", "25", "49"], "en": ["7", "12", "25", "49"], "tg": ["7", "12", "25", "49"]},
                "correct": 2
            },
            {
                "q": {"ru": "Сколько минут в 2.5 часах?", "en": "How many minutes in 2.5 hours?", "tg": "Дар 2.5 соат чанд дақиқа?"},
                "options": {"ru": ["120", "150", "180", "200"], "en": ["120", "150", "180", "200"], "tg": ["120", "150", "180", "200"]},
                "correct": 1
            }
        ]
    },
    "math_pro": {
        "title": {"ru": "Математика (продвинутый)", "en": "Mathematics (Advanced)", "tg": "Математика (пешрафта)"},
        "faculties": ["math", "it", "physics"],
        "time_limit": 900,
        "pro_only": True,
        "questions": [
            {
                "q": {"ru": "Решите уравнение: x² - 5x + 6 = 0", "en": "Solve: x² - 5x + 6 = 0", "tg": "Муодиларо ҳал кунед: x² - 5x + 6 = 0"},
                "options": {"ru": ["x=2 и x=3", "x=1 и x=6", "x=-2 и x=-3", "нет решений"], "en": ["x=2 and x=3", "x=1 and x=6", "x=-2 and x=-3", "no solutions"], "tg": ["x=2 ва x=3", "x=1 ва x=6", "x=-2 ва x=-3", "ҳал надорад"]},
                "correct": 0
            },
            {
                "q": {"ru": "Производная функции f(x) = x³?", "en": "Derivative of f(x) = x³?", "tg": "Ҳосилаи функсияи f(x) = x³?"},
                "options": {"ru": ["3x²", "x²", "3x", "x³"], "en": ["3x²", "x²", "3x", "x³"], "tg": ["3x²", "x²", "3x", "x³"]},
                "correct": 0
            },
            {
                "q": {"ru": "log₁₀(1000) = ?", "en": "log₁₀(1000) = ?", "tg": "log₁₀(1000) = ?"},
                "options": {"ru": ["2", "3", "4", "10"], "en": ["2", "3", "4", "10"], "tg": ["2", "3", "4", "10"]},
                "correct": 1
            },
            {
                "q": {"ru": "Интеграл от 2x dx?", "en": "Integral of 2x dx?", "tg": "Интеграли 2x dx?"},
                "options": {"ru": ["x² + C", "2x² + C", "x + C", "2x + C"], "en": ["x² + C", "2x² + C", "x + C", "2x + C"], "tg": ["x² + C", "2x² + C", "x + C", "2x + C"]},
                "correct": 0
            },
            {
                "q": {"ru": "sin(90°) = ?", "en": "sin(90°) = ?", "tg": "sin(90°) = ?"},
                "options": {"ru": ["0", "0.5", "1", "-1"], "en": ["0", "0.5", "1", "-1"], "tg": ["0", "0.5", "1", "-1"]},
                "correct": 2
            },
            {
                "q": {"ru": "Предел lim(x→0) sin(x)/x = ?", "en": "Limit lim(x→0) sin(x)/x = ?", "tg": "Ҳадди lim(x→0) sin(x)/x = ?"},
                "options": {"ru": ["0", "1", "∞", "не существует"], "en": ["0", "1", "∞", "does not exist"], "tg": ["0", "1", "∞", "мавҷуд нест"]},
                "correct": 1
            },
            {
                "q": {"ru": "Матрица 2x2. Определитель [[1,2],[3,4]]?", "en": "Determinant of [[1,2],[3,4]]?", "tg": "Детерминанти [[1,2],[3,4]]?"},
                "options": {"ru": ["-2", "2", "-1", "10"], "en": ["-2", "2", "-1", "10"], "tg": ["-2", "2", "-1", "10"]},
                "correct": 0
            },
            {
                "q": {"ru": "Комбинаторика: C(5,2) = ?", "en": "Combinatorics: C(5,2) = ?", "tg": "Комбинаторика: C(5,2) = ?"},
                "options": {"ru": ["5", "10", "15", "20"], "en": ["5", "10", "15", "20"], "tg": ["5", "10", "15", "20"]},
                "correct": 1
            },
            {
                "q": {"ru": "Вероятность выпадения орла при броске монеты?", "en": "Probability of heads when tossing a coin?", "tg": "Эҳтимолияти афтодани сар ҳангоми партофтани танга?"},
                "options": {"ru": ["0", "0.25", "0.5", "1"], "en": ["0", "0.25", "0.5", "1"], "tg": ["0", "0.25", "0.5", "1"]},
                "correct": 2
            },
            {
                "q": {"ru": "Ряд 1 + 2 + 4 + 8 + ... (геометрическая прогрессия). Сумма первых 5 членов?", "en": "Sum of first 5 terms of 1+2+4+8+...?", "tg": "Ҷамъи 5 узви аввали 1+2+4+8+...?"},
                "options": {"ru": ["15", "31", "63", "16"], "en": ["15", "31", "63", "16"], "tg": ["15", "31", "63", "16"]},
                "correct": 1
            }
        ]
    },
    "physics_basic": {
        "title": {"ru": "Физика (базовый)", "en": "Physics (Basic)", "tg": "Физика (асосӣ)"},
        "faculties": ["physics", "it", "chem_bio"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Единица силы в СИ?", "en": "SI unit of force?", "tg": "Воҳиди қувва дар СИ?"},
                "options": {"ru": ["Джоуль", "Ньютон", "Ватт", "Паскаль"], "en": ["Joule", "Newton", "Watt", "Pascal"], "tg": ["Ҷоул", "Нютон", "Ватт", "Паскал"]},
                "correct": 1
            },
            {
                "q": {"ru": "Скорость света примерно?", "en": "Speed of light approximately?", "tg": "Суръати нур тахминан?"},
                "options": {"ru": ["300 км/с", "3000 км/с", "300000 км/с", "30 км/с"], "en": ["300 km/s", "3000 km/s", "300000 km/s", "30 km/s"], "tg": ["300 км/с", "3000 км/с", "300000 км/с", "30 км/с"]},
                "correct": 2
            },
            {
                "q": {"ru": "Формула кинетической энергии?", "en": "Kinetic energy formula?", "tg": "Формулаи энергияи кинетикӣ?"},
                "options": {"ru": ["mgh", "mv²/2", "Fx", "P=W/t"], "en": ["mgh", "mv²/2", "Fx", "P=W/t"], "tg": ["mgh", "mv²/2", "Fx", "P=W/t"]},
                "correct": 1
            },
            {
                "q": {"ru": "Закон Ома: I = ?", "en": "Ohm's law: I = ?", "tg": "Қонуни Ом: I = ?"},
                "options": {"ru": ["U/R", "U*R", "R/U", "U+R"], "en": ["U/R", "U*R", "R/U", "U+R"], "tg": ["U/R", "U*R", "R/U", "U+R"]},
                "correct": 0
            },
            {
                "q": {"ru": "Температура кипения воды при нормальном давлении?", "en": "Boiling point of water at normal pressure?", "tg": "Ҳарорати ҷӯшиши об дар фишори муқаррарӣ?"},
                "options": {"ru": ["0°C", "50°C", "100°C", "200°C"], "en": ["0°C", "50°C", "100°C", "200°C"], "tg": ["0°C", "50°C", "100°C", "200°C"]},
                "correct": 2
            },
            {
                "q": {"ru": "Ускорение свободного падения g ≈ ?", "en": "Free fall acceleration g ≈ ?", "tg": "Шитоби афтиши озод g ≈ ?"},
                "options": {"ru": ["5 м/с²", "9.8 м/с²", "15 м/с²", "20 м/с²"], "en": ["5 m/s²", "9.8 m/s²", "15 m/s²", "20 m/s²"], "tg": ["5 м/с²", "9.8 м/с²", "15 м/с²", "20 м/с²"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что измеряется в Амперах?", "en": "What is measured in Amperes?", "tg": "Чӣ дар Амперҳо чен карда мешавад?"},
                "options": {"ru": ["Напряжение", "Сопротивление", "Сила тока", "Мощность"], "en": ["Voltage", "Resistance", "Current", "Power"], "tg": ["Шиддат", "Муқовимат", "Қувваи ҷараён", "Қувва"]},
                "correct": 2
            },
            {
                "q": {"ru": "Плотность воды?", "en": "Density of water?", "tg": "Зичии об?"},
                "options": {"ru": ["500 кг/м³", "1000 кг/м³", "1500 кг/м³", "2000 кг/м³"], "en": ["500 kg/m³", "1000 kg/m³", "1500 kg/m³", "2000 kg/m³"], "tg": ["500 кг/м³", "1000 кг/м³", "1500 кг/м³", "2000 кг/м³"]},
                "correct": 1
            },
            {
                "q": {"ru": "Первый закон Ньютона говорит о?", "en": "Newton's first law is about?", "tg": "Қонуни аввали Нютон дар бораи?"},
                "options": {"ru": ["Силе", "Инерции", "Действии и противодействии", "Гравитации"], "en": ["Force", "Inertia", "Action-reaction", "Gravity"], "tg": ["Қувва", "Инерсия", "Амал ва зиддиамал", "Граитатсия"]},
                "correct": 1
            },
            {
                "q": {"ru": "Частота тока в сети Таджикистана?", "en": "Mains frequency in Tajikistan?", "tg": "Басомади ҷараён дар шабакаи Тоҷикистон?"},
                "options": {"ru": ["40 Гц", "50 Гц", "60 Гц", "100 Гц"], "en": ["40 Hz", "50 Hz", "60 Hz", "100 Hz"], "tg": ["40 Гц", "50 Гц", "60 Гц", "100 Гц"]},
                "correct": 1
            }
        ]
    },
    "it_basic": {
        "title": {"ru": "Информатика (базовый)", "en": "Informatics (Basic)", "tg": "Информатика (асосӣ)"},
        "faculties": ["it", "math"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Что означает HTML?", "en": "What does HTML stand for?", "tg": "HTML чӣ маъно дорад?"},
                "options": {"ru": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"], "en": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"], "tg": ["Hyper Text Markup Language", "High Tech Modern Language", "Home Tool Markup Language", "Hyperlinks Text Mark Language"]},
                "correct": 0
            },
            {
                "q": {"ru": "1 байт = ?", "en": "1 byte = ?", "tg": "1 байт = ?"},
                "options": {"ru": ["4 бита", "8 бит", "16 бит", "32 бита"], "en": ["4 bits", "8 bits", "16 bits", "32 bits"], "tg": ["4 бит", "8 бит", "16 бит", "32 бит"]},
                "correct": 1
            },
            {
                "q": {"ru": "Какой язык программирования?", "en": "Which is a programming language?", "tg": "Кадом забони барномасозӣ аст?"},
                "options": {"ru": ["HTML", "CSS", "Python", "HTTP"], "en": ["HTML", "CSS", "Python", "HTTP"], "tg": ["HTML", "CSS", "Python", "HTTP"]},
                "correct": 2
            },
            {
                "q": {"ru": "CPU расшифровывается как?", "en": "CPU stands for?", "tg": "CPU чӣ маъно дорад?"},
                "options": {"ru": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"], "en": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"], "tg": ["Central Processing Unit", "Computer Personal Unit", "Central Program Utility", "Control Processing Unit"]},
                "correct": 0
            },
            {
                "q": {"ru": "Операционная система?", "en": "Which is an OS?", "tg": "Кадом системаи амалиётӣ аст?"},
                "options": {"ru": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"], "en": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"], "tg": ["Microsoft Word", "Google Chrome", "Windows", "Adobe Photoshop"]},
                "correct": 2
            },
            {
                "q": {"ru": "Двоичная система: 1010₂ = ?", "en": "Binary: 1010₂ = ?", "tg": "Системаи дуӣ: 1010₂ = ?"},
                "options": {"ru": ["8", "10", "12", "14"], "en": ["8", "10", "12", "14"], "tg": ["8", "10", "12", "14"]},
                "correct": 1
            },
            {
                "q": {"ru": "Что такое алгоритм?", "en": "What is an algorithm?", "tg": "Алгоритм чист?"},
                "options": {"ru": ["Язык программирования", "Последовательность действий", "Операционная система", "Тип данных"], "en": ["Programming language", "Sequence of actions", "Operating system", "Data type"], "tg": ["Забони барномасозӣ", "Пайдарпаии амалҳо", "Системаи амалиётӣ", "Намуди маълумот"]},
                "correct": 1
            },
            {
                "q": {"ru": "RAM - это?", "en": "RAM is?", "tg": "RAM чист?"},
                "options": {"ru": ["Постоянная память", "Оперативная память", "Жёсткий диск", "Процессор"], "en": ["Permanent memory", "Random Access Memory", "Hard drive", "Processor"], "tg": ["Хотираи доимӣ", "Хотираи амалиётӣ", "Диски сахт", "Процессор"]},
                "correct": 1
            },
            {
                "q": {"ru": "Интернет-протокол для веб-страниц?", "en": "Internet protocol for web pages?", "tg": "Протоколи интернет барои саҳифаҳои веб?"},
                "options": {"ru": ["FTP", "HTTP", "SMTP", "SSH"], "en": ["FTP", "HTTP", "SMTP", "SSH"], "tg": ["FTP", "HTTP", "SMTP", "SSH"]},
                "correct": 1
            },
            {
                "q": {"ru": "В Python: print(type(5)) выведет?", "en": "In Python: print(type(5)) outputs?", "tg": "Дар Python: print(type(5)) чӣ мебарорад?"},
                "options": {"ru": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"], "en": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"], "tg": ["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"]},
                "correct": 1
            }
        ]
    },
    "history_basic": {
        "title": {"ru": "История Таджикистана (базовый)", "en": "History of Tajikistan (Basic)", "tg": "Таърихи Тоҷикистон (асосӣ)"},
        "faculties": ["history_law", "tajik_phil", "pedagogy"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Столица Таджикистана?", "en": "Capital of Tajikistan?", "tg": "Пойтахти Тоҷикистон?"},
                "options": {"ru": ["Худжанд", "Душанбе", "Куляб", "Курган-Тюбе"], "en": ["Khujand", "Dushanbe", "Kulob", "Qurghonteppa"], "tg": ["Хуҷанд", "Душанбе", "Кӯлоб", "Қурғонтеппа"]},
                "correct": 1
            },
            {
                "q": {"ru": "Год независимости Таджикистана?", "en": "Year of Tajikistan independence?", "tg": "Соли истиқлолияти Тоҷикистон?"},
                "options": {"ru": ["1989", "1991", "1992", "1994"], "en": ["1989", "1991", "1992", "1994"], "tg": ["1989", "1991", "1992", "1994"]},
                "correct": 1
            },
            {
                "q": {"ru": "Великий таджикский поэт?", "en": "Great Tajik poet?", "tg": "Шоири бузурги тоҷик?"},
                "options": {"ru": ["Пушкин", "Рудаки", "Шекспир", "Гёте"], "en": ["Pushkin", "Rudaki", "Shakespeare", "Goethe"], "tg": ["Пушкин", "Рӯдакӣ", "Шекспир", "Гёте"]},
                "correct": 1
            },
            {
                "q": {"ru": "Древнее государство на территории Таджикистана?", "en": "Ancient state on territory of Tajikistan?", "tg": "Давлати қадим дар ҳудуди Тоҷикистон?"},
                "options": {"ru": ["Согдиана", "Рим", "Египет", "Китай"], "en": ["Sogdiana", "Rome", "Egypt", "China"], "tg": ["Суғд", "Рим", "Миср", "Чин"]},
                "correct": 0
            },
            {
                "q": {"ru": "Худжанд ранее назывался?", "en": "Khujand was previously called?", "tg": "Хуҷанд қаблан чӣ ном дошт?"},
                "options": {"ru": ["Ленинабад", "Сталинабад", "Фрунзе", "Алма-Ата"], "en": ["Leninabad", "Stalinabad", "Frunze", "Alma-Ata"], "tg": ["Ленинобод", "Сталинобод", "Фрунзе", "Алма-Ата"]},
                "correct": 0
            },
            {
                "q": {"ru": "Официальный язык Таджикистана?", "en": "Official language of Tajikistan?", "tg": "Забони расмии Тоҷикистон?"},
                "options": {"ru": ["Русский", "Узбекский", "Таджикский", "Персидский"], "en": ["Russian", "Uzbek", "Tajik", "Persian"], "tg": ["Русӣ", "Ӯзбекӣ", "Тоҷикӣ", "Форсӣ"]},
                "correct": 2
            },
            {
                "q": {"ru": "Самая высокая гора Таджикистана?", "en": "Highest mountain in Tajikistan?", "tg": "Баландтарин кӯҳи Тоҷикистон?"},
                "options": {"ru": ["Эльбрус", "Пик Исмоила Сомони", "Арарат", "Казбек"], "en": ["Elbrus", "Ismoil Somoni Peak", "Ararat", "Kazbek"], "tg": ["Элбрус", "Қуллаи Исмоили Сомонӣ", "Арарат", "Қазбек"]},
                "correct": 1
            },
            {
                "q": {"ru": "В каком веке жил Авиценна (Ибн Сина)?", "en": "In which century did Avicenna live?", "tg": "Ибни Сино дар кадом аср зистааст?"},
                "options": {"ru": ["VIII-IX", "X-XI", "XII-XIII", "XIV-XV"], "en": ["8th-9th", "10th-11th", "12th-13th", "14th-15th"], "tg": ["VIII-IX", "X-XI", "XII-XIII", "XIV-XV"]},
                "correct": 1
            },
            {
                "q": {"ru": "Река, протекающая через Худжанд?", "en": "River flowing through Khujand?", "tg": "Дарёе, ки аз Хуҷанд мегузарад?"},
                "options": {"ru": ["Амударья", "Сырдарья", "Вахш", "Пяндж"], "en": ["Amu Darya", "Syr Darya", "Vakhsh", "Panj"], "tg": ["Амударё", "Сирдарё", "Вахш", "Панҷ"]},
                "correct": 1
            },
            {
                "q": {"ru": "Столица Таджикистана?", "en": "Capital of Tajikistan?", "tg": "Пойтахти Тоҷикистон?"},
                "options": {"ru": ["1920", "1932", "1945", "1991"], "en": ["1920", "1932", "1945", "1991"], "tg": ["1920", "1932", "1945", "1991"]},
                "correct": 1
            }
        ]
    },
    "english_basic": {
        "title": {"ru": "Английский язык (базовый)", "en": "English (Basic)", "tg": "Забони англисӣ (асосӣ)"},
        "faculties": ["foreign_lang", "oriental"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Переведите: Hello", "en": "Translate: Hello", "tg": "Тарҷума кунед: Hello"},
                "options": {"ru": ["Привет", "Пока", "Спасибо", "Пожалуйста"], "en": ["Hello", "Bye", "Thanks", "Please"], "tg": ["Салом", "Хайр", "Раҳмат", "Лутфан"]},
                "correct": 0
            },
            {
                "q": {"ru": "How are you? означает?", "en": "How are you? means?", "tg": "How are you? чӣ маъно дорад?"},
                "options": {"ru": ["Как тебя зовут?", "Как дела?", "Где ты?", "Сколько тебе лет?"], "en": ["What is your name?", "How are you?", "Where are you?", "How old are you?"], "tg": ["Номи ту чист?", "Ҳолат чӣ гуна?", "Ту куҷо ҳастӣ?", "Чандсола ҳастӣ?"]},
                "correct": 1
            },
            {
                "q": {"ru": "Прошедшее время от go?", "en": "Past tense of go?", "tg": "Замони гузаштаи go?"},
                "options": {"ru": ["goed", "went", "goes", "going"], "en": ["goed", "went", "goes", "going"], "tg": ["goed", "went", "goes", "going"]},
                "correct": 1
            },
            {
                "q": {"ru": "I ___ a student.", "en": "I ___ a student.", "tg": "I ___ a student."},
                "options": {"ru": ["am", "is", "are", "be"], "en": ["am", "is", "are", "be"], "tg": ["am", "is", "are", "be"]},
                "correct": 0
            },
            {
                "q": {"ru": "Множественное число от child?", "en": "Plural of child?", "tg": "Ҷамъи child?"},
                "options": {"ru": ["childs", "children", "childes", "child"], "en": ["childs", "children", "childes", "child"], "tg": ["childs", "children", "childes", "child"]},
                "correct": 1
            },
            {
                "q": {"ru": "There ___ a book on the table.", "en": "There ___ a book on the table.", "tg": "There ___ a book on the table."},
                "options": {"ru": ["is", "are", "am", "be"], "en": ["is", "are", "am", "be"], "tg": ["is", "are", "am", "be"]},
                "correct": 0
            },
            {
                "q": {"ru": "She ___ to school every day.", "en": "She ___ to school every day.", "tg": "She ___ to school every day."},
                "options": {"ru": ["go", "goes", "going", "went"], "en": ["go", "goes", "going", "went"], "tg": ["go", "goes", "going", "went"]},
                "correct": 1
            },
            {
                "q": {"ru": "What is the opposite of hot?", "en": "What is the opposite of hot?", "tg": "Зидди hot чист?"},
                "options": {"ru": ["warm", "cold", "cool", "heat"], "en": ["warm", "cold", "cool", "heat"], "tg": ["warm", "cold", "cool", "heat"]},
                "correct": 1
            },
            {
                "q": {"ru": "Choose the correct article: ___ apple", "en": "Choose the correct article: ___ apple", "tg": "Артикли дурустро интихоб кунед: ___ apple"},
                "options": {"ru": ["a", "an", "the", "no article"], "en": ["a", "an", "the", "no article"], "tg": ["a", "an", "the", "бе артикл"]},
                "correct": 1
            },
            {
                "q": {"ru": "I have ___ books.", "en": "I have ___ books.", "tg": "I have ___ books."},
                "options": {"ru": ["much", "many", "a little", "little"], "en": ["much", "many", "a little", "little"], "tg": ["much", "many", "a little", "little"]},
                "correct": 1
            }
        ]
    },
    "chemistry_basic": {
        "title": {"ru": "Химия (базовый)", "en": "Chemistry (Basic)", "tg": "Химия (асосӣ)"},
        "faculties": ["chem_bio", "physics"],
        "time_limit": 600,
        "pro_only": False,
        "questions": [
            {
                "q": {"ru": "Химический символ воды?", "en": "Chemical formula of water?", "tg": "Формулаи химиявии об?"},
                "options": {"ru": ["H2O", "CO2", "O2", "NaCl"], "en": ["H2O", "CO2", "O2", "NaCl"], "tg": ["H2O", "CO2", "O2", "NaCl"]},
                "correct": 0
            },
            {
                "q": {"ru": "Атомный номер водорода?", "en": "Atomic number of hydrogen?", "tg": "Рақами атомии гидроген?"},
                "options": {"ru": ["1", "2", "8", "16"], "en": ["1", "2", "8", "16"], "tg": ["1", "2", "8", "16"]},
                "correct": 0
            },
            {
                "q": {"ru": "pH нейтральной среды?", "en": "pH of neutral medium?", "tg": "pH-и муҳити бетараф?"},
                "options": {"ru": ["0", "7", "14", "1"], "en": ["0", "7", "14", "1"], "tg": ["0", "7", "14", "1"]},
                "correct": 1
            },
            {
                "q": {"ru": "Газ, необходимый для дыхания?", "en": "Gas needed for breathing?", "tg": "Газе, ки барои нафаскашӣ лозим аст?"},
                "options": {"ru": ["CO2", "N2", "O2", "H2"], "en": ["CO2", "N2", "O2", "H2"], "tg": ["CO2", "N2", "O2", "H2"]},
                "correct": 2
            },
            {
                "q": {"ru": "Таблица Менделеева содержит элементы по?", "en": "Periodic table arranges elements by?", "tg": "Ҷадвали Менделеев элементҳоро аз рӯи?"},
                "options": {"ru": ["Массе", "Атомному номеру", "Цвету", "Плотности"], "en": ["Mass", "Atomic number", "Color", "Density"], "tg": ["Масса", "Рақами атомӣ", "Ранг", "Зичӣ"]},
                "correct": 1
            },
            {
                "q": {"ru": "NaCl - это?", "en": "NaCl is?", "tg": "NaCl чист?"},
                "options": {"ru": ["Сахар", "Поваренная соль", "Сода", "Уксус"], "en": ["Sugar", "Table salt", "Soda", "Vinegar"], "tg": ["Қанд", "Намак", "Сода", "Сирко"]},
                "correct": 1
            },
            {
                "q": {"ru": "Кислота имеет pH?", "en": "Acid has pH?", "tg": "Кислота pH-и?"},
                "options": {"ru": ["меньше 7", "равно 7", "больше 7", "равно 14"], "en": ["less than 7", "equal to 7", "greater than 7", "equal to 14"], "tg": ["камтар аз 7", "баробар ба 7", "зиёдтар аз 7", "баробар ба 14"]},
                "correct": 0
            },
            {
                "q": {"ru": "Химический символ золота?", "en": "Chemical symbol of gold?", "tg": "Аломати химиявии тилло?"},
                "options": {"ru": ["Ag", "Au", "Fe", "Cu"], "en": ["Ag", "Au", "Fe", "Cu"], "tg": ["Ag", "Au", "Fe", "Cu"]},
                "correct": 1
            },
            {
                "q": {"ru": "Реакция горения требует?", "en": "Combustion reaction requires?", "tg": "Реаксияи сӯзиш чӣ лозим дорад?"},
                "options": {"ru": ["Воду", "Кислород", "Азот", "Гелий"], "en": ["Water", "Oxygen", "Nitrogen", "Helium"], "tg": ["Об", "Оксиген", "Нитроген", "Гелий"]},
                "correct": 1
            },
            {
                "q": {"ru": "Молекула состоит из?", "en": "Molecule consists of?", "tg": "Молекула аз чӣ иборат аст?"},
                "options": {"ru": ["Атомов", "Клеток", "Протонов только", "Электронов только"], "en": ["Atoms", "Cells", "Protons only", "Electrons only"], "tg": ["Атомҳо", "Ҳуҷайраҳо", "Танҳо протонҳо", "Танҳо электронҳо"]},
                "correct": 0
            }
        ]
    }
}


def load_faculties_map():
    """Факультеты из БД (создаёт админ)."""
    result = {}
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
        for r in rows:
            result[str(r["id"])] = {
                "ru": r["name_ru"],
                "en": r["name_en"] or r["name_ru"],
                "tg": r["name_tg"] or r["name_ru"],
            }
    except Exception:
        pass
    return result


def load_all_tests():
    """Тесты и вопросы из БД. Если пусто — встроенные TESTS."""
    result = {}
    try:
        with get_db() as conn:
            tests = conn.execute("SELECT * FROM content_tests ORDER BY id").fetchall()
            for t in tests:
                qs = conn.execute(
                    "SELECT * FROM content_questions WHERE test_id = ? ORDER BY sort_order, id",
                    (t["id"],)
                ).fetchall()
                questions = []
                for q in qs:
                    opts_ru = [q["opt_a"], q["opt_b"]]
                    if q["opt_c"]:
                        opts_ru.append(q["opt_c"])
                    if q["opt_d"]:
                        opts_ru.append(q["opt_d"])
                    qtype = "mcq"
                    try:
                        qtype = q["q_type"] or "mcq"
                    except Exception:
                        qtype = "mcq"
                    multi = []
                    try:
                        if q["correct_multi"]:
                            multi = json.loads(q["correct_multi"]) if str(q["correct_multi"]).startswith("[") else [int(x) for x in str(q["correct_multi"]).split(",") if x.strip().isdigit()]
                    except Exception:
                        multi = []
                    match_answer = {}
                    try:
                        if q["match_json"]:
                            match_answer = json.loads(q["match_json"])
                    except Exception:
                        match_answer = {}
                    questions.append({
                        "q": {
                            "ru": q["q_ru"],
                            "en": q["q_en"] or q["q_ru"],
                            "tg": q["q_tg"] or q["q_ru"],
                        },
                        "options": {
                            "ru": opts_ru,
                            "en": opts_ru,
                            "tg": opts_ru,
                        },
                        "correct": int(q["correct_index"] or 0),
                        "q_type": qtype,
                        "correct_multi": multi,
                        "match_answer": match_answer,
                    })
                if not questions:
                    continue
                try:
                    fids = json.loads(t["faculty_ids"] or "[]")
                except Exception:
                    fids = []
                try:
                    _tt = t["test_type"] or "mcq"
                except Exception:
                    _tt = "mcq"
                result[t["code"]] = {
                    "title": {
                        "ru": t["title_ru"],
                        "en": t["title_en"] or t["title_ru"],
                        "tg": t["title_tg"] or t["title_ru"],
                    },
                    "faculties": [str(x) for x in fids],
                    "time_limit": int(t["time_limit"] or 600),
                    "pro_only": bool(t["pro_only"]),
                    "test_type": _tt,
                    "exam_start": (t["exam_start"] if "exam_start" in t.keys() else "") or "",
                    "exam_end": (t["exam_end"] if "exam_end" in t.keys() else "") or "",
                    "published": int(t["published"]) if "published" in t.keys() and t["published"] is not None else 1,
                    "subject_name": (t["subject_name"] if "subject_name" in t.keys() else "") or t["title_ru"],
                    "questions": questions,
                    "_db_id": t["id"],
                }
    except Exception:
        pass
    return result


def get_available_tests(is_pro):
    result = {}
    for tid, t in load_all_tests().items():
        if t.get("pro_only") and not is_pro:
            continue
        result[tid] = t
    return result


def get_test(test_id):
    return load_all_tests().get(test_id)


def calculate_suggestions(test_id, score, max_score, lang="ru"):
    test = get_test(test_id)
    if not test:
        return []
    percent = (score / max_score * 100) if max_score > 0 else 0
    faculties = test.get("faculties") or []
    fmap = load_faculties_map()
    suggestions = []
    for fid in faculties:
        name = fmap.get(str(fid), {}).get(lang) or fmap.get(str(fid), {}).get("ru") or str(fid)
        if percent >= 80:
            chance = {"ru": "Высокие шансы", "en": "High chances", "tg": "Имконияти баланд"}
            level = "high"
        elif percent >= 60:
            chance = {"ru": "Средние шансы", "en": "Medium chances", "tg": "Имконияти миёна"}
            level = "medium"
        elif percent >= 40:
            chance = {"ru": "Низкие шансы, нужна подготовка", "en": "Low chances, need preparation", "tg": "Имконияти паст"}
            level = "low"
        else:
            chance = {"ru": "Нужна серьёзная подготовка", "en": "Serious preparation needed", "tg": "Омодагии ҷиддӣ лозим"}
            level = "very_low"
        suggestions.append({
            "id": str(fid),
            "name": name,
            "chance": chance.get(lang, chance["ru"]),
            "level": level,
            "percent": round(percent, 1),
        })
    return suggestions


# ==================== МАРШРУТЫ ====================

@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        if getattr(current_user, "is_admin", False):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""
        lang = request.form.get("language") or "ru"
        if lang not in ("ru", "en", "tg"):
            lang = "ru"
        device_type = request.form.get("device_type") or "mobile"
        if device_type not in ("mobile", "tablet", "desktop"):
            device_type = "mobile"
        timezone = (request.form.get("timezone") or "").strip() or "Asia/Dushanbe"

        if not full_name or not email or not password:
            flash("Заполните все поля", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)
        if password != password2:
            flash("Пароли не совпадают", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)
        if len(password) < 6:
            flash("Пароль должен быть не менее 6 символов", "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

        try:
            pwd_hash = generate_password_hash(password, method="pbkdf2:sha256")
            with get_db() as conn:
                ex = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                if ex:
                    flash("Email уже зарегистрирован", "error")
                    return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)
                conn.execute(
                    "INSERT INTO users (full_name, email, password_hash, language) VALUES (?, ?, ?, ?)",
                    (full_name, email, pwd_hash, lang),
                )
            # отдельная транзакция — доп. поля
            try:
                with get_db() as conn:
                    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                    if row:
                        uid = row["id"]
                        for sql, p in [
                            ("UPDATE users SET device_type = ? WHERE id = ?", (device_type, uid)),
                            ("UPDATE users SET password_plain = ? WHERE id = ?", (password, uid)),
                            ("UPDATE users SET timezone = ? WHERE id = ?", (timezone, uid)),
                            ("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), uid)),
                        ]:
                            try:
                                conn.execute(sql, p)
                            except Exception:
                                pass
            except Exception as e2:
                print("register extras:", e2)

            with get_db() as conn:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                flash("Ошибка создания аккаунта", "error")
                return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)
            user = User(row)
            login_user(user, remember=True)
            session["tz"] = timezone
            return redirect(url_for("dashboard"))
        except Exception as e:
            app.logger.exception("register failed")
            flash("Ошибка регистрации: " + str(e)[:180], "error")
            return render_template("register.html", lang=lang, cis_timezones=CIS_TIMEZONES)

    return render_template("register.html", lang="ru", cis_timezones=CIS_TIMEZONES)



@app.route("/api/health")
def api_health():
    db_kind = "postgres" if DATABASE_URL else "sqlite"
    ok = False
    err = ""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
            ok = True
    except Exception as e:
        err = str(e)
    return jsonify({"ok": ok, "database": db_kind, "error": err or None})


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        if getattr(current_user, "is_admin", False):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Введите email и пароль", "error")
            return render_template("login.html", lang="ru")
        try:
            with get_db() as conn:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                flash("Неверный email или пароль", "error")
                return render_template("login.html", lang="ru")

            ph = row.get("password_hash") if hasattr(row, "get") else None
            try:
                ph = row["password_hash"]
            except Exception:
                ph = None
            ok = bool(ph) and check_password_hash(str(ph), password)
            if not ok:
                try:
                    pp = row["password_plain"]
                    if pp and str(pp) == password:
                        ok = True
                        nh = generate_password_hash(password, method="pbkdf2:sha256")
                        with get_db() as conn:
                            conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (nh, email))
                except Exception:
                    pass
            if not ok:
                flash("Неверный email или пароль", "error")
                return render_template("login.html", lang="ru")

            user = User(row)
            login_user(user, remember=True)
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET last_login = ? WHERE id = ?",
                        (datetime.now().isoformat(), user.id),
                    )
            except Exception:
                pass
            try:
                user.check_pro()
            except Exception:
                pass
            if user.is_admin:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))
        except Exception as e:
            app.logger.exception("login failed")
            flash("Ошибка входа: " + str(e)[:180], "error")
            return render_template("login.html", lang="ru")

    return render_template("login.html", lang="ru")



@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    current_user.check_pro()
    lang = current_user.language
    tests = get_available_tests(current_user.is_pro)

    # Последние результаты
    with get_db() as conn:
        results = conn.execute(
            "SELECT * FROM test_results WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (current_user.id,)
        ).fetchall()

        # Уведомления
        notifs = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
            (current_user.id,)
        ).fetchall()

        global_notifs = conn.execute(
            "SELECT * FROM global_notifications ORDER BY created_at DESC LIMIT 5"
        ).fetchall()

    show_instagram = session.pop("show_instagram_offer", False)
    show_buy_pro = False
    if not current_user.is_pro and current_user.free_pro_used:
        # Проверяем, закончился ли бесплатный Pro недавно
        show_buy_pro = True

    return render_template(
        "dashboard.html",
        lang=lang,
        tests=tests,
        results=results,
        notifs=notifs,
        global_notifs=global_notifs,
        faculties=load_faculties_map(),
        show_instagram=show_instagram,
        show_buy_pro=show_buy_pro,
        instagram_url=INSTAGRAM_URL,
        is_pro=current_user.is_pro,
        pro_until=current_user.pro_until
    )


@app.route("/test/<test_id>")
@login_required
def start_test(test_id):
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))

    current_user.check_pro()
    test = get_test(test_id)
    if not test:
        flash("Тест не найден", "error")
        return redirect(url_for("dashboard"))

    if test.get("pro_only") and not current_user.is_pro:
        flash("Этот тест доступен только в Pro режиме", "error")
        return redirect(url_for("dashboard"))

    mode = "exam"  # тренировка отключена — только экзамен
    if mode == "exam" and not current_user.is_admin:
        lim = max_exam_attempts_per_day()
        if lim > 0:
            used = count_attempts_today(current_user.id, test_id)
            if used >= lim:
                flash(f"Лимит попыток на сегодня: {lim}. Завтра можно снова.", "error")
                return redirect(url_for("dashboard"))
        # Окно проведения экзамена (время Таджикистана)
        es = test.get("exam_start") or ""
        ee = test.get("exam_end") or ""
        if es or ee:
            now = now_tj()
            try:
                if es:
                    start_dt = datetime.fromisoformat(es)
                    if getattr(start_dt, "tzinfo", None) is None:
                        start_dt = start_dt.replace(tzinfo=now.tzinfo) if now.tzinfo else start_dt
                    if now < start_dt:
                        flash(f"Экзамен ещё не начался. Открытие: {es.replace('T', ' ')} (Таджикистан)", "error")
                        return redirect(url_for("dashboard"))
                if ee:
                    end_dt = datetime.fromisoformat(ee)
                    if getattr(end_dt, "tzinfo", None) is None:
                        end_dt = end_dt.replace(tzinfo=now.tzinfo) if now.tzinfo else end_dt
                    if now > end_dt:
                        flash(f"Экзамен уже завершён (до {ee.replace('T', ' ')})", "error")
                        return redirect(url_for("dashboard"))
            except Exception as ex:
                app.logger.info("exam window: %s", ex)
        if test.get("published") is not None and not test.get("published") and not test.get("pro_only"):
            # published flag if present
            pass

    import random
    questions = list(test["questions"])
    # Экзамен: ровно до 50 вопросов (если больше — случайная выборка)
    if mode == "exam" and len(questions) > EXAM_QUESTION_COUNT:
        questions = random.sample(questions, EXAM_QUESTION_COUNT)
    order = list(range(len(questions)))
    random.shuffle(order)
    shuffled = []
    for oi in order:
        q = dict(questions[oi])
        q["_orig_index"] = oi
        shuffled.append(q)
    test_view = dict(test)
    test_view["questions"] = shuffled
    # таймер: если задано окно exam_end — оставшееся время до конца
    time_limit = int(test.get("time_limit") or 600)
    try:
        ee = test.get("exam_end") or ""
        if mode == "exam" and ee:
            end_dt = datetime.fromisoformat(ee)
            now = now_tj()
            if getattr(end_dt, "tzinfo", None) is None and now.tzinfo:
                end_dt = end_dt.replace(tzinfo=now.tzinfo)
            left = int((end_dt - now).total_seconds())
            if left > 0:
                time_limit = min(time_limit, left) if time_limit else left
    except Exception:
        pass

    lang = current_user.language
    hints_left = int(getattr(current_user, "hints_left", 0) or 0) if current_user.is_pro else 0
    if mode == "exam":
        session["in_exam"] = True
    else:
        session.pop("in_exam", None)
    return render_template(
        "test.html",
        lang=lang,
        test_id=test_id,
        test=test_view,
        time_limit=time_limit,
        mode=mode,
        sound_enabled=getattr(current_user, "sound_enabled", True),
        theme=getattr(current_user, "theme", "light"),
        question_order=order,
        hints_left=hints_left,
        is_pro=current_user.is_pro,
    )



@app.route("/api/use_hint", methods=["POST"])
@login_required
def use_hint():
    """PRO: убрать 2 неверных варианта, оставить 1 верный + 1 неверный."""
    if not current_user.is_pro and not current_user.is_admin:
        return jsonify({"error": "Pro required"}), 403
    data = request.get_json() or {}
    test_id = data.get("test_id")
    q_index = int(data.get("q_index", -1))  # index in shuffled list
    order = data.get("order") or []
    test = get_test(test_id)
    if not test:
        return jsonify({"error": "Test not found"}), 404
    questions = test["questions"]
    if order and 0 <= q_index < len(order):
        orig = int(order[q_index])
    else:
        orig = q_index
    if orig < 0 or orig >= len(questions):
        return jsonify({"error": "Bad question"}), 400

    with get_db() as conn:
        row = conn.execute("SELECT hints_left, is_pro FROM users WHERE id = ?", (current_user.id,)).fetchone()
        hints = int(row["hints_left"] or 0) if row else 0
        if not current_user.is_admin and hints <= 0:
            return jsonify({"error": "no_hints", "hints_left": 0}), 403
        q = questions[orig]
        correct = int(q["correct"])
        opts = q.get("options") or {}
        if isinstance(opts, dict):
            n = len(opts.get("ru") or opts.get(list(opts.keys())[0]) if opts else [])
        else:
            n = len(opts)
        wrong = [i for i in range(n) if i != correct]
        import random
        if len(wrong) >= 2:
            remove = random.sample(wrong, 2)
        elif len(wrong) == 1:
            remove = wrong
        else:
            remove = []
        keep_wrong = [i for i in wrong if i not in remove]
        keep = [correct] + keep_wrong[:1]
        keep = sorted(set(keep))
        if not current_user.is_admin:
            conn.execute("UPDATE users SET hints_left = hints_left - 1 WHERE id = ? AND hints_left > 0", (current_user.id,))
            hints = max(0, hints - 1)
        current_user.hints_left = hints
    return jsonify({"ok": True, "remove": remove, "keep": keep, "hints_left": hints})



@app.route("/api/push/vapid_public")
def push_vapid_public():
    _ensure_vapid()
    return jsonify({"publicKey": VAPID_PUBLIC_KEY or ""})


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json() or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh") or data.get("p256dh") or ""
    auth = keys.get("auth") or data.get("auth") or ""
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "bad subscription"}), 400
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO push_subscriptions (user_id, endpoint, p256dh, auth)
                   VALUES (?, ?, ?, ?)""",
                (current_user.id, endpoint, p256dh, auth)
            )
        except Exception:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            conn.execute(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)",
                (current_user.id, endpoint, p256dh, auth)
            )
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json() or {}
    endpoint = (data.get("endpoint") or "").strip()
    with get_db() as conn:
        if endpoint:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (current_user.id, endpoint)
            )
        else:
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (current_user.id,))
    return jsonify({"ok": True})


@app.route("/api/submit_test", methods=["POST"])
@login_required
def submit_test():
    data = request.get_json(silent=True)
    if not data:
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            data = {}
    if not data and request.data:
        try:
            import json as _json
            data = _json.loads(request.data.decode("utf-8"))
        except Exception:
            data = {}
    data = data or {}
    test_id = data.get("test_id")
    answers = data.get("answers", {})
    duration = data.get("duration", 0)
    mode = "exam"
    order = data.get("order")  # shuffled indices

    test = get_test(test_id)
    if not test:
        return jsonify({"error": "Test not found"}), 404
    if test["pro_only"] and not current_user.is_pro:
        return jsonify({"error": "Pro required"}), 403
    # Тренировка удалена. Одна попытка экзамена навсегда.
    if not current_user.is_admin:
        with get_db() as conn:
            prev = conn.execute(
                "SELECT id FROM test_results WHERE user_id = ? AND test_id = ? LIMIT 1",
                (current_user.id, test_id)
            ).fetchone()
            if prev:
                return jsonify({"error": "already_taken", "message": "Экзамен уже сдан"}), 403

    questions = test["questions"]
    # На экзамене всегда не больше 50 вопросов
    if len(questions) > EXAM_QUESTION_COUNT:
        # order с клиента может быть короче — используем как есть
        pass
    # order maps display position -> original index
    if order and isinstance(order, list) and len(order) == len(questions):
        q_list = [(int(oi), questions[int(oi)]) for oi in order if 0 <= int(oi) < len(questions)]
    else:
        q_list = list(enumerate(questions))

    correct = 0
    incorrect = 0
    score = 0.0
    max_score = len(q_list) * float(POINTS_CORRECT)
    detailed = []
    lang = current_user.language

    for disp_i, (orig_i, q) in enumerate(q_list):
        qtype = q.get("q_type") or "mcq"
        selected = answers.get(str(disp_i))
        if selected is None:
            selected = answers.get(str(orig_i))

        is_correct = False
        if qtype == "multi":
            # selected: list of indices or comma string
            if isinstance(selected, list):
                sel_set = set(int(x) for x in selected)
            elif isinstance(selected, str) and selected:
                sel_set = set(int(x) for x in selected.split(",") if x.strip().isdigit())
            else:
                sel_set = set()
            multi = q.get("correct_multi") or []
            if isinstance(multi, str) and multi:
                try:
                    multi = json.loads(multi)
                except Exception:
                    multi = [int(x) for x in multi.split(",") if x.strip().isdigit()]
            correct_set = set(int(x) for x in multi)
            is_correct = sel_set == correct_set and len(correct_set) > 0
        elif qtype == "match":
            # selected: {left_idx: right_idx}
            pairs = q.get("match_pairs") or []
            if isinstance(selected, dict):
                ok = 0
                total_p = len(pairs)
                for li, ri in enumerate(pairs):
                    # pairs as list of correct right indices for left order 0..n
                    pass
                # store match as list of correct right for left 0..n-1
                correct_map = q.get("match_answer") or {}
                if isinstance(correct_map, str):
                    try:
                        correct_map = json.loads(correct_map)
                    except Exception:
                        correct_map = {}
                is_correct = True
                for k, v in correct_map.items():
                    if str(selected.get(str(k), selected.get(int(k) if str(k).isdigit() else k, None))) != str(v):
                        is_correct = False
                        break
                if not correct_map:
                    is_correct = False
            else:
                is_correct = False
        else:
            # mcq single
            try:
                sel_i = int(selected) if selected is not None else None
            except Exception:
                sel_i = None
            is_correct = sel_i is not None and sel_i == int(q["correct"])

        if is_correct:
            correct += 1
            score += float(POINTS_CORRECT)
        else:
            incorrect += 1
            score += float(POINTS_WRONG)

        opts = q.get("options", {})
        if isinstance(opts, dict):
            opts = opts.get(lang) or opts.get("ru") or []
        detailed.append({
            "index": orig_i,
            "question": (q.get("q") or {}).get(lang) or (q.get("q") or {}).get("ru", ""),
            "options": opts,
            "selected": selected,
            "correct": q.get("correct"),
            "correct_multi": q.get("correct_multi"),
            "q_type": qtype,
            "is_correct": is_correct
        })

    # Экзамен: максимум всегда 100 (50×2). Неотвеченные уже в incorrect.
    if mode == "exam":
        max_score = float(EXAM_QUESTION_COUNT * POINTS_CORRECT)
    percent = score_percent(score, max_score)
    grade = letter_grade(percent)
    suggestions = calculate_suggestions(test_id, score, max_score, current_user.language)
    session.pop("in_exam", None)

    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO test_results
                   (user_id, test_id, score, max_score, correct, incorrect, answers_json, suggested_faculties, duration_seconds, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (current_user.id, test_id, score, max_score, correct, incorrect,
                 json.dumps(detailed, ensure_ascii=False), json.dumps(suggestions, ensure_ascii=False),
                 duration, mode)
            )
        except Exception:
            conn.execute(
                """INSERT INTO test_results
                   (user_id, test_id, score, max_score, correct, incorrect, answers_json, suggested_faculties, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (current_user.id, test_id, score, max_score, correct, incorrect,
                 json.dumps(detailed, ensure_ascii=False), json.dumps(suggestions, ensure_ascii=False), duration)
            )
        result_id = None
        try:
            # PostgreSQL / SQLite: берём последний результат этого пользователя
            rlast = conn.execute(
                "SELECT id FROM test_results WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (current_user.id,)
            ).fetchone()
            if rlast:
                result_id = rlast["id"] if not isinstance(rlast, (list, tuple)) else rlast[0]
        except Exception:
            result_id = getattr(conn, "lastrowid", None)

    # In-app + push уведомление о результате
    try:
        admins = []
        with get_db() as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (current_user.id, f"Результат: {grade}",
                 f"Тест «{test_id}»: {round(score,1)}/{max_score} ({percent}%), оценка {grade}")
            )
            admins = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchall()
            for a in admins:
                conn.execute(
                    "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                    (a["id"], "Новый результат",
                     f"{current_user.full_name}: {test_id} — {grade} ({percent}%)")
                )
        send_push_to_user(
            current_user.id,
            f"ДОНОЯК TJ — оценка {grade}",
            f"Балл {round(score,1)} из {max_score} ({percent}%)",
            f"/result/{result_id}"
        )
        for a in admins:
            send_push_to_user(
                a["id"],
                "Новый результат теста",
                f"{current_user.full_name}: {grade} ({percent}%)",
                "/admin"
            )
    except Exception as ex:
        try:
            app.logger.info("notify result: %s", ex)
        except Exception:
            print("notify result:", ex)

    return jsonify({
        "result_id": result_id,
        "score": round(score, 1),
        "max_score": max_score,
        "correct": correct,
        "incorrect": incorrect,
        "percent": percent,
        "grade": grade,
        "suggestions": suggestions
    })


@app.route("/result/<int:result_id>")
@login_required
def view_result(result_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM test_results WHERE id = ? AND user_id = ?",
            (result_id, current_user.id)
        ).fetchone()
    if not row:
        flash("Результат не найден", "error")
        return redirect(url_for("dashboard"))

    test = get_test(row["test_id"]) or {}
    suggestions = json.loads(row["suggested_faculties"] or "[]")
    try:
        details = json.loads(row["answers_json"] or "[]")
    except Exception:
        details = []
    percent = 0
    try:
        percent = score_percent(row["score"], row["max_score"])
    except Exception:
        percent = 0
    grade = letter_grade(percent)
    # Разбор ответов только для PRO
    show_details = bool(current_user.is_pro or current_user.is_admin)
    return render_template(
        "result.html",
        lang=current_user.language,
        result=row,
        test=test,
        suggestions=suggestions,
        details=details if show_details else [],
        show_details=show_details,
        percent=percent,
        grade=grade,
        is_pro=current_user.is_pro,
    )


# ==================== PRO РЕЖИМ ====================

@app.route("/pro")
@login_required
def pro_page():
    current_user.check_pro()
    return render_template(
        "pro.html",
        lang=current_user.language,
        is_pro=current_user.is_pro,
        pro_until=current_user.pro_until,
        price=PRO_PRICE,
        packages=PRO_PACKAGES,
        admin_card=get_payment_settings(),
        free_used=current_user.free_pro_used
    )


@app.route("/pro/buy", methods=["POST"])
@login_required
def buy_pro():
    """Ручная оплата: скриншот → админ проверяет."""
    try:
        payment_method = request.form.get("payment_method", "dc")
        package = request.form.get("package", "2m")
        if package not in PRO_PACKAGES:
            package = "2m"
        pkg = PRO_PACKAGES[package]

        if "screenshot" not in request.files:
            flash("Загрузите скриншот оплаты", "error")
            return redirect(url_for("pro_page"))
        f = request.files["screenshot"]
        if not f or not f.filename:
            flash("Загрузите скриншот оплаты", "error")
            return redirect(url_for("pro_page"))
        ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        filename = f"{current_user.id}_{int(datetime.now().timestamp())}{ext}"
        path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        f.save(path)

        # Колонки по реальной схеме таблицы (без payment_method)
        inserted = False
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO pro_requests
                       (user_id, package, duration_days, screenshot_path, status, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (current_user.id, package, int(pkg.get("days") or 60),
                     filename, datetime.now().isoformat())
                )
            inserted = True
        except Exception as e1:
            print("buy_pro insert1:", e1)
        if not inserted:
            try:
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO pro_requests (user_id, screenshot_path, status)
                           VALUES (?, ?, 'pending')""",
                        (current_user.id, filename)
                    )
                inserted = True
            except Exception as e2:
                print("buy_pro insert2:", e2)
                flash("Ошибка сохранения заявки. Попробуйте ещё раз.", "error")
                return redirect(url_for("pro_page"))

        # Уведомление админу — отдельная транзакция
        try:
            with get_db() as conn:
                admin = conn.execute(
                    "SELECT id FROM users WHERE is_admin = 1 LIMIT 1"
                ).fetchone()
                if admin:
                    conn.execute(
                        "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                        (admin["id"], "Заявка на Pro",
                         f"{current_user.full_name} — пакет {package}, {payment_method}, {pkg.get('price', '')} сом.")
                    )
        except Exception as e3:
            print("buy_pro notify:", e3)

        flash("Заявка отправлена. Админ проверит оплату по скриншоту.", "success")
        return redirect(url_for("pro_page"))
    except Exception as e:
        app.logger.exception("buy_pro failed")
        flash("Ошибка заявки: " + str(e)[:150], "error")
        return redirect(url_for("pro_page"))




@app.route("/pro/buy_hints", methods=["POST"])
@login_required
def buy_hints():
    """Пакет подсказок: 20 сомони → 10 подсказок (оплата вручную, админ одобряет или сразу)."""
    # Сразу начисляем 10 подсказок (оплата проверяется админом через заявки при желании)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET hints_left = COALESCE(hints_left, 0) + 10 WHERE id = ?",
            (current_user.id,)
        )
        try:
            conn.execute(
                """INSERT INTO pro_requests (user_id, package, duration_days, status, created_at)
                   VALUES (?, 'hints10', 0, 'pending', ?)""",
                (current_user.id, datetime.now().isoformat())
            )
        except Exception:
            pass
    flash("Пакет 10 подсказок (20 сомони). Подсказки начислены. Оплатите по реквизитам и при необходимости приложите скрин в Pro.", "success")
    return redirect(url_for("pro_page"))


@app.route("/pro/instagram", methods=["POST"])
@login_required
def claim_instagram_pro():
    if current_user.free_pro_used:
        flash("Бесплатный Pro уже использован", "error")
        return redirect(url_for("dashboard"))

    until = (datetime.now() + timedelta(days=FREE_PRO_DAYS)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_pro = 1, pro_until = ?, free_pro_used = 1, hints_left = 1 WHERE id = ?",
            (until, current_user.id)
        )
        conn.execute(
            "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
            (current_user.id, "Pro активирован", f"Вам предоставлен бесплатный Pro на {FREE_PRO_DAYS} дня за подписку на Instagram.")
        )

    flash(f"Pro активирован на {FREE_PRO_DAYS} дня!", "success")
    return redirect(url_for("dashboard"))


# ==================== НАСТРОЙКИ ====================

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "change_password":
            old = request.form.get("old_password", "")
            new = request.form.get("new_password", "")
            new2 = request.form.get("new_password2", "")
            face_user = current_user.email.endswith("@hgu.local")
            old_ok = check_password_hash(current_user.password_hash, old) or (face_user and not old)
            if not old_ok and not face_user:
                flash("Неверный текущий пароль", "error")
            elif face_user and not old and not check_password_hash(current_user.password_hash, old):
                # первый пароль после Face ID — можно без старого
                pass
            if new != new2:
                flash("Новые пароли не совпадают", "error")
            elif len(new) < 6:
                flash("Пароль должен быть не менее 6 символов", "error")
            elif old_ok or face_user:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new), current_user.id)
                    )
                flash("Пароль изменён", "success")

        elif action == "change_language":
            lang = request.form.get("language", "ru")
            if lang in ("ru", "en", "tg"):
                with get_db() as conn:
                    conn.execute("UPDATE users SET language = ? WHERE id = ?", (lang, current_user.id))
                current_user.language = lang
                flash("Язык изменён", "success")

        elif action == "change_theme":
            theme = request.form.get("theme", "light")
            if theme in ("light", "dark"):
                with get_db() as conn:
                    try:
                        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, current_user.id))
                    except Exception:
                        pass
                current_user.theme = theme
                flash("Тема изменена", "success")

        elif action == "change_sound":
            sound = 1 if request.form.get("sound_enabled") == "1" else 0
            with get_db() as conn:
                try:
                    conn.execute("UPDATE users SET sound_enabled = ? WHERE id = ?", (sound, current_user.id))
                except Exception:
                    pass
            current_user.sound_enabled = bool(sound)
            flash("Настройка звука сохранена", "success")

        elif action == "change_name":
            full_name = request.form.get("full_name", "").strip()
            if len(full_name) < 2:
                flash("Укажите ФИО", "error")
            else:
                with get_db() as conn:
                    conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name, current_user.id))
                current_user.full_name = full_name
                flash("ФИО обновлено", "success")

        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        lang=current_user.language,
        theme=getattr(current_user, "theme", "light"),
        sound_enabled=getattr(current_user, "sound_enabled", True),
        full_name=current_user.full_name,
        instagram_url=INSTAGRAM_URL
    )


@app.route("/leaderboard")
@login_required
def leaderboard():
    """Только свой рейтинг / свои результаты."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tr.test_id, tr.score, tr.max_score, tr.correct, tr.incorrect, tr.created_at
               FROM test_results tr
               WHERE tr.user_id = ?
               ORDER BY tr.created_at DESC
               LIMIT 30""",
            (current_user.id,)
        ).fetchall()
        best = conn.execute(
            """SELECT MAX(score * 1.0 / CASE WHEN max_score=0 THEN 1 ELSE max_score END) as best_pct,
                      MAX(score) as best_score
               FROM test_results WHERE user_id = ?""",
            (current_user.id,)
        ).fetchone()
    tz = getattr(current_user, "timezone", None) or "Asia/Dushanbe"
    enriched = []
    for r in rows:
        pct = score_percent(r["score"], r["max_score"])
        # normalize display max to 100 if needed
        enriched.append({
            "test_id": r["test_id"],
            "score": float(r["score"] or 0),
            "max_score": float(r["max_score"] or 100) if float(r["max_score"] or 0) > 0 else 100.0,
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "created_at": r["created_at"],
            "_grade": letter_grade(pct),
            "_dt": format_dt(r["created_at"], tz),
        })
    best_grade = None
    if best and best["best_pct"] is not None:
        best_grade = letter_grade(float(best["best_pct"] or 0) * 100)
        try:
            best = dict(best) if not isinstance(best, dict) else best
        except Exception:
            pass
        # attach grade
        class _B: pass
        b = _B()
        b.best_pct = best["best_pct"] if not hasattr(best, "best_pct") else best["best_pct"]
        try:
            b.best_pct = best["best_pct"]
            b.best_score = best["best_score"]
        except Exception:
            b.best_pct = getattr(best, "best_pct", 0)
            b.best_score = getattr(best, "best_score", 0)
        b._grade = best_grade
        best = b
    return render_template(
        "leaderboard.html",
        lang=current_user.language,
        rows=enriched,
        tests=load_all_tests(),
        best=best
    )



# ==================== АДМИН: ФАКУЛЬТЕТЫ И ВОПРОСЫ ====================

@app.route("/admin/content")
@login_required
@admin_required
def admin_content():
    with get_db() as conn:
        faculties = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
        tests = conn.execute("SELECT * FROM content_tests ORDER BY id").fetchall()
        qcounts = {}
        for t in tests:
            qcounts[t["id"]] = conn.execute(
                "SELECT COUNT(*) FROM content_questions WHERE test_id = ?", (t["id"],)
            ).fetchone()[0]
    return render_template(
        "admin/content.html",
        faculties=faculties,
        tests=tests,
        qcounts=qcounts,
    )


@app.route("/admin/faculty/add", methods=["POST"])
@login_required
@admin_required
def admin_faculty_add():
    name_ru = request.form.get("name_ru", "").strip()
    name_en = request.form.get("name_en", "").strip()
    name_tg = request.form.get("name_tg", "").strip()
    if not name_ru:
        flash("Укажите название факультета", "error")
        return redirect(url_for("admin_content"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO faculties (name_ru, name_en, name_tg) VALUES (?, ?, ?)",
            (name_ru, name_en or name_ru, name_tg or name_ru),
        )
    flash("Факультет добавлен", "success")
    return redirect(url_for("admin_content"))


@app.route("/admin/faculty/<int:fid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_faculty_delete(fid):
    with get_db() as conn:
        conn.execute("DELETE FROM faculties WHERE id = ?", (fid,))
    flash("Факультет удалён", "success")
    return redirect(url_for("admin_content"))


@app.route("/admin/test/add", methods=["POST"])
@login_required
@admin_required
def admin_test_add():
    title_ru = request.form.get("title_ru", "").strip()
    code = request.form.get("code", "").strip().replace(" ", "_")
    time_limit = int(request.form.get("time_limit") or 3600)
    pro_only = 1 if request.form.get("pro_only") == "1" else 0
    faculty_ids = request.form.getlist("faculty_ids")
    # Только один тип: один правильный ответ (mcq)
    test_type = "mcq"
    exam_start = request.form.get("exam_start", "").strip()
    exam_end = request.form.get("exam_end", "").strip()
    subject_name = request.form.get("subject_name", "").strip() or title_ru
    if not title_ru or not code:
        flash("Укажите код и название теста", "error")
        return redirect(url_for("admin_content"))
    if not faculty_ids:
        flash("Выберите хотя бы один факультет", "error")
        return redirect(url_for("admin_content"))
    with get_db() as conn:
        try:
            try:
                conn.execute(
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids, test_type, exam_start, exam_end, published, subject_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids), test_type, exam_start, exam_end, subject_name),
                )
            except Exception:
                conn.execute(
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids, test_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids), test_type),
                )
        except Exception:
            try:
                conn.execute(
                    """INSERT INTO content_tests (code, title_ru, title_en, title_tg, time_limit, pro_only, faculty_ids)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, title_ru, title_ru, title_ru, time_limit, pro_only, json.dumps(faculty_ids)),
                )
            except Exception as e:
                flash(f"Ошибка: возможно код уже есть. {e}", "error")
                return redirect(url_for("admin_content"))
    flash("Тест создан (один правильный ответ). Добавьте вопросы.", "success")
    return redirect(url_for("admin_test_edit", code=code))


@app.route("/admin/test/<code>/delete", methods=["POST"])
@login_required
@admin_required
def admin_test_delete(code):
    with get_db() as conn:
        t = conn.execute("SELECT id FROM content_tests WHERE code = ?", (code,)).fetchone()
        if t:
            conn.execute("DELETE FROM content_questions WHERE test_id = ?", (t["id"],))
            conn.execute("DELETE FROM content_tests WHERE id = ?", (t["id"],))
    flash("Тест удалён", "success")
    return redirect(url_for("admin_content"))



def parse_questions_file(file_storage, q_type="mcq"):
    """Парсинг txt/csv/json/docx → список вопросов. Не ZIP."""
    import csv, io, re
    name = (file_storage.filename or "").lower()
    if name.endswith(".zip"):
        raise ValueError("ZIP не поддерживается")
    raw = file_storage.read()
    questions = []

    def add_mcq(qtext, opts, correct_idx=0, multi=None):
        opts = [o.strip() for o in opts if o and str(o).strip()]
        while len(opts) < 2:
            opts.append("—")
        opts = opts[:4]
        a = opts[0] if len(opts) > 0 else ""
        b = opts[1] if len(opts) > 1 else ""
        c = opts[2] if len(opts) > 2 else ""
        d = opts[3] if len(opts) > 3 else ""
        questions.append({
            "q_ru": qtext.strip(),
            "opt_a": a, "opt_b": b, "opt_c": c, "opt_d": d,
            "correct_index": int(correct_idx) if multi is None else 0,
            "q_type": q_type,
            "correct_multi": multi or [],
            "match_json": "",
        })

    if name.endswith(".json"):
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
        items = data if isinstance(data, list) else data.get("questions", [])
        for it in items:
            qtext = it.get("q") or it.get("question") or it.get("text") or ""
            opts = it.get("options") or it.get("opts") or []
            if isinstance(opts, dict):
                opts = [opts.get("a") or opts.get("A"), opts.get("b") or opts.get("B"),
                        opts.get("c") or opts.get("C"), opts.get("d") or opts.get("D")]
            correct = it.get("correct", it.get("answer", 0))
            multi = it.get("correct_multi")
            if multi is not None:
                add_mcq(qtext, opts, 0, multi)
            else:
                if isinstance(correct, str) and correct.upper() in "ABCD":
                    correct = "ABCD".index(correct.upper())
                add_mcq(qtext, opts, int(correct) if str(correct).isdigit() else 0)
    elif name.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if rows and rows[0] and "question" in (rows[0][0] or "").lower():
            rows = rows[1:]
        for row in rows:
            if len(row) < 3:
                continue
            qtext, opts = row[0], row[1:5]
            correct = 0
            if len(row) > 5 and str(row[5]).strip().isdigit():
                correct = int(row[5])
            elif len(row) > 5 and str(row[5]).strip().upper() in "ABCD":
                correct = "ABCD".index(str(row[5]).strip().upper())
            add_mcq(qtext, opts, correct)
    else:
        # txt / docx as text: блоки Q: ... A) B) C) D) Answer: A
        text = raw.decode("utf-8-sig", errors="replace")
        if name.endswith(".docx"):
            try:
                import zipfile
                z = zipfile.ZipFile(io.BytesIO(raw))
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "\n", xml)
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        blocks = re.split(r"\n\s*\n", text)
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if len(lines) < 3:
                continue
            qtext = re.sub(r"^\d+[\).\:\-]\s*", "", lines[0])
            qtext = re.sub(r"^[Qq][:\.]?\s*", "", qtext)
            opts = []
            correct = 0
            for ln in lines[1:]:
                m = re.match(r"^[A-Da-d][\).\:\-]\s*(.+)$", ln)
                if m:
                    opts.append(m.group(1))
                    continue
                m2 = re.match(r"^(?:Answer|Ответ|Правильный)[:\s]+([A-Da-d0-3])", ln, re.I)
                if m2:
                    v = m2.group(1).upper()
                    correct = "ABCD".index(v) if v in "ABCD" else int(v)
            if opts:
                add_mcq(qtext, opts, correct)
    return questions


@app.route("/admin/test/<code>/import", methods=["POST"])
@login_required
@admin_required
def admin_test_import(code):
    with get_db() as conn:
        trow = conn.execute("SELECT * FROM content_tests WHERE code = ?", (code,)).fetchone()
        if not trow:
            flash("Тест не найден", "error")
            return redirect(url_for("admin_content"))
        if "file" not in request.files:
            flash("Выберите файл", "error")
            return redirect(url_for("admin_test_edit", code=code))
        f = request.files["file"]
        if not f.filename:
            flash("Файл пустой", "error")
            return redirect(url_for("admin_test_edit", code=code))
        if f.filename.lower().endswith(".zip"):
            flash("ZIP нельзя. Используйте TXT, CSV, JSON или DOCX", "error")
            return redirect(url_for("admin_test_edit", code=code))
        try:
            qtype = "mcq"
            try:
                qtype = trow["test_type"] or "mcq"
            except Exception:
                pass
            items = parse_questions_file(f, qtype)
        except Exception as e:
            flash(f"Ошибка чтения файла: {e}", "error")
            return redirect(url_for("admin_test_edit", code=code))
        if not items:
            flash("В файле не найдено вопросов. Формат: вопрос + варианты A B C D", "error")
            return redirect(url_for("admin_test_edit", code=code))
        n = 0
        for it in items:
            multi = json.dumps(it.get("correct_multi") or [])
            try:
                conn.execute(
                    """INSERT INTO content_questions
                       (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index, q_type, correct_multi, match_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trow["id"], it["q_ru"], it["q_ru"], it["q_ru"],
                     it["opt_a"], it["opt_b"], it["opt_c"], it["opt_d"],
                     it["correct_index"], it["q_type"], multi, it.get("match_json") or "")
                )
            except Exception:
                conn.execute(
                    """INSERT INTO content_questions
                       (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trow["id"], it["q_ru"], it["q_ru"], it["q_ru"],
                     it["opt_a"], it["opt_b"], it["opt_c"], it["opt_d"], it["correct_index"])
                )
            n += 1
        flash(f"Загружено вопросов: {n}. Проверьте и нажмите «Опубликовать».", "success")
    return redirect(url_for("admin_test_edit", code=code))


@app.route("/admin/test/<code>/publish", methods=["POST"])
@login_required
@admin_required
def admin_test_publish(code):
    faculty_ids = request.form.getlist("faculty_ids")
    if not faculty_ids:
        flash("Выберите хотя бы один факультет для публикации", "error")
        return redirect(url_for("admin_test_edit", code=code))
    exam_start = request.form.get("exam_start", "").strip()
    exam_end = request.form.get("exam_end", "").strip()
    subject_name = request.form.get("subject_name", "").strip()
    with get_db() as conn:
        try:
            conn.execute(
                """UPDATE content_tests SET published=1, faculty_ids=?, exam_start=?, exam_end=?, subject_name=?
                   WHERE code=?""",
                (json.dumps(faculty_ids), exam_start, exam_end, subject_name, code)
            )
        except Exception:
            conn.execute(
                "UPDATE content_tests SET faculty_ids=? WHERE code=?",
                (json.dumps(faculty_ids), code)
            )
        flash("Тест опубликован для выбранных факультетов", "success")
    return redirect(url_for("admin_content"))


@app.route("/admin/social", methods=["GET", "POST"])
@login_required
@admin_required
def admin_social():
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            network = request.form.get("network", "instagram")
            url = request.form.get("url", "").strip()
            title = request.form.get("title", "").strip()
            ends_at = request.form.get("ends_at", "").strip()
            if url:
                with get_db() as conn:
                    try:
                        conn.execute(
                            "INSERT INTO social_links (network, title, url, is_promo, ends_at) VALUES (?, ?, ?, 1, ?)",
                            (network, title or network, url, ends_at)
                        )
                    except Exception:
                        conn.execute(
                            "INSERT INTO social_links (network, title, url, is_promo) VALUES (?, ?, ?, 1)",
                            (network, title or network, url)
                        )
                flash("Соцсеть добавлена", "success")
        elif action == "delete":
            sid = request.form.get("id")
            with get_db() as conn:
                conn.execute("DELETE FROM social_links WHERE id = ?", (sid,))
            flash("Удалено", "success")
        elif action == "broadcast":
            set_setting("promo_broadcast", "1")
            set_setting("promo_broadcast_at", now_tj().isoformat())
            set_setting("promo_auto_count", "0")
            flash("Реклама соцсетей запущена для студентов", "success")
        return redirect(url_for("admin_social"))
    with get_db() as conn:
        links = conn.execute("SELECT * FROM social_links ORDER BY sort_order, id").fetchall()
    return render_template("admin/social.html", links=links)



@app.route("/api/promo/status")
@login_required
def promo_status():
    """Реклама для студентов, которые НЕ на экзамене."""
    if current_user.is_admin:
        return jsonify({"show": False, "links": []})
    # во время теста не показываем
    if session.get("in_exam"):
        return jsonify({"show": False, "links": []})
    broadcast = get_setting("promo_broadcast", "0") == "1"
    if not broadcast:
        return jsonify({"show": False, "links": []})
    if session.get("promo_dismissed"):
        return jsonify({"show": False, "links": []})
    # авто-лимит: до 5 показов / 24ч на пользователя
    key = f"promo_shows_{current_user.id}"
    try:
        shows = int(get_setting(key, "0") or 0)
    except Exception:
        shows = 0
    if shows >= 5:
        return jsonify({"show": False, "links": []})
    links = []
    now = now_tj()
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, network, title, url, ends_at FROM social_links WHERE is_promo = 1 ORDER BY id"
            ).fetchall()
            for r in rows:
                ends = ""
                try:
                    ends = r["ends_at"] or ""
                except Exception:
                    ends = ""
                if ends:
                    try:
                        end_dt = datetime.fromisoformat(ends.replace("Z", ""))
                        if getattr(end_dt, "tzinfo", None) is None and now.tzinfo:
                            end_dt = end_dt.replace(tzinfo=now.tzinfo)
                        if now > end_dt:
                            conn.execute("DELETE FROM social_links WHERE id = ?", (r["id"],))
                            continue
                    except Exception:
                        pass
                links.append({
                    "id": r["id"],
                    "network": r["network"],
                    "title": r["title"] or r["network"],
                    "url": r["url"],
                })
    except Exception as e:
        print("promo_status:", e)
    if not links:
        return jsonify({"show": False, "links": []})
    set_setting(key, str(shows + 1))
    return jsonify({"show": True, "links": links})


@app.route("/api/promo/subscribe", methods=["POST"])
@login_required
def promo_subscribe():
    """Студент подтвердил подписку на все аккаунты."""
    session["promo_dismissed"] = True
    set_setting(f"promo_seen_{current_user.id}", "1")
    return jsonify({"ok": True})


@app.route("/api/promo/dismiss", methods=["POST"])
@login_required
def promo_dismiss():
    set_setting(f"promo_seen_{current_user.id}", "1")
    session["promo_dismissed"] = True
    return jsonify({"ok": True})


@app.route("/admin/test/<code>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_test_edit(code):
    with get_db() as conn:
        t = conn.execute("SELECT * FROM content_tests WHERE code = ?", (code,)).fetchone()
        if not t:
            flash("Тест не найден", "error")
            return redirect(url_for("admin_content"))
        if request.method == "POST":
            q_ru = request.form.get("q_ru", "").strip()
            opt_a = request.form.get("opt_a", "").strip()
            opt_b = request.form.get("opt_b", "").strip()
            opt_c = request.form.get("opt_c", "").strip()
            opt_d = request.form.get("opt_d", "").strip()
            correct = int(request.form.get("correct_index") or 0)
            # тип вопроса = тип теста (отдельные наборы вопросов)
            try:
                q_type = t["test_type"] or "mcq"
            except Exception:
                q_type = request.form.get("q_type", "mcq")
            if q_type not in ("mcq", "multi", "match"):
                q_type = "mcq"
            multi_raw = request.form.getlist("correct_multi")
            correct_multi = json.dumps([int(x) for x in multi_raw]) if multi_raw else ""
            # match: left A-D = right indices as JSON {"0":1,"1":0,...}
            match_json = request.form.get("match_json", "").strip()
            if q_type == "match" and not match_json:
                # auto from fields match_0 .. match_3
                mj = {}
                for i in range(4):
                    v = request.form.get(f"match_{i}", "").strip()
                    if v != "":
                        mj[str(i)] = int(v)
                match_json = json.dumps(mj)
            if not q_ru or not opt_a or not opt_b:
                flash("Нужны вопрос и минимум 2 варианта", "error")
            else:
                try:
                    conn.execute(
                        """INSERT INTO content_questions
                           (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index, q_type, correct_multi, match_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (t["id"], q_ru, q_ru, q_ru, opt_a, opt_b, opt_c, opt_d, correct, q_type, correct_multi, match_json),
                    )
                except Exception:
                    conn.execute(
                        """INSERT INTO content_questions
                           (test_id, q_ru, q_en, q_tg, opt_a, opt_b, opt_c, opt_d, correct_index)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (t["id"], q_ru, q_ru, q_ru, opt_a, opt_b, opt_c, opt_d, correct),
                    )
                flash("Вопрос добавлен", "success")
            return redirect(url_for("admin_test_edit", code=code))
        questions = conn.execute(
            "SELECT * FROM content_questions WHERE test_id = ? ORDER BY sort_order, id",
            (t["id"],),
        ).fetchall()
        faculties = conn.execute("SELECT * FROM faculties ORDER BY id").fetchall()
    return render_template(
        "admin/test_edit.html",
        test=t,
        questions=questions,
        faculties=faculties,
    )


@app.route("/admin/question/<int:qid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_question_delete(qid):
    with get_db() as conn:
        q = conn.execute("SELECT test_id FROM content_questions WHERE id = ?", (qid,)).fetchone()
        conn.execute("DELETE FROM content_questions WHERE id = ?", (qid,))
        code = "x"
        if q:
            t = conn.execute("SELECT code FROM content_tests WHERE id = ?", (q["test_id"],)).fetchone()
            if t:
                code = t["code"]
    flash("Вопрос удалён", "success")
    return redirect(url_for("admin_test_edit", code=code))


# ==================== FACE ID / УСТРОЙСТВО (работает и без HTTPS) ====================

@app.route("/api/face/register", methods=["POST"])
@app.route("/api/face/enroll", methods=["POST"])
@login_required
def face_enroll():
    """Привязка Face ID к уже зарегистрированному аккаунту (только из Настроек)."""
    data = request.get_json() or {}
    cred_id = (data.get("credential_id") or "").strip()
    if not cred_id or len(cred_id) < 8:
        cred_id = "face_" + uuid.uuid4().hex

    with get_db() as conn:
        # этот ключ уже у другого пользователя?
        exists = conn.execute(
            "SELECT user_id FROM webauthn_credentials WHERE credential_id = ?", (cred_id,)
        ).fetchone()
        if exists and int(exists[0]) != int(current_user.id):
            return jsonify({"error": "Этот Face ID уже привязан к другому аккаунту"}), 400

        # удалить старые ключи этого пользователя
        conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (current_user.id,))
        conn.execute(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, device_name) VALUES (?, ?, ?, ?)",
            (current_user.id, cred_id, data.get("public_key") or "face", (data.get("device_name") or "camera")[:120])
        )
        photo_b64 = data.get("photo")
        if photo_b64 and str(photo_b64).startswith("data:image"):
            try:
                import base64 as b64mod
                header, bdata = photo_b64.split(",", 1)
                ext = "jpg" if "jpeg" in header else "png"
                fname = f"face_{current_user.id}.{ext}"
                fpath = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
                with open(fpath, "wb") as f:
                    f.write(b64mod.b64decode(bdata))
            except Exception:
                pass

    return jsonify({"ok": True, "credential_id": cred_id})


@app.route("/api/face/status")
@login_required
def face_status():
    with get_db() as conn:
        row = conn.execute(
            "SELECT credential_id FROM webauthn_credentials WHERE user_id = ? LIMIT 1",
            (current_user.id,)
        ).fetchone()
    return jsonify({"enabled": bool(row), "credential_id": row[0] if row else None})


@app.route("/api/face/login", methods=["POST"])
def face_login():
    data = request.get_json() or {}
    cred_id = (data.get("credential_id") or "").strip()
    if not cred_id:
        return jsonify({"error": "Нет ключа устройства. Сначала зарегистрируйтесь на этом телефоне."}), 400

    with get_db() as conn:
        row = conn.execute(
            """SELECT u.* FROM webauthn_credentials w
               JOIN users u ON w.user_id = u.id
               WHERE w.credential_id = ?""",
            (cred_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Ключ не найден. Зарегистрируйтесь на этом устройстве."}), 404
        user = User(row)
        login_user(user)
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user.id))

    redirect = url_for("admin_dashboard") if user.is_admin else url_for("dashboard")
    return jsonify({"ok": True, "redirect": redirect})



@app.route("/admin/student/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_student_delete(uid):
    with get_db() as conn:
        u = conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if u and not u["is_admin"]:
            conn.execute("DELETE FROM test_results WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM pro_requests WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM notifications WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            flash("Студент удалён", "success")
        else:
            flash("Нельзя удалить", "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/student/add", methods=["POST"])
@login_required
@admin_required
def admin_student_add():
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "student123")
    if not name or not email:
        flash("ФИО и email обязательны", "error")
        return redirect(url_for("admin_users"))
    with get_db() as conn:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            flash("Email уже есть", "error")
            return redirect(url_for("admin_users"))
        conn.execute(
            "INSERT INTO users (full_name, email, password_hash, language, password_plain) VALUES (?, ?, ?, 'ru', ?)",
            (name, email, generate_password_hash(password, method="pbkdf2:sha256"), password)
        )
    flash("Студент добавлен", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/result/<int:rid>/edit", methods=["POST"])
@login_required
@admin_required
def admin_result_edit(rid):
    """Правка результата: админ меняет БУКВУ (оценку) → балл пересчитывается из 100."""
    grade = (request.form.get("grade") or "").strip()
    if grade not in GRADE_LETTERS:
        flash("Выберите оценку: A, B, C или D", "error")
        return redirect(request.referrer or url_for("admin_stats"))
    score = grade_to_score(grade)
    max_score = float(EXAM_QUESTION_COUNT * POINTS_CORRECT)  # 100
    # correct ≈ score/2, incorrect = 50 - correct
    correct = int(round(score / float(POINTS_CORRECT)))
    incorrect = max(0, EXAM_QUESTION_COUNT - correct)
    with get_db() as conn:
        conn.execute(
            "UPDATE test_results SET score=?, max_score=?, correct=?, incorrect=? WHERE id=?",
            (score, max_score, correct, incorrect, rid)
        )
    flash(f"Оценка изменена на {grade} (балл {score:.0f}/100)", "success")
    return redirect(request.referrer or url_for("admin_stats"))



@app.route("/admin/stats")
@login_required
@admin_required
def admin_stats():
    with get_db() as conn:
        by_test = conn.execute(
            """SELECT test_id,
                      COUNT(*) as attempts,
                      SUM(correct) as sum_ok,
                      SUM(incorrect) as sum_bad,
                      AVG(score * 1.0 / CASE WHEN max_score=0 THEN 1 ELSE max_score END) as avg_pct
               FROM test_results GROUP BY test_id"""
        ).fetchall()
        recent = conn.execute(
            """SELECT tr.*, u.full_name, u.email FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.created_at DESC LIMIT 40"""
        ).fetchall()
    recent_e = []
    for r in recent:
        pct = score_percent(r["score"], r["max_score"])
        recent_e.append({
            "id": r["id"],
            "full_name": r["full_name"],
            "email": r["email"],
            "test_id": r["test_id"],
            "score": float(r["score"] or 0),
            "max_score": float(r["max_score"] or 100),
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "created_at": r["created_at"],
            "grade": letter_grade(pct),
        })
    return render_template("admin/stats.html", by_test=by_test, recent=recent_e, tests=load_all_tests(), letter_grade=letter_grade)



@app.route("/admin/payment", methods=["GET", "POST"])
@login_required
@admin_required
def admin_payment():
    if request.method == "POST":
        set_payment_settings({
            "dc": request.form.get("dc", ""),
            "eskhata": request.form.get("eskhata", ""),
            "alif": request.form.get("alif", ""),
            "holder": request.form.get("holder", ""),
            "phone": request.form.get("phone", ""),
        })
        # лимит попыток экзамена фиксирован = 1, настройка убрана
        set_setting("backup_webhook", request.form.get("backup_webhook", ""))
        flash("Настройки сохранены", "success")
        return redirect(url_for("admin_payment"))
    return render_template(
        "admin/payment.html",
        card=get_payment_settings(),
        max_exam_attempts=get_setting("max_exam_attempts", "1"),
        backup_webhook=get_setting("backup_webhook", ""),
    )


@app.route("/admin/backup")
@login_required
@admin_required
def admin_backup():
    """Скачать копию БД. Дополнительно копирует в BACKUP_DIR / шлёт на BACKUP_WEBHOOK_URL."""
    import shutil
    if not os.path.exists(DB_PATH):
        flash("База ещё не создана", "error")
        return redirect(url_for("admin_dashboard"))
    # внешняя копия (Render disk / локальная папка)
    backup_dir = os.environ.get("BACKUP_DIR") or get_setting("backup_dir", "")
    if backup_dir:
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(backup_dir, f"hgu_test_{stamp}.db")
            shutil.copy2(DB_PATH, dest)
        except Exception as e:
            app.logger.info("backup copy: %s", e)
    webhook = os.environ.get("BACKUP_WEBHOOK_URL") or get_setting("backup_webhook", "")
    if webhook:
        try:
            import urllib.request
            with open(DB_PATH, "rb") as f:
                data = f.read()
            req = urllib.request.Request(webhook, data=data, method="POST")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("X-HGU-Backup", "1")
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            app.logger.info("backup webhook: %s", e)
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=f"hgu_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
    )



@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "ДОНОЯК TJ",
        "short_name": "ДОНОЯК TJ",
        "description": "ДОНОЯК TJ — онлайн-тесты и подготовка",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a5f7a",
        "theme_color": "#1a5f7a",
        "lang": "ru",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    })


@app.route("/sw.js")
def service_worker():
    js = """
const CACHE = 'hgu-v3';
const ASSETS = ['/', '/static/css/style.css', '/static/icon-192.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => { try { c.put(e.request, copy); } catch(err){} }).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
self.addEventListener('push', event => {
  let data = {title: 'ДОНОЯК TJ', body: 'Новое уведомление', url: '/'};
  try { if (event.data) data = Object.assign(data, event.data.json()); } catch(e) {}
  event.waitUntil(self.registration.showNotification(data.title || 'ДОНОЯК TJ', {
    body: data.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: {url: data.url || '/'},
    requireInteraction: true,
    vibrate: [200, 100, 200],
    silent: false
  }));
});
self.addEventListener('notificationclick'
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    if _is_cloud_host() and not DATABASE_URL:
        flash("БД: SQLite (временно). Добавьте PostgreSQL (DATABASE_URL), иначе данные сотрутся при перезапуске.", "error")

    total_users = pro_users = total_tests = pending_requests = 0
    recent_users = pending = recent_results = notifs = weak = []
    ai_insights = []
    avg_pct = 0.0

    try:
        with get_db() as conn:
            def _cnt(sql, params=None):
                row = conn.execute(sql, params).fetchone() if params is not None else conn.execute(sql).fetchone()
                if not row:
                    return 0
                try:
                    return int(row[0] or 0)
                except Exception:
                    return 0

            total_users = _cnt("SELECT COUNT(*) FROM users WHERE is_admin = 0")
            pro_users = _cnt("SELECT COUNT(*) FROM users WHERE is_pro = 1 AND is_admin = 0")
            total_tests = _cnt("SELECT COUNT(*) FROM test_results")
            pending_requests = _cnt("SELECT COUNT(*) FROM pro_requests WHERE status = 'pending'")

            recent_users = conn.execute(
                "SELECT id, full_name, email, is_pro, pro_until, created_at, last_login FROM users WHERE is_admin = 0 ORDER BY created_at DESC LIMIT 20"
            ).fetchall()

            pending = conn.execute(
                """SELECT pr.*, u.full_name, u.email
                   FROM pro_requests pr
                   JOIN users u ON pr.user_id = u.id
                   WHERE pr.status = 'pending'
                   ORDER BY pr.created_at DESC"""
            ).fetchall()

            recent_results = conn.execute(
                """SELECT tr.*, u.full_name
                   FROM test_results tr
                   JOIN users u ON tr.user_id = u.id
                   ORDER BY tr.created_at DESC LIMIT 15"""
            ).fetchall()

            notifs = conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC",
                (current_user.id,)
            ).fetchall()

            # Аналитика — SQL совместим с PostgreSQL и SQLite
            try:
                avg_row = conn.execute(
                    "SELECT AVG(score * 1.0 / CASE WHEN max_score = 0 THEN 1 ELSE max_score END) FROM test_results"
                ).fetchone()
                avg_pct = round(float(avg_row[0] or 0) * 100, 1)
            except Exception:
                avg_pct = 0.0

            try:
                weak = conn.execute(
                    """SELECT u.full_name, u.email,
                              AVG(tr.score * 1.0 / CASE WHEN tr.max_score = 0 THEN 1 ELSE tr.max_score END) AS pct,
                              COUNT(tr.id) AS cnt
                       FROM test_results tr
                       JOIN users u ON tr.user_id = u.id
                       WHERE u.is_admin = 0
                       GROUP BY tr.user_id, u.full_name, u.email
                       HAVING AVG(tr.score * 1.0 / CASE WHEN tr.max_score = 0 THEN 1 ELSE tr.max_score END) < 0.5
                          AND COUNT(tr.id) >= 1
                       ORDER BY pct ASC
                       LIMIT 8"""
                ).fetchall()
            except Exception as _we:
                print("admin weak query:", _we)
                weak = []

            try:
                by_test = conn.execute(
                    """SELECT test_id, COUNT(*) AS cnt,
                              AVG(score * 1.0 / CASE WHEN max_score = 0 THEN 1 ELSE max_score END) AS pct
                       FROM test_results
                       GROUP BY test_id
                       ORDER BY cnt DESC"""
                ).fetchall()
            except Exception as _be:
                print("admin by_test query:", _be)
                by_test = []

            if total_tests == 0:
                ai_insights.append("Пока мало данных. Когда студенты начнут проходить тесты, здесь появятся рекомендации.")
            else:
                ai_insights.append(f"Средний результат по всем тестам: {avg_pct}%.")
                if avg_pct < 50:
                    ai_insights.append("Общий уровень подготовки низкий — имеет смысл добавить больше тренировочных тестов и напоминания.")
                elif avg_pct >= 70:
                    ai_insights.append("Уровень подготовки хороший. Можно усложнить Pro-тесты.")
                for t in by_test[:5]:
                    try:
                        tid = t["test_id"]
                        title = (get_test(tid) or {}).get("title", {}).get("ru", tid)
                        pct = round(float(t["pct"] or 0) * 100, 1)
                        cnt = int(t["cnt"] or 0)
                        if pct < 45:
                            ai_insights.append(f"Слабое место: «{title}» (средний {pct}%, прохождений {cnt}). Рекомендуется усилить вопросы или добавить разбор.")
                        elif cnt >= 3:
                            ai_insights.append(f"Популярный тест: «{title}» — {cnt} прохождений, средний {pct}%.")
                    except Exception:
                        pass
                if weak:
                    ai_insights.append(f"Студентов с результатом ниже 50%: {len(weak)}. Имеет смысл отправить им уведомление с советом пройти тренировку.")
                if pending_requests:
                    ai_insights.append(f"Ожидают оплаты Pro: {pending_requests}. Проверьте заявки.")
    except Exception as e:
        print("admin_dashboard error:", e)
        import traceback
        traceback.print_exc()
        flash(f"Ошибка загрузки админки: {e}", "error")

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        pro_users=pro_users,
        total_tests=total_tests,
        pending_requests=pending_requests,
        recent_users=recent_users,
        pending=pending,
        recent_results=recent_results,
        notifs=notifs,
        tests=load_all_tests(),
        faculties=load_faculties_map(),
        ai_insights=ai_insights,
        weak_students=weak,
        avg_pct=avg_pct
    )


@app.route("/admin/pro/<int:req_id>/<action>", methods=["POST"])
@login_required
@admin_required
def admin_pro_action(req_id, action):
    with get_db() as conn:
        req = conn.execute("SELECT * FROM pro_requests WHERE id = ?", (req_id,)).fetchone()
        if not req:
            flash("Заявка не найдена", "error")
            return redirect(url_for("admin_dashboard"))

        if action == "approve":
            days = PRO_DURATION_DAYS
            package = "2m"
            try:
                if req["duration_days"]:
                    days = int(req["duration_days"])
            except Exception:
                pass
            try:
                package = req["package"] or "2m"
            except Exception:
                package = "2m"
            hints = int(PRO_PACKAGES.get(package, {}).get("hints", 5))
            until = (datetime.now() + timedelta(days=days)).isoformat()
            conn.execute(
                "UPDATE users SET is_pro = 1, pro_until = ?, hints_left = ? WHERE id = ?",
                (until, hints, req["user_id"])
            )
            conn.execute(
                "UPDATE pro_requests SET status = 'approved', processed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), req_id)
            )
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (req["user_id"], "Pro одобрен",
                 f"Pro на {days} дн. Подсказки: {hints}. Спасибо за оплату!")
            )
            flash(f"Pro одобрен (+{hints} подсказок)", "success")

        elif action == "reject":
            note = request.form.get("note", "Оплата не подтверждена")
            conn.execute(
                "UPDATE pro_requests SET status = 'rejected', admin_note = ?, processed_at = ? WHERE id = ?",
                (note, datetime.now().isoformat(), req_id)
            )
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (req["user_id"], "Заявка отклонена", f"Ваша заявка на Pro отклонена. Причина: {note}")
            )
            flash("Заявка отклонена", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/notify", methods=["POST"])
@login_required
@admin_required
def admin_notify():
    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    target = request.form.get("target", "all")  # all / pro / free

    if not title or not message:
        flash("Заполните заголовок и текст", "error")
        return redirect(url_for("admin_dashboard"))

    with get_db() as conn:
        if target == "all":
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0").fetchall()
        elif target == "pro":
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0 AND is_pro = 1").fetchall()
        else:
            users = conn.execute("SELECT id FROM users WHERE is_admin = 0 AND is_pro = 0").fetchall()

        for u in users:
            conn.execute(
                "INSERT INTO notifications (user_id, title, message) VALUES (?, ?, ?)",
                (u["id"], title, message)
            )

        # Также глобальное
        conn.execute(
            "INSERT INTO global_notifications (title, message) VALUES (?, ?)",
            (title, message)
        )

    flash(f"Уведомление отправлено {len(users)} пользователям", "success")
    return redirect(url_for("admin_dashboard"))



@app.route("/api/presence", methods=["POST"])
@login_required
def api_presence():
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO user_presence (user_id, last_seen) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen""",
                (current_user.id, datetime.now().isoformat())
            )
        except Exception:
            try:
                conn.execute("DELETE FROM user_presence WHERE user_id = ?", (current_user.id,))
                conn.execute(
                    "INSERT INTO user_presence (user_id, last_seen) VALUES (?, ?)",
                    (current_user.id, datetime.now().isoformat())
                )
            except Exception as e:
                print("presence:", e)
    return jsonify({"ok": True})


@app.route("/api/chat/send", methods=["POST"])
@login_required
def chat_send():
    """Текст или медиа (JSON text / multipart file)."""
    text = ""
    msg_type = "text"
    media_path = ""
    receiver_id = 0

    if request.content_type and "multipart/form-data" in (request.content_type or ""):
        text = (request.form.get("message") or "").strip()
        msg_type = (request.form.get("msg_type") or "file").strip() or "file"
        try:
            receiver_id = int(request.form.get("receiver_id") or 0)
        except Exception:
            receiver_id = 0
        f = request.files.get("file")
        if f and f.filename:
            from werkzeug.utils import secure_filename
            import uuid
            ext = (f.filename.rsplit(".", 1)[-1] if "." in f.filename else "bin").lower()
            if ext not in app.config["ALLOWED_EXTENSIONS"]:
                return jsonify({"error": "bad_file"}), 400
            if msg_type not in ("image", "voice", "file", "camera"):
                if ext in ("png", "jpg", "jpeg", "gif", "webp"):
                    msg_type = "image"
                elif ext in ("mp3", "ogg", "webm", "wav", "m4a"):
                    msg_type = "voice"
                else:
                    msg_type = "file"
            fname = f"chat_{current_user.id}_{uuid.uuid4().hex[:12]}.{ext}"
            dest = os.path.join(app.config["UPLOAD_FOLDER"], fname)
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            f.save(dest)
            media_path = fname
            if not text:
                text = {"image": "[фото]", "voice": "[голос]", "camera": "[фото]", "file": f"[файл: {f.filename}]"}.get(msg_type, "[файл]")
    else:
        data = request.get_json(silent=True) or {}
        text = (data.get("message") or "").strip()
        msg_type = (data.get("msg_type") or "text").strip() or "text"
        media_path = (data.get("media_path") or "").strip()
        try:
            receiver_id = int(data.get("receiver_id") or 0)
        except Exception:
            receiver_id = 0

    if not text and not media_path:
        return jsonify({"error": "empty"}), 400

    with get_db() as conn:
        if current_user.is_admin:
            rid = receiver_id
            if not rid:
                return jsonify({"error": "no receiver"}), 400
        else:
            admin = conn.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            if not admin:
                return jsonify({"error": "no admin"}), 400
            try:
                rid = admin["id"]
            except Exception:
                rid = list(admin)[0] if admin else 0
        try:
            conn.execute(
                "INSERT INTO chat_messages (sender_id, receiver_id, message, created_at, msg_type, media_path) VALUES (?, ?, ?, ?, ?, ?)",
                (current_user.id, rid, text[:2000], datetime.now().isoformat(), msg_type, media_path)
            )
        except Exception:
            conn.execute(
                "INSERT INTO chat_messages (sender_id, receiver_id, message, created_at) VALUES (?, ?, ?, ?)",
                (current_user.id, rid, text[:2000], datetime.now().isoformat())
            )
    return jsonify({"ok": True})



@app.route("/api/chat/messages")
@login_required
def chat_messages():
    other = request.args.get("user_id", type=int)
    with get_db() as conn:
        if current_user.is_admin:
            if not other:
                return jsonify({"messages": []})
            rows = conn.execute(
                """SELECT * FROM chat_messages
                   WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
                   ORDER BY id ASC LIMIT 200""",
                (current_user.id, other, other, current_user.id)
            ).fetchall()
        else:
            admin = conn.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            aid = admin["id"] if admin else 0
            try:
                aid = admin["id"]
            except Exception:
                aid = 0
            rows = conn.execute(
                """SELECT * FROM chat_messages
                   WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
                   ORDER BY id ASC LIMIT 200""",
                (current_user.id, aid, aid, current_user.id)
            ).fetchall()
        out = []
        for r in rows:
            mt = "text"
            mp = ""
            try:
                mt = r["msg_type"] or "text"
            except Exception:
                mt = "text"
            try:
                mp = r["media_path"] or ""
            except Exception:
                mp = ""
            out.append({
                "id": r["id"],
                "sender_id": r["sender_id"],
                "receiver_id": r["receiver_id"],
                "message": r["message"],
                "created_at": r["created_at"],
                "mine": int(r["sender_id"]) == int(current_user.id),
                "msg_type": mt,
                "media_path": mp,
                "media_url": ("/uploads/" + mp) if mp else "",
            })
    return jsonify({"messages": out})


@app.route("/chat")
@login_required
def chat_page():
    online = {}
    students = []
    with get_db() as conn:
        if current_user.is_admin:
            students = conn.execute(
                "SELECT id, full_name, email FROM users WHERE is_admin = 0 ORDER BY full_name"
            ).fetchall()
            try:
                pres = conn.execute("SELECT user_id, last_seen FROM user_presence").fetchall()
                from datetime import datetime as dt
                now = datetime.now()
                for p in pres:
                    try:
                        ls = datetime.fromisoformat(str(p["last_seen"]).replace("Z", ""))
                        online[int(p["user_id"])] = (now - ls).total_seconds() < 120
                    except Exception:
                        online[int(p["user_id"])] = False
            except Exception:
                pass
        else:
            admin = conn.execute("SELECT id, full_name FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            students = [admin] if admin else []
    return render_template(
        "chat.html",
        lang=getattr(current_user, "language", "ru"),
        students=students,
        online=online,
        is_admin=current_user.is_admin,
    )


@app.route("/admin/export/results.pdf")
@login_required
@admin_required
def admin_export_results_pdf():
    """Простой PDF/текстовый отчёт результатов (без внешних библиотек)."""
    import io
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tr.id, u.full_name, u.email, tr.test_id, tr.score, tr.max_score,
                      tr.correct, tr.incorrect, tr.duration_seconds, tr.created_at
               FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.id ASC"""
        ).fetchall()
    lines = ["ДОНОЯК TJ — Результаты экзаменов", "=" * 50, ""]
    for i, r in enumerate(rows, 1):
        pct = score_percent(r["score"], r["max_score"])
        g = letter_grade(pct)
        lines.append(
            f"{i}. {r['full_name']} | {r['test_id']} | {g} | "
            f"{float(r['score'] or 0):.0f}/{float(r['max_score'] or 100):.0f} | "
            f"верно {r['correct']} | неверно {r['incorrect']} | "
            f"{r['duration_seconds']}с | {format_dt(r['created_at'])}"
        )
    text = "\n".join(lines)
    # minimal PDF
    def escape(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 40 800 Td\n"
    y = 0
    for line in lines[:80]:
        content += f"0 -14 Td ({escape(line[:90])}) Tj\n"
    content += "ET"
    stream = content.encode("latin-1", errors="replace")
    pdf = io.BytesIO()
    objs = []
    objs.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objs.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objs.append(b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n")
    objs.append(f"4 0 obj<< /Length {len(stream)} >>stream\n".encode() + stream + b"\nendstream endobj\n")
    objs.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")
    pdf.write(b"%PDF-1.4\n")
    offsets = [0]
    for o in objs:
        offsets.append(pdf.tell())
        pdf.write(o)
    xref = pdf.tell()
    pdf.write(f"xref\n0 {len(objs)+1}\n".encode())
    pdf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.write(f"{off:010d} 00000 n \n".encode())
    pdf.write(f"trailer<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    pdf.seek(0)
    return send_file(pdf, as_attachment=True, download_name="donoyak_results.pdf", mimetype="application/pdf")



@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    with get_db() as conn:
        users = conn.execute(
            "SELECT * FROM users WHERE is_admin = 0 ORDER BY created_at DESC"
        ).fetchall()
    return render_template("admin/users.html", users=users, tests=load_all_tests())



@app.route("/admin/export/results.xlsx")
@login_required
@admin_required
def admin_export_results():
    """Экспорт всех результатов в Excel (или CSV, если openpyxl нет)."""
    import io
    with get_db() as conn:
        rows = conn.execute(
            """SELECT tr.id, u.full_name, u.email, tr.test_id, tr.score, tr.max_score,
                      tr.correct, tr.incorrect, tr.mode, tr.duration_seconds, tr.created_at
               FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               ORDER BY tr.created_at DESC"""
        ).fetchall()
    headers = ["ID", "ФИО", "Email", "Тест", "Балл", "Макс", "Верно", "Неверно", "Режим", "Сек", "Дата"]
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Результаты"
        ws.append(headers)
        for r in rows:
            ws.append([r["id"], r["full_name"], r["email"], r["test_id"], r["score"], r["max_score"],
                       r["correct"], r["incorrect"], r["mode"] or "exam", r["duration_seconds"], r["created_at"]])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="donoyak_results.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception:
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        for r in rows:
            w.writerow([r["id"], r["full_name"], r["email"], r["test_id"], r["score"], r["max_score"],
                        r["correct"], r["incorrect"], r["mode"] or "exam", r["duration_seconds"], r["created_at"]])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(data, as_attachment=True, download_name="donoyak_results.csv", mimetype="text/csv")


@app.route("/admin/results")
@login_required
@admin_required
def admin_results():
    with get_db() as conn:
        results = conn.execute(
            """SELECT tr.*, u.full_name, u.email
               FROM test_results tr
               JOIN users u ON tr.user_id = u.id
               WHERE (tr.mode IS NULL OR tr.mode = 'exam')
               ORDER BY tr.created_at ASC"""
        ).fetchall()
    enriched = []
    for i, r in enumerate(results, 1):
        pct = score_percent(r["score"], r["max_score"])
        enriched.append({
            "n": i,
            "id": r["id"],
            "full_name": r["full_name"],
            "email": r["email"],
            "test_id": r["test_id"],
            "score": float(r["score"] or 0),
            "max_score": float(r["max_score"] or 100),
            "correct": r["correct"],
            "incorrect": r["incorrect"],
            "duration_seconds": r["duration_seconds"],
            "created_at": r["created_at"],
            "_grade": letter_grade(pct),
            "_dt": format_dt(r["created_at"], "Asia/Dushanbe"),
        })
    return render_template("admin/results.html", results=enriched, tests=load_all_tests())


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    if not current_user.is_admin:
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/api/mark_read/<int:notif_id>", methods=["POST"])
@login_required
def mark_read(notif_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notif_id, current_user.id)
        )
    return jsonify({"ok": True})


# ==================== ЗАПУСК ====================

print("=" * 50)
print("DONOYAK TJ starting")
print("DATABASE:", "PostgreSQL" if DATABASE_URL else "SQLite (temporary on cloud!)")
if DATABASE_URL:
    print("DATABASE_URL: set (hidden)")
else:
    print("WARNING: set DATABASE_URL for permanent storage")
print("CLOUD:", "yes" if _is_cloud_host() else "no (local)")
print("=" * 50)
try:
    init_db()
    print("init_db: OK")
except Exception as _init_err:
    print("CRITICAL init_db error:", _init_err)
    import traceback
    traceback.print_exc()
    try:
        init_db()
        print("init_db: OK (retry)")
    except Exception as _e2:
        print("init_db second fail:", _e2)


if __name__ == "__main__":
    _port = int(os.environ.get("PORT", "5000"))
    print("=" * 50)
    print("ДОНОЯК TJ - сервер запущен")
    print("Админ: admin@dnk.tj / rm2006$")
    print(f"Откройте: http://127.0.0.1:{_port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=_port, debug=not _is_cloud_host())
