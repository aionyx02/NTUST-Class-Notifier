"""NTUST 選課系統自動加選 + session keepalive"""

import asyncio
import logging
import re
import sqlite3
import subprocess
import sys
from functools import partial
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

COURSE_SELECTION_ROOT = "https://courseselection.ntust.edu.tw/"
DEPT_SELECT_URL = "https://courseselection.ntust.edu.tw/First/A06/A06"
DEPT_SELECT_JOIN_URL = "https://courseselection.ntust.edu.tw/First/A06/ExtraJoin"
COURSE_LIST_URL = "https://courseselection.ntust.edu.tw/First/A02/A02"

# 從選課頁面 HTML 中抓取已選課程代碼
ENROLLED_COURSE_PATTERN = re.compile(r'class="table-cell">\s*([A-Z]{2}[A-Z0-9]{7})\s*<')
WISH_LIST_PATTERN = re.compile(
    r'<tr\s+class="data"[^>]*>\s*'
    r'<td[^>]*>\s*\d+\s*</td>\s*'
    r'<td[^>]*>\s*([A-Z]{2}[A-Z0-9]{7})\s*</td>',
    re.DOTALL,
)

OIDC_MARKERS = (
    frozenset({"code", "state", "iss"}),
    frozenset({"id_token"}),
    frozenset({"SAMLResponse"}),
    frozenset({"RelayState"}),
    frozenset({"wresult"}),
    frozenset({"wctx"}),
)

DB_PATH = Path(__file__).parent / "cookies.sqlite3"


def _form_payload(form) -> dict[str, str]:
    return {
        i.get("name"): i.get("value", "")
        for i in form.find_all("input") if i.get("name")
    }


def _is_login_page(resp: httpx.Response) -> bool:
    if "ssoam2.ntust.edu.tw" not in str(resp.url):
        return False
    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.find("form", id="loginForm"):
        return True
    names = {i.get("name", "") for i in soup.find_all("input")}
    return "Username" in names and "Password" in names


def _find_bridge_form(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        action = (form.get("action") or "").strip().lower()
        if not action or "logout" in action:
            continue
        names = {(i.get("name") or "").strip() for i in form.find_all("input")}
        if not names or {"Username", "Password"} & names:
            continue
        if any(marker <= names for marker in OIDC_MARKERS):
            return form
    return None


class CookieStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cookies "
            "(account TEXT, name TEXT, value TEXT, domain TEXT, path TEXT, "
            "PRIMARY KEY (account, name, domain, path))"
        )
        self._db.commit()

    def load_into(self, account: str, client: httpx.Client) -> bool:
        rows = self._db.execute(
            "SELECT name, value, domain, path FROM cookies WHERE account=?", (account,)
        ).fetchall()
        if not rows:
            return False
        client.cookies.clear()
        for name, value, domain, path in rows:
            client.cookies.set(name, value, domain=domain, path=path)
        return True

    def save_from(self, account: str, client: httpx.Client):
        self._db.execute("DELETE FROM cookies WHERE account=?", (account,))
        self._db.executemany(
            "INSERT INTO cookies VALUES (?,?,?,?,?)",
            [(account, c.name, c.value, c.domain or "", c.path or "/")
             for c in client.cookies.jar],
        )
        self._db.commit()

    def delete(self, account: str):
        self._db.execute("DELETE FROM cookies WHERE account=?", (account,))
        self._db.commit()

    def close(self):
        self._db.close()


