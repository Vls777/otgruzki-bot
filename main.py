"""
VK Warehouse Bot — единый файл.
Все настройки — в блоке НАСТРОЙКИ в начале файла.
"""
import io
import csv
import json
import time
import shutil
import sqlite3
import tempfile
import os
import traceback
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
plt.rcParams['font.family'] = 'DejaVu Sans'


# ════════════════════════════════════════════════════════════
#                        НАСТРОЙКИ
# ════════════════════════════════════════════════════════════

TOKEN = os.getenv('VK_TOKEN', '').strip()
GROUP_ID = int(os.getenv('VK_GROUP_ID', '0'))

ADMIN_IDS = []

TRUCK_CAPACITY = 19.0

TIMEZONE_OFFSET_HOURS = 5

MORNING_REPORT_HOUR = 8
MORNING_REPORT_MINUTE = 0

BACKUP_HOUR = 23
BACKUP_MINUTE = 0

BACKUP_KEEP = 30

DATA_DIR = '/app/data'
DB_PATH = os.path.join(DATA_DIR, 'shipments.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')


# ════════════════════════════════════════════════════════════
#                       ЧАСОВОЙ ПОЯС
# ════════════════════════════════════════════════════════════

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


def tz_now():
    return datetime.now(TZ)


def tz_today():
    return tz_now().date()


def now_iso():
    return tz_now().strftime('%Y-%m-%d %H:%M:%S')


# ════════════════════════════════════════════════════════════
#                      СОСТОЯНИЯ ДИАЛОГА
# ════════════════════════════════════════════════════════════

_states = {}


def set_state(user_id, **kw):
    _states[user_id] = kw


def update_state(user_id, **kw):
    s = _states.setdefault(user_id, {})
    s.update(kw)
    return s


def get_state(user_id):
    return dict(_states.get(user_id, {}))


def clear_state(user_id):
    _states.pop(user_id, None)


# ════════════════════════════════════════════════════════════
#                        БАЗА ДАННЫХ
# ════════════════════════════════════════════════════════════

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shipments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name   TEXT NOT NULL,
                tonnage       REAL NOT NULL,
                trucks        REAL NOT NULL,
                shipment_date TEXT NOT NULL,
                comment       TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                created_by    INTEGER,
                updated_at    TEXT,
                updated_by    INTEGER
            );
            CREATE TABLE IF NOT EXISTS clients (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                last_used TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                first_name     TEXT,
                role           TEXT DEFAULT 'manager',
                morning_report INTEGER DEFAULT 1,
                last_seen      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ship_date ON shipments(shipment_date);
            CREATE INDEX IF NOT EXISTS idx_ship_client ON shipments(client_name);
        """)


def calc_trucks(tonnage):
    if tonnage <= 0:
        return 0.0
    return round(float(tonnage) / TRUCK_CAPACITY, 2)


def upsert_user(user_id, first_name=None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO users (user_id, first_name, last_seen)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 last_seen = excluded.last_seen,
                 first_name = COALESCE(excluded.first_name, users.first_name)""",
            (user_id, first_name, now_iso()))


def get_all_users():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def toggle_morning_report(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT morning_report FROM users WHERE user_id = ?",
                           (user_id,)).fetchone()
        if not row:
            return 1
        new_val = 0 if row['morning_report'] else 1
        conn.execute("UPDATE users SET morning_report = ? WHERE user_id = ?",
                     (new_val, user_id))
        return new_val


def get_clients(limit=200):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM clients ORDER BY last_used DESC, name ASC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_client(client_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def touch_client(name, conn=None):
    """Обновляет last_used. Если передан conn — использует его."""
    sql = """INSERT INTO clients (name, last_used) VALUES (?, ?)
             ON CONFLICT(name) DO UPDATE SET last_used = excluded.last_used"""
    params = (name.strip(), now_iso())
    if conn is not None:
        conn.execute(sql, params)
    else:
        with get_db() as c:
            c.execute(sql, params)


def add_shipment(client_name, tonnage, shipment_date, user_id, comment=''):
    trucks = calc_trucks(tonnage)
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO shipments
               (client_name, tonnage, trucks, shipment_date, comment, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_name.strip(), float(tonnage), trucks,
             shipment_date, comment, now_iso(), user_id))
        touch_client(client_name, conn=conn)
        return cur.lastrowid


def get_shipment(shipment_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE id = ?",
                           (shipment_id,)).fetchone()
        return dict(row) if row else None


def update_shipment(shipment_id, client_name=None, tonnage=None,
                    shipment_date=None, comment=None, user_id=None):
    fields, values = [], []
    touched_name = None
    if client_name is not None:
        fields.append("client_name = ?"); values.append(client_name.strip())
        touched_name = client_name.strip()
    if tonnage is not None:
        fields += ["tonnage = ?", "trucks = ?"]
        values += [float(tonnage), calc_trucks(float(tonnage))]
    if shipment_date is not None:
        fields.append("shipment_date = ?"); values.append(shipment_date)
    if comment is not None:
        fields.append("comment = ?"); values.append(comment)
    if not fields:
        return
    fields += ["updated_at = ?", "updated_by = ?"]
    values += [now_iso(), user_id]
    values.append(shipment_id)
    with get_db() as conn:
        conn.execute(f"UPDATE shipments SET {', '.join(fields)} WHERE id = ?", values)
        if touched_name:
            touch_client(touched_name, conn=conn)


def delete_shipment(shipment_id):
    with get_db() as conn:
        conn.execute("DELETE FROM shipments WHERE id = ?", (shipment_id,))


def get_shipments_by_date(d):
    if isinstance(d, date):
        d = d.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shipments WHERE shipment_date = ? ORDER BY id", (d,)).fetchall()
        return [dict(r) for r in rows]


def get_shipments_by_period(start, end):
    if isinstance(start, date):
        start = start.strftime("%Y-%m-%d")
    if isinstance(end, date):
        end = end.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM shipments WHERE shipment_date BETWEEN ? AND ? "
            "ORDER BY shipment_date, id", (start, end)).fetchall()
        return [dict(r) for r in rows]


