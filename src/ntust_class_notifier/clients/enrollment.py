"""台科大選課系統的登入、加選與 session 維持。

透過 SSO 登入後把 cookie 存進 config.data_dir()，供電選課加選期間送出加選
請求。只有 ntust-notify 在設定了帳密時才會用到這一支。
"""

import asyncio
import functools
import logging
import pathlib
import re
import sqlite3
import urllib.parse

import bs4
import httpx

from ntust_class_notifier import config

logger = logging.getLogger(__name__)


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


COURSE_SELECTION_ROOT = "https://courseselection.ntust.edu.tw/"


DEPT_SELECT_URL = "https://courseselection.ntust.edu.tw/First/A06/A06"


DEPT_SELECT_JOIN_URL = (
    "https://courseselection.ntust.edu.tw/First/A06/ExtraJoin"
)


COURSE_LIST_URL = "https://courseselection.ntust.edu.tw/First/A02/A02"


# 從選課頁面 HTML 中抓取已選課程代碼
ENROLLED_COURSE_PATTERN = re.compile(
    r'class="table-cell">\s*([A-Z]{2}[A-Z0-9]{7})\s*<'
)


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


def _form_payload(form) -> dict[str, str]:
    return {
        i.get("name"): i.get("value", "")
        for i in form.find_all("input") if i.get("name")
    }


def _is_login_page(resp: httpx.Response) -> bool:
    if "ssoam2.ntust.edu.tw" not in str(resp.url):
        return False
    soup = bs4.BeautifulSoup(resp.text, "html.parser")
    if soup.find("form", id="loginForm"):
        return True
    names = {i.get("name", "") for i in soup.find_all("input")}
    return "Username" in names and "Password" in names


def _find_bridge_form(html: str):
    soup = bs4.BeautifulSoup(html, "html.parser")
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
    def __init__(self, db_path: pathlib.Path | None = None):
        """開啟 cookie 資料庫。

        Args:
            db_path: 資料庫位置，None 代表用 config.data_dir() 下的預設檔。
        """
        db_path = db_path or config.data_dir() / "cookies.sqlite3"
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cookies "
            "(account TEXT, name TEXT, value TEXT, domain TEXT, path TEXT, "
            "PRIMARY KEY (account, name, domain, path))"
        )
        self._db.commit()

    def load_into(self, account: str, client: httpx.Client) -> bool:
        rows = self._db.execute(
            "SELECT name, value, domain, path FROM cookies WHERE account=?",
            (account,),
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
    """選課系統的 SSO 登入與加選操作。

    登入後的 cookie 會存進 config.data_dir()，避免每次執行都要重新 SSO
    登入。
    """

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
            resp = self._resolve_bridges(
                self._client.get(COURSE_SELECTION_ROOT))
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
        """訪問選課頁面以延長 session。

        Returns:
            session 仍有效（或重新登入成功）為 True。
        """
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
            logger.error("Keepalive 失敗: %s", e)
            return False

    def select_course(self, course_no: str) -> tuple[bool, str]:
        """送出電選課加選請求。

        Args:
            course_no: 要加選的課程代碼。

        Returns:
            (是否送出成功, 伺服器回應內容)。送出成功不代表搶到名額，
            仍需以 verify_enrolled() 確認。
        """
        if not self._logged_in and not self.login():
            return False, "登入失敗"
        try:
            resp = self._client.post(
                DEPT_SELECT_JOIN_URL,
                data={"CourseNo": course_no, "type": "3"},
                headers={
                    "Content-Type":
                        "application/x-www-form-urlencoded; charset=UTF-8"
                },
            )
            self._store.save_from(self._account, self._client)
            body = resp.text
            logger.info("加選 %s 回應: %s | %s",
                        course_no, resp.status_code, body[:200])
            return resp.is_success, body
        except Exception as e:
            logger.error("加選 %s 發生錯誤: %s", course_no, e)
            return False, str(e)

    def verify_enrolled(self, course_no: str) -> bool:
        """檢查課程是否真的出現在已選清單中。

        Args:
            course_no: 要確認的課程代碼。

        Returns:
            課程已在清單（含志願序清單）中為 True。
        """
        if not self._logged_in and not self.login():
            return False
        try:
            resp = self._client.get(COURSE_LIST_URL)
            html = resp.text
            enrolled = ENROLLED_COURSE_PATTERN.findall(html)
            wished = WISH_LIST_PATTERN.findall(html)
            all_courses = set(enrolled + wished)
            found = course_no.upper() in all_courses
            state = "已在清單中" if found else "未在清單中"
            logger.info("驗證加選結果: %s %s", course_no, state)
            return found
        except Exception as e:
            logger.error("驗證加選結果時發生錯誤: %s", e)
            return False

    def close(self):
        self._client.close()
        self._store.close()

    def _resolve_bridges(
        self, resp: httpx.Response, max_steps: int = 3
    ) -> httpx.Response:
        current = resp
        for _ in range(max_steps):
            if _is_login_page(current):
                return current
            form = _find_bridge_form(current.text)
            if not form:
                return current
            action = urllib.parse.urljoin(str(current.url), form.get("action"))
            payload = _form_payload(form)
            current = self._client.post(action, data=payload)
        return current

    def _submit_login(self, resp: httpx.Response) -> httpx.Response:
        soup = bs4.BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", id="loginForm")
        if not form:
            raise RuntimeError("找不到 SSO 登入表單")
        payload = _form_payload(form)
        payload.update(Username=self._account, Password=self._password)
        payload.setdefault("captcha", "")
        action = urllib.parse.urljoin(
            str(resp.url), form.get("action") or str(resp.url))
        return self._client.post(action, data=payload)


async def run_sync(func, *args):
    """在 executor 中執行同步函式，避免阻塞事件迴圈。

    Args:
        func: 要執行的同步函式。
        *args: 傳給該函式的參數。

    Returns:
        該函式的回傳值。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))