class CourseSelector:
    """SSO 登入 + 電選課加選自動加選"""

    def __init__(self, student_id: str, password: str):
        self._account = student_id.strip().upper()
        self._password = password
        self._store = CookieStore()
        self._client = httpx.Client(
            http2=True,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": BROWSER_UA},
        )
        self._logged_in = False

    def login(self) -> bool:
        if self._logged_in:
            return True
        self._store.load_into(self._account, self._client)
        try:
            resp = self._resolve_bridges(self._client.get(COURSE_SELECTION_ROOT))
            if _is_login_page(resp):
                logger.info("Session 過期，重新 SSO 登入...")
                self._client.cookies.clear()
                self._store.delete(self._account)
                resp = self._client.get(COURSE_SELECTION_ROOT)
                if _is_login_page(resp):
                    resp = self._submit_login(resp)
                resp = self._resolve_bridges(resp)
                if _is_login_page(resp):
                    logger.error("SSO 登入失敗")
                    return False
            self._store.save_from(self._account, self._client)
            self._logged_in = True
            logger.info("選課系統登入成功")
            return True
        except Exception:
            logger.exception("登入過程發生錯誤")
            return False

    def keepalive(self) -> bool:
        """訪問選課頁面以延長 session"""
        try:
            resp = self._client.get(DEPT_SELECT_URL)
            if _is_login_page(resp):
                logger.warning("Session 已過期，嘗試重新登入...")
                self._logged_in = False
                return self.login()
            self._store.save_from(self._account, self._client)
            logger.debug("Session keepalive OK")
            return True
        except Exception as e:
            logger.error(f"Keepalive 失敗: {e}")
            return False

    def select_course(self, course_no: str) -> tuple[bool, str]:
        """電選課加選"""
        if not self._logged_in and not self.login():
            return False, "登入失敗"
        try:
            resp = self._client.post(
                DEPT_SELECT_JOIN_URL,
                data={"CourseNo": course_no, "type": "3"},
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            )
            self._store.save_from(self._account, self._client)
            body = resp.text
            logger.info(f"加選 {course_no} 回應: {resp.status_code} | {body[:200]}")
            return resp.is_success, body
        except Exception as e:
            logger.error(f"加選 {course_no} 發生錯誤: {e}")
            return False, str(e)

    def verify_enrolled(self, course_no: str) -> bool:
        """檢查課程是否已出現在已選清單中"""
        if not self._logged_in and not self.login():
            return False
        try:
            resp = self._client.get(COURSE_LIST_URL)
            html = resp.text
            enrolled = ENROLLED_COURSE_PATTERN.findall(html)
            wished = WISH_LIST_PATTERN.findall(html)
            all_courses = set(enrolled + wished)
            found = course_no.upper() in all_courses
            logger.info(f"驗證加選結果: {course_no} {'已在清單中 ✓' if found else '未在清單中 ✗'}")
            return found
        except Exception as e:
            logger.error(f"驗證加選結果時發生錯誤: {e}")
            return False

    def close(self):
        self._client.close()
        self._store.close()

    def _resolve_bridges(self, resp: httpx.Response, max_steps: int = 3) -> httpx.Response:
        current = resp
        for _ in range(max_steps):
            if _is_login_page(current):
                return current
            form = _find_bridge_form(current.text)
            if not form:
                return current
            action = urljoin(str(current.url), form.get("action"))
            payload = _form_payload(form)
            current = self._client.post(action, data=payload)
        return current

    def _submit_login(self, resp: httpx.Response) -> httpx.Response:
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", id="loginForm")
        if not form:
            raise RuntimeError("找不到 SSO 登入表單")
        payload = _form_payload(form)
        payload.update(Username=self._account, Password=self._password)
        payload.setdefault("captcha", "")
        action = urljoin(str(resp.url), form.get("action") or str(resp.url))
        return self._client.post(action, data=payload)


async def run_sync(func, *args):
    """在 executor 中跑同步函式"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args))


def play_sound(sound_type: str = "vacancy"):
    """播放系統音效 (macOS)"""
    if sys.platform != "darwin":
        return
    sounds = {
        "vacancy": "Glass",       # 有空位
        "success": "Hero",        # 加選成功
        "failure": "Sosumi",      # 加選失敗
    }
    sound_name = sounds.get(sound_type, "Glass")
    subprocess.Popen(
        ["afplay", f"/System/Library/Sounds/{sound_name}.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