def get_shipments_by_client(client_name, limit=1000):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM shipments WHERE client_name = ? COLLATE NOCASE
               ORDER BY shipment_date DESC, id DESC LIMIT ?""",
            (client_name, limit)).fetchall()
        return [dict(r) for r in rows]


def stats_for_period(start, end):
    shipments = get_shipments_by_period(start, end)
    by_client, by_day = {}, {}
    total_t = 0.0
    for s in shipments:
        total_t += s['tonnage']
        c = by_client.setdefault(s['client_name'], {'tonnage': 0.0})
        c['tonnage'] += s['tonnage']
        d = by_day.setdefault(s['shipment_date'], {'tonnage': 0.0})
        d['tonnage'] += s['tonnage']

    total_trucks = round(total_t / TRUCK_CAPACITY, 2)
    for c in by_client.values():
        c['trucks'] = round(c['tonnage'] / TRUCK_CAPACITY, 2)
    for d in by_day.values():
        d['trucks'] = round(d['tonnage'] / TRUCK_CAPACITY, 2)

    return {
        'count': len(shipments),
        'total_tonnage': total_t,
        'total_trucks': total_trucks,
        'by_client': by_client,
        'by_day': by_day,
    }


def client_summary(name):
    shipments = get_shipments_by_client(name, limit=10000)
    if not shipments:
        return None
    total_t = sum(s['tonnage'] for s in shipments)
    return {
        'name': name,
        'count': len(shipments),
        'total_tonnage': total_t,
        'total_trucks': round(total_t / TRUCK_CAPACITY, 2),
        'avg_tonnage': total_t / len(shipments),
        'last_date': shipments[0]['shipment_date'],
        'first_date': shipments[-1]['shipment_date'],
    }


def week_comparison():
    today = tz_today()
    this_start = today - timedelta(days=today.weekday())
    this_end = today
    last_start = this_start - timedelta(days=7)
    last_end = this_start - timedelta(days=1)
    return {
        'this': stats_for_period(this_start, this_end),
        'last': stats_for_period(last_start, last_end),
        'this_range': (this_start, this_end),
        'last_range': (last_start, last_end),
    }


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(DB_PATH):
        return None
    stamp = tz_now().strftime('%Y-%m-%d_%H-%M-%S')
    dst = os.path.join(BACKUP_DIR, f'shipments_{stamp}.db')
    shutil.copy2(DB_PATH, dst)

    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('shipments_'))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return dst


def get_db_bytes():
    with open(DB_PATH, 'rb') as f:
        return f.read()


# ════════════════════════════════════════════════════════════
#                        КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════════

def _p(**kw):
    return json.dumps(kw, ensure_ascii=False)


def main_menu(is_admin=False, morning_on=True):
    kb = VkKeyboard(one_time=False)
    kb.add_button('➕ Добавить погрузку', color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button('📋 Сегодня', color=VkKeyboardColor.PRIMARY)
    kb.add_button('📋 Завтра', color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button('📅 Другая дата', color=VkKeyboardColor.SECONDARY)
    kb.add_button('📊 Статистика', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button('📁 Клиенты', color=VkKeyboardColor.SECONDARY)
    kb.add_button('❓ Помощь', color=VkKeyboardColor.SECONDARY)
    kb.add_line()
    label = '🔔 Сводка: вкл' if morning_on else '🔕 Сводка: выкл'
    kb.add_button(label, color=VkKeyboardColor.SECONDARY)
    if is_admin:
        kb.add_button('⚙️ Настройки', color=VkKeyboardColor.SECONDARY)
    return kb.get_keyboard()


def date_choice_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('📅 Сегодня', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='pick_date', date='today'))
    kb.add_button('📅 Завтра', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='pick_date', date='tomorrow'))
    kb.add_line()
    kb.add_button('📅 Другая дата', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='pick_date', date='custom'))
    kb.add_line()
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def clients_menu(clients, action='add', sid=None, page=0, per_page=8):
    kb = VkKeyboard(inline=True)
    start = page * per_page
    chunk = clients[start:start + per_page]
    for i, c in enumerate(chunk):
        if action == 'view':
            p = {'cmd': 'open_client', 'cid': c['id']}
        else:
            p = {'cmd': 'pick_client', 'action': action, 'cid': c['id']}
            if sid is not None:
                p['sid'] = sid
        kb.add_button(c['name'][:40], color=VkKeyboardColor.SECONDARY,
                      payload=json.dumps(p, ensure_ascii=False))
        if i % 2 == 1 and i != len(chunk) - 1:
            kb.add_line()

    nav = []
    if page > 0:
        p = {'cmd': 'clients_page', 'action': action, 'page': page - 1}
        if sid is not None: p['sid'] = sid
        nav.append(('⬅️', json.dumps(p, ensure_ascii=False)))
    if start + per_page < len(clients):
        p = {'cmd': 'clients_page', 'action': action, 'page': page + 1}
        if sid is not None: p['sid'] = sid
        nav.append(('➡️', json.dumps(p, ensure_ascii=False)))
    if nav:
        kb.add_line()
        for label, pl in nav:
            kb.add_button(label, color=VkKeyboardColor.SECONDARY, payload=pl)

    kb.add_line()
    if action in ('add', 'edit_client'):
        p = {'cmd': 'new_client', 'action': action}
        if sid is not None: p['sid'] = sid
        kb.add_button('➕ Новый клиент', color=VkKeyboardColor.POSITIVE,
                      payload=json.dumps(p, ensure_ascii=False))
    elif action == 'view':
        kb.add_button('📊 Сводка по всем', color=VkKeyboardColor.PRIMARY,
                      payload=_p(cmd='clients_summary'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def client_card_menu(cid):
    kb = VkKeyboard(inline=True)
    kb.add_button('📜 Все отгрузки', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='client_history', cid=cid))
    kb.add_button('📈 График', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='client_chart', cid=cid))
    kb.add_line()
    kb.add_button('⬅️ К списку', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='clients_page', action='view', page=0))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def tonnage_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('19 т', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='pick_tonnage', t=19))
    kb.add_button('✏️ Другое', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='pick_tonnage', t='custom'))
    kb.add_line()
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE,
                  payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def confirm_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('✅ Сохранить', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='save_shipment'))
    kb.add_button('💬 Коммент.', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='add_comment'))
    kb.add_line()
    kb.add_button('✏️ Заново', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='restart_add'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def after_save_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('➕ Ещё погрузку', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='add_again'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def stats_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('Сегодня', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='stats', period='today'))
    kb.add_button('Вчера', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='stats', period='yesterday'))
    kb.add_line()
    kb.add_button('Эта неделя', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='week'))
    kb.add_button('Прошлая неделя', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='last_week'))
    kb.add_line()
    kb.add_button('Этот месяц', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='month'))
    kb.add_button('Прошлый месяц', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='last_month'))
    kb.add_line()
    kb.add_button('Этот год', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='stats', period='year'))
    kb.add_button('📅 Произвольно', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='stats', period='custom'))
    kb.add_line()
    kb.add_button('⚖️ Сравнить недели', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='week_compare'))
    kb.add_line()
    kb.add_button('📈 График: 7 дней', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='chart', period='week'))
    kb.add_button('📈 График: месяц', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='chart', period='month'))
    kb.add_line()
    kb.add_button('📤 Экспорт CSV (30 дн.)', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='export_csv'))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def shipment_item_menu(sid, allow_delete=True):
    kb = VkKeyboard(inline=True)
    kb.add_button('✏️ Изменить', color=VkKeyboardColor.PRIMARY, payload=_p(cmd='edit_shipment', sid=sid))
    if allow_delete:
        kb.add_button('🗑 Удалить', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='del_shipment', sid=sid))
    return kb.get_keyboard()


def edit_menu(sid):
    kb = VkKeyboard(inline=True)
    kb.add_button('Клиент', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='client'))
    kb.add_button('Тоннаж', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='tonnage'))
    kb.add_line()
    kb.add_button('Дата', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='date'))
    kb.add_button('Коммент.', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='edit_field', sid=sid, field='comment'))
    kb.add_line()
    kb.add_button('✅ Готово', color=VkKeyboardColor.POSITIVE, payload=_p(cmd='to_menu'))
    kb.add_button('❌ Отмена', color=VkKeyboardColor.NEGATIVE, payload=_p(cmd='cancel'))
    return kb.get_keyboard()


def day_actions(shipments, d_iso):
    kb = VkKeyboard(inline=True)
    for i, s in enumerate(shipments):
        kb.add_button(f'#{i+1}', color=VkKeyboardColor.SECONDARY,
                      payload=_p(cmd='open_shipment', sid=s['id']))
        if i % 5 == 4 and i != len(shipments) - 1:
            kb.add_line()
    if shipments:
        kb.add_line()
    kb.add_button('➕ Добавить', color=VkKeyboardColor.POSITIVE,
                  payload=_p(cmd='add_again_for', date=d_iso))
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY, payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


def admin_menu():
    kb = VkKeyboard(inline=True)
    kb.add_button('💾 Скачать базу', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='download_db'))
    kb.add_button('📤 Экспорт CSV (всё)', color=VkKeyboardColor.PRIMARY,
                  payload=_p(cmd='export_csv_all'))
    kb.add_line()
    kb.add_button('📤 Экспорт CSV (30 дн.)', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='export_csv'))
    kb.add_line()
    kb.add_button('🏠 В меню', color=VkKeyboardColor.SECONDARY,
                  payload=_p(cmd='to_menu'))
    return kb.get_keyboard()


# ════════════════════════════════════════════════════════════
#                        ИНИЦИАЛИЗАЦИЯ VK
# ════════════════════════════════════════════════════════════

init_db()

vk_session = None
vk = None
longpoll = None

if TOKEN:
    try:
        vk_session = vk_api.VkApi(token=TOKEN)
        vk = vk_session.get_api()
        longpoll = VkLongPoll(vk_session)
    except Exception as e:
        print(f"❌ Ошибка инициализации VK: {e}")
        vk_session = None
        vk = None
        longpoll = None


# ════════════════════════════════════════════════════════════
#                          УТИЛИТЫ
# ════════════════════════════════════════════════════════════

def is_admin(user_id):
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def send(user_id, text, keyboard=None, attachment=None):
    if vk is None:
        print("[send] VK не инициализирован")
        return
    if len(text) > 4000:
        text = text[:3990] + "\n…(обрезано)"
    params = {'user_id': user_id, 'message': text, 'random_id': get_random_id()}
    if keyboard:
        params['keyboard'] = keyboard
    if attachment:
        params['attachment'] = attachment
    try:
        vk.messages.send(**params)
    except Exception:
        traceback.print_exc()


def fmt_num(x):
    if x == int(x):
        return str(int(x))
    return f"{x:.2f}".rstrip('0').rstrip('.')


def parse_number(text):
    text = text.strip().replace(',', '.').replace(' ', '')
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(text):
    text = text.strip()
    today = tz_today()
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ('%d.%m', '%d/%m'):
        try:
            d = datetime.strptime(text, fmt).date()
            return d.replace(year=today.year)
        except ValueError:
            pass
    low = text.lower()
    if low in ('сегодня', 'today', 'с'):
        return today
    if low in ('завтра', 'tomorrow', 'з'):
        return today + timedelta(days=1)
    if low == 'послезавтра':
        return today + timedelta(days=2)
    return None


def ru_date(d):
    if isinstance(d, str):
        d = datetime.strptime(d, '%Y-%m-%d').date()
    return d.strftime('%d.%m.%Y')


# ════════════════════════════════════════════════════════════
#                       ФОРМАТИРОВАНИЕ
# ════════════════════════════════════════════════════════════

def format_shipment_card(s):
    if not s:
        return "❌ Отгрузка не найдена (возможно, удалена)."
    d = ru_date(s['shipment_date'])
    lines = [
        f"#{s['id']} от {d}",
        f"👤 Клиент: {s['client_name']}",
        f"⚖️ Тоннаж: {fmt_num(s['tonnage'])} т",
        f"🚛 Фур: {fmt_num(s['trucks'])} ({fmt_num(s['tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})",
    ]
    if s.get('comment'):
        lines.append(f"💬 Комментарий: {s['comment']}")
    return "\n".join(lines)


def format_day_shipments(d, shipments, title=None):
    if title is None:
        title = f"📋 Погрузки на {ru_date(d)}:"
    lines = [title, ""]
    if not shipments:
        lines.append("— нет —")
        return "\n".join(lines)
    total_t = 0.0
    for i, s in enumerate(shipments, 1):
        lines.append(f"{i}. {s['client_name']} — {fmt_num(s['tonnage'])} т "
                     f"({fmt_num(s['trucks'])} фур)")
        total_t += s['tonnage']
    total_trucks = round(total_t / TRUCK_CAPACITY, 2)
    lines += ["", f"Итого: {fmt_num(total_t)} т, {fmt_num(total_trucks)} фур"]
    return "\n".join(lines)


def format_stats(title, stats):
    lines = [f"📊 {title}", ""]
    if stats['count'] == 0:
        lines.append("Нет отгрузок за период.")
        return "\n".join(lines)
    lines.append(f"Всего отгрузок: {stats['count']}")
    lines.append(f"Общий тоннаж: {fmt_num(stats['total_tonnage'])} т")
    lines.append(f"Фур: {fmt_num(stats['total_trucks'])} "
                 f"({fmt_num(stats['total_tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})")
    lines.append("")
    lines.append("👥 По клиентам:")
    for name, c in sorted(stats['by_client'].items(), key=lambda x: -x[1]['tonnage'])[:30]:
        lines.append(f"• {name}: {fmt_num(c['tonnage'])} т / {fmt_num(c['trucks'])} фур")
    lines.append("")
    lines.append("📅 По дням:")
    for d in sorted(stats['by_day'].keys(), reverse=True)[:30]:
        c = stats['by_day'][d]
        lines.append(f"• {ru_date(d)}: {fmt_num(c['tonnage'])} т / {fmt_num(c['trucks'])} фур")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#                          МЕНЮ
# ════════════════════════════════════════════════════════════

def show_main_menu(user_id):
    u = next((x for x in get_all_users() if x['user_id'] == user_id), None)
    morning_on = bool(u and u.get('morning_report'))
    send(user_id, "Главное меню:", main_menu(is_admin(user_id), morning_on))


def show_help(user_id):
    text = (
        "🤖 Бот учёта отгрузок склада\n\n"
        "Что я умею:\n"
        "• ➕ Добавлять погрузки\n"
        "• 📋 Показывать погрузки на день\n"
        "• ✏️ Изменять и удалять отгрузки\n"
        "• 📊 Статистика за любой период\n"
        "• 📁 Справочник клиентов\n"
        "• 🔔 Утренняя сводка\n"
        "• 📤 Экспорт в CSV\n\n"
        f"🚛 Фур = тоннаж ÷ {fmt_num(TRUCK_CAPACITY)}\n"
        f"🕐 Часовой пояс: Уфа (UTC+{TIMEZONE_OFFSET_HOURS})"
    )
    send(user_id, text, main_menu(is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                        ДОБАВЛЕНИЕ
# ════════════════════════════════════════════════════════════

def start_add(user_id):
    clear_state(user_id)
    send(user_id, "📅 На какую дату добавить погрузку?", date_choice_menu())


def start_add_with_date(user_id, d_iso):
    clear_state(user_id)
    set_state(user_id, pending={'date': d_iso})
    show_client_picker(user_id, action='add')


def show_client_picker(user_id, action='add', sid=None, page=0):
    clients = get_clients()
    if not clients:
        update_state(user_id, awaiting='new_client_name', action=action, sid=sid)
        send(user_id, "📁 Справочник пуст. Введите имя первого клиента:")
        return
    send(user_id, "👤 Выберите клиента:",
         clients_menu(clients, action=action, sid=sid, page=page))


def show_confirm(user_id):
    p = get_state(user_id).get('pending', {})
    if not (p.get('date') and p.get('client') and p.get('tonnage')):
        send(user_id, "❌ Недостаточно данных.")
        clear_state(user_id)
        show_main_menu(user_id)
        return
    trucks = calc_trucks(p['tonnage'])
    text = (
        "Проверьте данные:\n\n"
        f"📅 Дата: {ru_date(p['date'])}\n"
        f"👤 Клиент: {p['client']}\n"
        f"⚖️ Тоннаж: {fmt_num(p['tonnage'])} т\n"
        f"🚛 Фур: {fmt_num(trucks)} ({fmt_num(p['tonnage'])} ÷ {fmt_num(TRUCK_CAPACITY)})\n"
    )
    if p.get('comment'):
        text += f"💬 Комментарий: {p['comment']}\n"
    send(user_id, text, confirm_menu())


def on_save_shipment(user_id):
    p = get_state(user_id).get('pending', {})
    if not (p.get('date') and p.get('client') and p.get('tonnage')):
        send(user_id, "❌ Недостаточно данных.")
        clear_state(user_id)
        show_main_menu(user_id)
        return
    sid = add_shipment(p['client'], p['tonnage'], p['date'], user_id,
                       comment=p.get('comment', ''))
    s = get_shipment(sid)
    d_iso = p['date']
    clear_state(user_id)
    set_state(user_id, pending={'date': d_iso})
    send(user_id, f"✅ Погрузка добавлена!\n\n{format_shipment_card(s)}",
         after_save_menu())


# ════════════════════════════════════════════════════════════
#                       ПРОСМОТР ДНЯ
# ════════════════════════════════════════════════════════════

def show_day_shipments(user_id, d):
    shipments = get_shipments_by_date(d)
    text = format_day_shipments(d, shipments)
    send(user_id, text, day_actions(shipments, d.isoformat()))


def on_open_shipment(user_id, payload):
    sid = payload.get('sid')
    s = get_shipment(sid)
    if not s:
        send(user_id, "❌ Отгрузка не найдена.")
        return
    send(user_id, format_shipment_card(s),
         shipment_item_menu(sid, allow_delete=is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                        СТАТИСТИКА
# ════════════════════════════════════════════════════════════

def show_stats(user_id, period=None, start=None, end=None):
    today = tz_today()
    if period == 'today':
        s, e = today, today
        title = f"Сегодня ({ru_date(today)})"
    elif period == 'yesterday':
        y = today - timedelta(days=1)
        s, e = y, y
        title = f"Вчера ({ru_date(y)})"
    elif period == 'week':
        s = today - timedelta(days=today.weekday())
        e = today
        title = f"Эта неделя ({ru_date(s)}–{ru_date(e)})"
    elif period == 'last_week':
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=6)
        title = f"Прошлая неделя ({ru_date(s)}–{ru_date(e)})"
    elif period == 'month':
        s = today.replace(day=1)
        e = today
        title = f"Этот месяц ({ru_date(s)}–{ru_date(e)})"
    elif period == 'last_month':
        first = today.replace(day=1)
        e = first - timedelta(days=1)
        s = e.replace(day=1)
        title = f"Прошлый месяц ({ru_date(s)}–{ru_date(e)})"
    elif period == 'year':
        s = today.replace(month=1, day=1)
        e = today
        title = f"Этот год ({ru_date(s)}–{ru_date(e)})"
    elif period == 'custom':
        try:
            s = datetime.strptime(start, '%Y-%m-%d').date()
            e = datetime.strptime(end, '%Y-%m-%d').date()
        except Exception:
            send(user_id, "Ошибка в датах.")
            return
        title = f"Период {ru_date(s)}–{ru_date(e)}"
    else:
        send(user_id, "Выберите период:", stats_menu())
        return
    stats = stats_for_period(s, e)
    send(user_id, format_stats(title, stats), main_menu(is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                           CSV
# ════════════════════════════════════════════════════════════

def upload_csv_doc(user_id, data, filename):
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    try:
        with open(path, 'wb') as f:
            f.write(data)
        upload = vk_api.VkUpload(vk_session)
        resp = upload.document(path, filename, peer_id=user_id)
        doc = resp.get('doc') if isinstance(resp, dict) else None
        if not doc:
            raise RuntimeError(f"Некорректный ответ: {resp}")
        return f"doc{doc['owner_id']}_{doc['id']}"
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def upload_photo_bytes(user_id, photo_bytes):
    fd, path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        with open(path, 'wb') as f:
            f.write(photo_bytes)
        upload = vk_api.VkUpload(vk_session)
        photo = upload.photo_messages(photos=path, peer_id=user_id)[0]
        return f"photo{photo['owner_id']}_{photo['id']}"
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def on_export_csv(user_id, days=30):
    end = tz_today()
    if days is None:
        shipments = get_shipments_by_period(date(2000, 1, 1), end)
        title = "Полный экспорт"
        fname = f'shipments_all_{end.isoformat()}.csv'
    else:
        start = end - timedelta(days=days)
        shipments = get_shipments_by_period(start, end)
        title = f"Экспорт за последние {days} дней"
        fname = f'shipments_{start.isoformat()}_{end.isoformat()}.csv'

    if not shipments:
        send(user_id, "Нет данных.")
        return

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['ID', 'Дата', 'Клиент', 'Тоннаж (т)',
                'Фур (расчёт)', 'Комментарий', 'Создано'])
    for s in shipments:
        w.writerow([s['id'], s['shipment_date'], s['client_name'],
                    s['tonnage'], s['trucks'], s.get('comment', ''),
                    s.get('created_at', '')])
    data = buf.getvalue().encode('utf-8-sig')
    try:
        attach = upload_csv_doc(user_id, data, fname)
        send(user_id, f"📤 {title} ({len(shipments)} строк):",
             admin_menu() if is_admin(user_id) else main_menu(is_admin(user_id)),
             attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось загрузить файл: {e}")


# ════════════════════════════════════════════════════════════
#                      ИСТОРИЯ КЛИЕНТА
# ════════════════════════════════════════════════════════════

def show_clients_for_history(user_id, page=0):
    clients = get_clients()
    if not clients:
        send(user_id, "📁 Справочник пуст. Добавьте первую погрузку.",
             main_menu(is_admin(user_id)))
        return
    send(user_id, "👤 Выберите клиента:",
         clients_menu(clients, action='view', page=page))


def show_client_card(user_id, cid):
    c = get_client(cid)
    if not c:
        send(user_id, "❌ Клиент не найден.")
        return
    summary = client_summary(c['name'])
    if not summary:
        send(user_id, f"👤 {c['name']}\n\nНет отгрузок.", client_card_menu(cid))
        return
    lines = [
        f"👤 {c['name']}",
        "",
        f"Всего отгрузок: {summary['count']}",
        f"Общий тоннаж: {fmt_num(summary['total_tonnage'])} т",
        f"Фур: {fmt_num(summary['total_trucks'])}",
        f"Средний тоннаж: {fmt_num(round(summary['avg_tonnage'], 2))} т",
        "",
        f"Первая отгрузка: {ru_date(summary['first_date'])}",
        f"Последняя: {ru_date(summary['last_date'])}",
    ]
    send(user_id, "\n".join(lines), client_card_menu(cid))


def show_client_history(user_id, cid):
    c = get_client(cid)
    if not c:
        send(user_id, "❌ Клиент не найден.")
        return
    shipments = get_shipments_by_client(c['name'], limit=100)
    lines = [f"📜 {c['name']} — последние отгрузки:", ""]
    if not shipments:
        lines.append("— нет —")
    else:
        total_t = 0.0
        for s in shipments:
            lines.append(f"• {ru_date(s['shipment_date'])} — "
                         f"{fmt_num(s['tonnage'])} т ({fmt_num(s['trucks'])} фур)")
            total_t += s['tonnage']
        lines += ["", f"Всего (последние {len(shipments)}): {fmt_num(total_t)} т"]
    send(user_id, "\n".join(lines), client_card_menu(cid))


# ════════════════════════════════════════════════════════════
#                      СРАВНЕНИЕ НЕДЕЛЬ
# ════════════════════════════════════════════════════════════

def show_week_comparison(user_id):
    cmp = week_comparison()
    this_s, last_s = cmp['this'], cmp['last']
    t1, t0 = this_s['total_tonnage'], last_s['total_tonnage']

    if t0 > 0:
        diff_pct = (t1 - t0) / t0 * 100
        pct_sign = '📈 +' if diff_pct >= 0 else '📉 '
        pct_line = f"{pct_sign}{diff_pct:.1f}%"
    else:
        pct_line = "—" if t1 == 0 else "новая неделя"

    diff_t = t1 - t0
    diff_trucks = round(diff_t / TRUCK_CAPACITY, 2)

    lines = [
        "⚖️ Сравнение недель",
        "",
        f"Прошлая ({ru_date(cmp['last_range'][0])}–{ru_date(cmp['last_range'][1])}):",
        f"  • {fmt_num(t0)} т",
        f"  • {fmt_num(last_s['total_trucks'])} фур",
        f"  • {last_s['count']} отгрузок",
        "",
        f"Эта ({ru_date(cmp['this_range'][0])}–{ru_date(cmp['this_range'][1])}):",
        f"  • {fmt_num(t1)} т",
        f"  • {fmt_num(this_s['total_trucks'])} фур",
        f"  • {this_s['count']} отгрузок",
        "",
        f"Разница: {fmt_num(diff_t)} т ({pct_line})",
        f"          {fmt_num(diff_trucks)} фур",
    ]
    send(user_id, "\n".join(lines), main_menu(is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                          ГРАФИКИ
# ════════════════════════════════════════════════════════════

def make_chart_png(start, end, title, shipments):
    by_day = defaultdict(float)
    d = start
    while d <= end:
        by_day[d] = 0.0
        d += timedelta(days=1)
    for s in shipments:
        day = datetime.strptime(s['shipment_date'], '%Y-%m-%d').date()
        by_day[day] += s['tonnage']

    dates = sorted(by_day.keys())
    values = [by_day[d] for d in dates]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(dates, values, color='#4a76a8', edgecolor='#2c4a70')

    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_ylabel('Тонны')
    ax.grid(axis='y', linestyle='--', alpha=0.5)
    ax.set_axisbelow(True)

    for bar, v in zip(bars, values):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height(),
                    f'{v:.0f}',
                    ha='center', va='bottom', fontsize=9)

    if len(dates) > 7:
        ax.xaxis.set_major_formatter(DateFormatter('%d.%m'))
        plt.xticks(rotation=45, ha='right')
    else:
        ax.set_xticks(dates)
        ax.set_xticklabels([d.strftime('%d.%m') for d in dates])

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def show_chart(user_id, period=None, start=None, end=None, client_name=None):
    today = tz_today()

    if client_name:
        shipments = get_shipments_by_client(client_name, limit=2000)
        if not shipments:
            send(user_id, "Нет данных.")
            return
        dates = sorted(set(s['shipment_date'] for s in shipments))
        s = datetime.strptime(dates[0], '%Y-%m-%d').date()
        e = datetime.strptime(dates[-1], '%Y-%m-%d').date()
        title = f"{client_name} — отгрузки ({ru_date(s)}–{ru_date(e)})"
    else:
        if period == 'week':
            s = today - timedelta(days=today.weekday())
            e = today
            title = f"Отгрузки за неделю ({ru_date(s)}–{ru_date(e)})"
        elif period == 'last_week':
            s = today - timedelta(days=today.weekday() + 7)
            e = s + timedelta(days=6)
            title = f"Отгрузки за прошлую неделю"
        elif period == 'month':
            s = today.replace(day=1)
            e = today
            title = f"Отгрузки за месяц ({ru_date(s)}–{ru_date(e)})"
        elif period == 'last_month':
            first = today.replace(day=1)
            e = first - timedelta(days=1)
            s = e.replace(day=1)
            title = f"Отгрузки за прошлый месяц"
        elif period == 'custom':
            s = datetime.strptime(start, '%Y-%m-%d').date()
            e = datetime.strptime(end, '%Y-%m-%d').date()
            title = f"Отгрузки {ru_date(s)}–{ru_date(e)}"
        else:
            s, e = today - timedelta(days=6), today
            title = "Отгрузки за последние 7 дней"

        shipments = get_shipments_by_period(s, e)

    if not shipments:
        send(user_id, "Нет данных за период.", main_menu(is_admin(user_id)))
        return

    png = make_chart_png(s, e, title, shipments)
    try:
        attach = upload_photo_bytes(user_id, png)
        total = sum(x['tonnage'] for x in shipments)
        caption = (f"📈 {title}\n\n"
                   f"Итого: {fmt_num(total)} т, "
                   f"{fmt_num(round(total / TRUCK_CAPACITY, 2))} фур")
        send(user_id, caption, main_menu(is_admin(user_id)), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить график: {e}")


# ════════════════════════════════════════════════════════════
#                      НАСТРОЙКИ / АДМИН
# ════════════════════════════════════════════════════════════

def on_download_db(user_id):
    if not is_admin(user_id):
        send(user_id, "❌ Только для администраторов.")
        return
    try:
        data = get_db_bytes()
        attach = upload_csv_doc(user_id, data,
                                f'shipments_{tz_today().isoformat()}.db')
        size_kb = len(data) / 1024
        send(user_id, f"💾 Резервная копия базы ({size_kb:.1f} КБ):",
             admin_menu(), attachment=attach)
    except Exception as e:
        traceback.print_exc()
        send(user_id, f"❌ Не удалось отправить базу: {e}")


def show_clients_info(user_id):
    clients = get_clients(limit=200)
    if not clients:
        send(user_id, "📁 Справочник пуст. Добавьте первую погрузку.",
             main_menu(is_admin(user_id)))
        return
    lines = ["📁 Клиенты:", ""]
    for c in clients[:50]:
        sh = get_shipments_by_client(c['name'])
        total_t = sum(s['tonnage'] for s in sh)
        lines.append(f"• {c['name']}: {len(sh)} отгр., {fmt_num(total_t)} т")
    if len(clients) > 50:
        lines.append(f"\n… и ещё {len(clients) - 50}")
    send(user_id, "\n".join(lines), main_menu(is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                     ОБРАБОТКА PAYLOAD
# ════════════════════════════════════════════════════════════

def handle_payload(user_id, payload):
    cmd = payload.get('cmd')

    if cmd == 'cancel':
        clear_state(user_id)
        send(user_id, "Отменено.", main_menu(is_admin(user_id)))

    elif cmd == 'to_menu':
        clear_state(user_id)
        show_main_menu(user_id)

    elif cmd == 'pick_date':
        d = payload.get('date')
        if d == 'today':
            target = tz_today()
        elif d == 'tomorrow':
            target = tz_today() + timedelta(days=1)
        elif d == 'custom':
            update_state(user_id, awaiting='add_custom_date')
            send(user_id, "Введите дату (ДД.ММ.ГГГГ). Можно: 'сегодня', 'завтра', 'послезавтра'.")
            return
        else:
            return
        update_state(user_id, pending={'date': target.isoformat()})
        show_client_picker(user_id, action='add')

    elif cmd == 'pick_client':
        cid = payload.get('cid')
        action = payload.get('action', 'add')
        sid = payload.get('sid')
        c = get_client(cid)
        if not c:
            send(user_id, "Клиент не найден.")
            return
        name = c['name']
        if action == 'add':
            p = get_state(user_id).get('pending', {})
            p['client'] = name
            update_state(user_id, awaiting='add_tonnage', pending=p)
            send(user_id, f"👤 {name}\nВведите тоннаж или выберите:", tonnage_menu())
        elif action == 'edit_client' and sid:
            update_shipment(sid, client_name=name, user_id=user_id)
            clear_state(user_id)
            s = get_shipment(sid)
            send(user_id, f"✅ Клиент изменён.\n\n{format_shipment_card(s)}",
                 main_menu(is_admin(user_id)))

    elif cmd == 'clients_page':
        show_client_picker(user_id, action=payload.get('action', 'add'),
                           sid=payload.get('sid'), page=int(payload.get('page', 0)))

    elif cmd == 'new_client':
        update_state(user_id, awaiting='new_client_name',
                     action=payload.get('action', 'add'),
                     sid=payload.get('sid'))
        send(user_id, "Введите имя нового клиента:")

    elif cmd == 'pick_tonnage':
        t = payload.get('t')
        if t == 'custom':
            update_state(user_id, awaiting='add_tonnage')
            send(user_id, "Введите тоннаж числом (например, 25.5):")
            return
        try:
            t = float(t)
        except (TypeError, ValueError):
            return
        p = get_state(user_id).get('pending', {})
        p['tonnage'] = t
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif cmd == 'save_shipment':
        on_save_shipment(user_id)

    elif cmd == 'restart_add':
        start_add(user_id)

    elif cmd == 'add_again':
        p = get_state(user_id).get('pending', {})
        d = p.get('date') or tz_today().isoformat()
        start_add_with_date(user_id, d)

    elif cmd == 'add_again_for':
        start_add_with_date(user_id, payload.get('date') or tz_today().isoformat())

    elif cmd == 'add_comment':
        update_state(user_id, awaiting='add_comment')
        send(user_id, "Введите комментарий (или '-' чтобы убрать):")

    elif cmd == 'stats':
        period = payload.get('period')
        if period == 'custom':
            update_state(user_id, awaiting='stats_custom_start')
            send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):")
            return
        show_stats(user_id, period=period)

    elif cmd == 'export_csv':
        on_export_csv(user_id)

    elif cmd == 'export_csv_all':
        on_export_csv(user_id, days=None)

    elif cmd == 'download_db':
        on_download_db(user_id)

    elif cmd == 'admin_menu':
        if is_admin(user_id):
            send(user_id, "⚙️ Настройки:", admin_menu())

    elif cmd == 'open_shipment':
        on_open_shipment(user_id, payload)

    elif cmd == 'edit_shipment':
        sid = payload.get('sid')
        s = get_shipment(sid)
        if not s:
            send(user_id, "Не найдено.")
            return
        send(user_id, f"Что изменить?\n\n{format_shipment_card(s)}", edit_menu(sid))

    elif cmd == 'edit_field':
        sid = payload.get('sid')
        field = payload.get('field')
        s = get_shipment(sid)
        if not s:
            send(user_id, "Не найдено.")
            return
        if field == 'client':
            show_client_picker(user_id, action='edit_client', sid=sid)
        elif field == 'tonnage':
            update_state(user_id, awaiting='edit_tonnage', sid=sid)
            send(user_id, f"Текущий тоннаж: {fmt_num(s['tonnage'])} т\nВведите новый:")
        elif field == 'date':
            update_state(user_id, awaiting='edit_date', sid=sid)
            send(user_id, f"Текущая дата: {ru_date(s['shipment_date'])}\nВведите новую (ДД.ММ.ГГГГ):")
        elif field == 'comment':
            update_state(user_id, awaiting='edit_comment', sid=sid)
            send(user_id, f"Текущий комментарий: {s.get('comment') or '(нет)'}\nВведите новый (или '-' чтобы убрать):")

    elif cmd == 'del_shipment':
        sid = payload.get('sid')
        if not is_admin(user_id):
            send(user_id, "❌ Удалять может только администратор.")
            return
        delete_shipment(sid)
        send(user_id, "🗑 Удалено.", main_menu(is_admin(user_id)))

    elif cmd == 'open_client':
        show_client_card(user_id, payload.get('cid'))

    elif cmd == 'clients_summary':
        show_clients_info(user_id)

    elif cmd == 'client_history':
        show_client_history(user_id, payload.get('cid'))

    elif cmd == 'client_chart':
        c = get_client(payload.get('cid'))
        if not c:
            send(user_id, "❌ Клиент не найден.")
            return
        show_chart(user_id, client_name=c['name'])

    elif cmd == 'week_compare':
        show_week_comparison(user_id)

    elif cmd == 'chart':
        period = payload.get('period')
        if period == 'custom':
            update_state(user_id, awaiting='chart_custom_start')
            send(user_id, "Введите начальную дату (ДД.ММ.ГГГГ):")
            return
        show_chart(user_id, period=period)


# ════════════════════════════════════════════════════════════
#                     ОБРАБОТКА ТЕКСТА
# ════════════════════════════════════════════════════════════

def handle_text(user_id, text):
    state = get_state(user_id)
    awaiting = state.get('awaiting')

    if awaiting == 'add_custom_date':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        clear_state(user_id)
        set_state(user_id, pending={'date': d.isoformat()})
        show_client_picker(user_id, action='add')

    elif awaiting == 'new_client_name':
        name = text.strip()
        if not name:
            send(user_id, "❌ Пустое имя. Введите ещё раз:")
            return
        action = state.get('action', 'add')
        sid = state.get('sid')
        if action == 'edit_client' and sid:
            update_shipment(sid, client_name=name, user_id=user_id)
            clear_state(user_id)
            s = get_shipment(sid)
            send(user_id, f"✅ Клиент изменён.\n\n{format_shipment_card(s)}",
                 main_menu(is_admin(user_id)))
        else:
            touch_client(name)
            p = state.get('pending', {})
            p['client'] = name
            update_state(user_id, awaiting='add_tonnage', pending=p)
            send(user_id, f"👤 {name}\nВведите тоннаж или выберите:", tonnage_menu())

    elif awaiting == 'add_tonnage':
        t = parse_number(text)
        if t is None or t <= 0:
            send(user_id, "❌ Введите положительное число:")
            return
        p = state.get('pending', {})
        p['tonnage'] = t
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif awaiting == 'add_comment':
        comment = text.strip()
        if comment == '-':
            comment = ''
        p = state.get('pending', {})
        p['comment'] = comment
        update_state(user_id, awaiting=None, pending=p)
        show_confirm(user_id)

    elif awaiting == 'edit_tonnage':
        t = parse_number(text)
        if t is None or t <= 0:
            send(user_id, "❌ Введите положительное число:")
            return
        sid = state.get('sid')
        update_shipment(sid, tonnage=t, user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Тоннаж изменён.\n\n{format_shipment_card(s)}",
             main_menu(is_admin(user_id)))

    elif awaiting == 'edit_date':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        sid = state.get('sid')
        update_shipment(sid, shipment_date=d.isoformat(), user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Дата изменена.\n\n{format_shipment_card(s)}",
             main_menu(is_admin(user_id)))

    elif awaiting == 'edit_comment':
        comment = text.strip()
        if comment == '-':
            comment = ''
        sid = state.get('sid')
        update_shipment(sid, comment=comment, user_id=user_id)
        clear_state(user_id)
        s = get_shipment(sid)
        send(user_id, f"✅ Комментарий изменён.\n\n{format_shipment_card(s)}",
             main_menu(is_admin(user_id)))

    elif awaiting == 'stats_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        update_state(user_id, awaiting='stats_custom_end', stats_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):")

    elif awaiting == 'stats_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        start = state.get('stats_start')
        clear_state(user_id)
        show_stats(user_id, period='custom', start=start, end=d.isoformat())

    elif awaiting == 'chart_custom_start':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        update_state(user_id, awaiting='chart_custom_end', chart_start=d.isoformat())
        send(user_id, "Введите конечную дату (ДД.ММ.ГГГГ):")

    elif awaiting == 'chart_custom_end':
        d = parse_date(text)
        if not d:
            send(user_id, "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ:")
            return
        start = state.get('chart_start')
        clear_state(user_id)
        show_chart(user_id, period='custom', start=start, end=d.isoformat())

    elif text == '➕ Добавить погрузку':
        start_add(user_id)
    elif text == '📋 Сегодня':
        show_day_shipments(user_id, tz_today())
    elif text == '📋 Завтра':
        show_day_shipments(user_id, tz_today() + timedelta(days=1))
    elif text == '📅 Другая дата':
        update_state(user_id, awaiting='add_custom_date')
        send(user_id, "Введите дату (ДД.ММ.ГГГГ). Можно: 'сегодня', 'завтра', 'послезавтра'.")
    elif text == '📊 Статистика':
        send(user_id, "Выберите период:", stats_menu())
    elif text == '📁 Клиенты':
        show_clients_for_history(user_id)
    elif text == '❓ Помощь':
        show_help(user_id)
    elif text.startswith('🔔 Сводка') or text.startswith('🔕 Сводка'):
        new_val = toggle_morning_report(user_id)
        label = 'включена' if new_val else 'выключена'
        send(user_id, f"Утренняя сводка {label}.",
             main_menu(is_admin(user_id), bool(new_val)))
    elif text == '⚙️ Настройки' and is_admin(user_id):
        send(user_id, "⚙️ Настройки:", admin_menu())
    else:
        send(user_id, "Не понял. Используйте кнопки меню.",
             main_menu(is_admin(user_id)))


# ════════════════════════════════════════════════════════════
#                       ФОНОВЫЕ ЗАДАЧИ
# ════════════════════════════════════════════════════════════

_morning_last_day = None


def morning_report_loop():
    global _morning_last_day
    while True:
        try:
            n = tz_now()
            today_key = n.date().isoformat()
            if (n.hour == MORNING_REPORT_HOUR
                    and n.minute >= MORNING_REPORT_MINUTE
                    and _morning_last_day != today_key):
                _morning_last_day = today_key
                for u in get_all_users():
                    if not u.get('morning_report'):
                        continue
                    uid = u['user_id']
                    try:
                        shipments = get_shipments_by_date(n.date())
                        text = format_day_shipments(
                            n.date(), shipments,
                            f"🌅 Доброе утро! Погрузки на сегодня ({ru_date(n.date())}):")
                        send(uid, text, main_menu(is_admin(uid)))
                    except Exception:
                        traceback.print_exc()
        except Exception:
            traceback.print_exc()
        time.sleep(60)


_backup_last_day = None


def backup_loop():
    global _backup_last_day
    while True:
        try:
            n = tz_now()
            today_key = n.date().isoformat()
            if (n.hour == BACKUP_HOUR
                    and n.minute >= BACKUP_MINUTE
                    and _backup_last_day != today_key):
                path = make_backup()
                _backup_last_day = today_key
                if path:
                    print(f"[backup] saved: {path} at {n}")
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ════════════════════════════════════════════════════════════
#                    KEEP-ALIVE ВЕБ-СЕРВЕР
# ════════════════════════════════════════════════════════════

def _run_keepalive_server():
    """Мини веб-сервер — нужен, чтобы Bothost видел контейнер живым."""
    port = int(os.getenv('PORT', '8080'))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'OK')

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(('0.0.0.0', port), Handler)
        print(f"[keepalive] listening on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"[keepalive] error: {e}")


# ════════════════════════════════════════════════════════════
#                          ЗАПУСК
# ════════════════════════════════════════════════════════════

def _extract_payload(event):
    """Безопасно достаёт payload из события VK."""
    raw = getattr(event, 'payload', None)
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def main():
    print(f"[bot] starting at {tz_now()} (UTC+{TIMEZONE_OFFSET_HOURS})")

    if not TOKEN:
        print("❌ VK_TOKEN не задан. Проверьте переменные окружения Bothost.")
        return
    if not GROUP_ID:
        print("❌ VK_GROUP_ID не задан. Проверьте переменные окружения Bothost.")
        return
    if longpoll is None:
        print("❌ Long Poll не инициализирован (проверьте токен).")
        return

    threading.Thread(target=_run_keepalive_server, daemon=True).start()
    threading.Thread(target=morning_report_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()

    print("[bot] listening for events...")
    for event in longpoll.listen():
        if event.type == VkEventType.MESSAGE_NEW and event.to_me:
            user_id = event.user_id
            upsert_user(user_id)
            payload = _extract_payload(event)
            if payload and payload.get('cmd'):
                try:
                    handle_payload(user_id, payload)
                except Exception:
                    traceback.print_exc()
                    send(user_id, "❌ Ошибка. Попробуйте ещё раз.")
            else:
                try:
                    handle_text(user_id, event.text or '')
                except Exception:
                    traceback.print_exc()
                    send(user_id, "❌ Ошибка. Попробуйте ещё раз.")


if __name__ == '__main__':
    main()
