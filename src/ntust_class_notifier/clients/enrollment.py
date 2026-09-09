"""台科大選課系統的登入、加選與 session 維持。

透過 SSO 登入後把 cookie 存進 config.data_dir()，供選課期間送出加選請求。
電選課（First/A06）與全校加退選（AddAndSub/B01）的端點與已選清單格式都不
一樣，各自寫成一個 Stage，select_course() 再依目前時段挑一個用。只有
ntust-notify 在設定了帳密時才會用到這一支。
"""

import asyncio
import collections.abc
import dataclasses
import functools
import logging
import pathlib
import re
import sqlite3
import threading
import urllib.parse

import bs4
import httpx

from ntust_class_notifier import config
from ntust_class_notifier.core import periods

logger = logging.getLogger(__name__)


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


COURSE_SELECTION_ROOT = "https://courseselection.ntust.edu.tw/"


# 電選課加選（First/A06）。
DEPT_SELECT_URL = "https://courseselection.ntust.edu.tw/First/A06/A06"


DEPT_SELECT_JOIN_URL = (
    "https://courseselection.ntust.edu.tw/First/A06/ExtraJoin"
)


COURSE_LIST_URL = "https://courseselection.ntust.edu.tw/First/A02/A02"


# 全校加退選（AddAndSub/B01）：課碼欄位送出後打 ExtraJoin，已選清單就在同
# 一頁的 #cartTable，所以主頁同時是 keepalive 與驗證用的頁面。
OPEN_SELECT_URL = "https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"


OPEN_SELECT_JOIN_URL = (
    "https://courseselection.ntust.edu.tw/AddAndSub/B01/ExtraJoin"
)


# 加退選頁面「選課清單」的表格 id。
CART_TABLE_ID = "cartTable"


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


# 送出加選時比照瀏覽器：選課系統的加選都是 jQuery 的 AJAX 呼叫。
JOIN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


# 課程已經在清單裡而沒有送出加選時，select_course() 回傳的說明。
ALREADY_ENROLLED = "已在選課清單中，沒有重複送出"


# 登入後的每一頁都有登出表單；session 過期時回的是 SSO 登入頁，沒有這個。
SIGNED_IN_MARKER = "Account/Logout"


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


def _looks_signed_in(resp: httpx.Response) -> bool:
    """判斷回應是不是登入狀態下的選課頁面。

    session 過期時選課系統不會回錯誤碼，而是回一個登入頁，所以只看狀態碼
    會把「已經被登出」誤判成正常頁面。

    Args:
        resp: 要判斷的回應。

    Returns:
        是登入後的選課頁面為 True。
    """
    return (
        resp.is_success
        and not _is_login_page(resp)
        and SIGNED_IN_MARKER in resp.text
    )


def _landed_on(resp: httpx.Response, url: str) -> bool:
    """判斷回應是不是「登入狀態下、而且真的停在要求的那一頁」。

    被導去首頁時 session 其實還在，登出表單也還在，但頁面上沒有想要的內
    容；只看登入狀態會把它當成一張空清單。

    Args:
        resp: 要判斷的回應。
        url: 原本要求的網址。

    Returns:
        兩個條件都成立為 True。
    """
    return (
        _looks_signed_in(resp)
        and resp.url.path.rstrip("/") == httpx.URL(url).path.rstrip("/")
    )


def _usable(resp: httpx.Response, url: str, exact_page: bool) -> bool:
    """判斷回應能不能用。

    Args:
        resp: 要判斷的回應。
        url: 原本要求的網址。
        exact_page: True 代表還要求停在這一頁。

    Returns:
        可以用為 True。
    """
    return _landed_on(resp, url) if exact_page else _looks_signed_in(resp)


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


def parse_dept_enrolled(html: str) -> set[str]:
    """從電選課頁面取出「已經選上」的課碼，不含志願序清單。

    志願序只是排隊，還沒選上，所以不能拿來擋加選——擋掉的話排了志願的課
    就永遠不會送出加選。

    Args:
        html: First/A02/A02 的 HTML。

    Returns:
        課碼集合，抓不到時為空集合。
    """
    return set(ENROLLED_COURSE_PATTERN.findall(html))


def parse_dept_submitted(html: str) -> set[str]:
    """取出已經選上、或已經排進志願序的課碼。

    電選課送出加選後就是進志願序，所以確認送出結果要看這一份。

    Args:
        html: First/A02/A02 的 HTML。

    Returns:
        課碼集合，抓不到時為空集合。
    """
    return parse_dept_enrolled(html) | set(WISH_LIST_PATTERN.findall(html))


def parse_open_enrolled(html: str) -> set[str]:
    """從加退選頁面的「選課清單」取出課碼。

    清單是 <table id="cartTable"> 底下的每一列 <tr class="data">，第一格就
    是課碼。加選成不成功只能靠這張表判斷——ExtraJoin 的回應是空的。

    Args:
        html: AddAndSub/B01/B01 的 HTML。

    Returns:
        課碼集合，找不到表格時為空集合。
    """
    soup = bs4.BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=CART_TABLE_ID)
    if not table:
        return set()

    courses = set()
    for row in table.find_all("tr", class_="data"):
        cells = row.find_all("td")
        if not cells:
            continue
        code = cells[0].get_text(strip=True).upper()
        if code:
            courses.add(code)
    return courses


@dataclasses.dataclass(frozen=True)
class Stage:
    """一個選課階段用到的端點與清單解析方式。

    Attributes:
        name: 中文階段名稱，寫進 log 與通知訊息。
        page_url: 階段主頁，keepalive 用它確認 session 還在。
        join_url: 送出加選的端點。
        list_url: 已選清單所在的頁面。
        list_marker: 清單頁面一定看得到的字串，用來確認拿到的真的是清單。
        parse_enrolled: 取出「已經選上」的課碼，送出加選前用它擋重送。
        parse_submitted: 取出「已選上或已送出」的課碼，送出後用它確認。
        guard_covers_submitted: 送出前的擋重送清單是否已經涵蓋「已送出」。
            False 代表呼叫端要自己再確認一次才安全（見電選課的志願序）。
    """

    name: str
    page_url: str
    join_url: str
    list_url: str
    list_marker: str
    parse_enrolled: collections.abc.Callable[[str], set[str]]
    parse_submitted: collections.abc.Callable[[str], set[str]]
    guard_covers_submitted: bool


DEPT_STAGE = Stage(
    name="電選課加選",
    page_url=DEPT_SELECT_URL,
    join_url=DEPT_SELECT_JOIN_URL,
    list_url=COURSE_LIST_URL,
    list_marker='class="table-cell"',
    parse_enrolled=parse_dept_enrolled,
    parse_submitted=parse_dept_submitted,
    # 擋重送只看「已選上」，不看志願序，所以會放行已經排進志願序的課。
    guard_covers_submitted=False,
)


OPEN_STAGE = Stage(
    name="全校加退選",
    page_url=OPEN_SELECT_URL,
    join_url=OPEN_SELECT_JOIN_URL,
    list_url=OPEN_SELECT_URL,
    list_marker=f'id="{CART_TABLE_ID}"',
    parse_enrolled=parse_open_enrolled,
    # 加退選只有一份選課清單，在裡面就是選上了，沒有志願序這回事。
    parse_submitted=parse_open_enrolled,
    guard_covers_submitted=True,
)


STAGES = {"dept": DEPT_STAGE, "open": OPEN_STAGE}


def read_enrolled(stage: Stage, html: str) -> set[str] | None:
    """取出「已經選上」的課碼，送出加選前用來擋重送。

    「讀不到清單」和「清單裡沒有這門課」必須分得開：把讀不到當成沒選上，
    就會在看不見狀態的情況下送出加選，那正是最危險的一種誤判。

    Args:
        stage: 目前的選課階段。
        html: 清單頁面的 HTML。

    Returns:
        已選上的課碼；頁面不是預期的清單頁時回傳 None。
    """
    return _read_list(stage, html, stage.parse_enrolled)


def read_submitted(stage: Stage, html: str) -> set[str] | None:
    """取出「已選上或已送出」的課碼，送出加選後用來確認結果。

    Args:
        stage: 目前的選課階段。
        html: 清單頁面的 HTML。

    Returns:
        課碼集合；頁面不是預期的清單頁時回傳 None。
    """
    return _read_list(stage, html, stage.parse_submitted)


def _read_list(
    stage: Stage,
    html: str,
    parser: collections.abc.Callable[[str], set[str]],
) -> set[str] | None:
    if stage.list_marker not in html:
        return None
    return parser(html)


def stage_for(period: str | None = None) -> Stage | None:
    """取得時段對應的選課階段。

    Args:
        period: 時段代碼——要送出加選就傳 periods.get_current_period()（收
            不收加選），只是要維持 session 則傳 get_scheduled_period()（日
            期落在哪一段）；None 代表看現在收不收加選。

    Returns:
        對應的 Stage；非選課時段時回傳 None。
    """
    return STAGES.get(period or periods.get_current_period())


def guard_covers_submitted(period: str | None = None) -> bool:
    """這個階段的擋重送清單夠不夠用。

    夠用的話呼叫端就不必在送出前自己多讀一次清單——同一頁重複讀兩遍只是
    白白多打學校系統一次。

    Args:
        period: 時段代碼，None 代表看現在收不收加選。

    Returns:
        擋重送清單已涵蓋「已送出」為 True；非選課時段時回傳 True（反正
        送不出去）。
    """
    stage = stage_for(period)
    return stage is None or stage.guard_covers_submitted


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
    登入。加選要走哪一組端點由目前時段決定，見 STAGES。
    """

    def __init__(self, student_id: str, password: str):
        self._account = student_id.strip().upper()
        self._password = password
        self._store = CookieStore()
        # keepalive 跑在背景執行緒，可能與加選同時發生；重新登入會清掉
        # cookie，撞在一起會讓進行中的加選失去 session。
        self._lock = threading.RLock()
        self._client = httpx.Client(
            http2=True,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": BROWSER_UA},
        )
        self._logged_in = False

    def login(self) -> bool:
        with self._lock:
            return self._login()

    def _login(self) -> bool:
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
        """訪問選課頁面以延長 session，過期就重新登入。

        Returns:
            session 仍有效（或重新登入成功）為 True。
        """
        # 維持 session 跟收不收加選是兩回事：選課期間的半夜也要一直碰選課
        # 頁，早上九點開放的那一刻才不會發現自己已經被登出。
        stage = stage_for(periods.get_scheduled_period())
        url = stage.page_url if stage else COURSE_SELECTION_ROOT
        with self._lock:
            try:
                # 非選課時段打的是首頁，而首頁本來就會轉去別的位址；這裡要
                # 求「停在指定頁面」的話會每次都判定失效而不斷重跑 SSO。
                return self._get_signed_in(url, exact_page=False) is not None
            except Exception as e:
                logger.error("Keepalive 失敗: %s", e)
                return False

    def _get_signed_in(
        self, url: str, exact_page: bool = True
    ) -> httpx.Response | None:
        """取得頁面；session 已經失效就重新登入後再試一次。

        Args:
            url: 要取得的頁面。
            exact_page: 是否要求最後真的停在這一頁。讀清單時一定要，被導
                去別頁會被當成一張空清單；keepalive 只需要知道還在登入狀
                態，停在哪一頁都算數。

        Returns:
            確定是登入狀態的回應；重新登入後仍拿不到時為 None。
        """
        resp = self._client.get(url)
        if _usable(resp, url, exact_page):
            self._store.save_from(self._account, self._client)
            logger.debug("Session 仍有效")
            return resp

        logger.warning("Session 已失效，重新登入...")
        self._logged_in = False
        if not self._login():
            return None

        resp = self._client.get(url)
        if _usable(resp, url, exact_page):
            return resp
        logger.error("重新登入後仍取不到 %s", url)
        return None

    def select_course(
        self, course_no: str, period: str | None = None
    ) -> tuple[bool, str]:
        """依目前時段送出加選請求。

        Args:
            course_no: 要加選的課程代碼。
            period: 指定時段，None 代表看今天落在哪個時段。

        Returns:
            (是否送出成功, 伺服器回應內容)。加退選的 ExtraJoin 回應是空字
            串，而且送出成功也不代表搶到名額，一律要再用 verify_enrolled()
            確認。課程本來就在清單裡時不會送出，回傳
            (True, ALREADY_ENROLLED)。
        """
        stage = stage_for(period)
        if stage is None:
            return False, "目前不是選課時段，沒有送出加選"

        with self._lock:
            if not self._login():
                return False, "登入失敗"

            # 這一次 GET 同時做兩件事：確認 session 還活著（失效就地重新登
            # 入），以及確認課程不在清單裡——已經選上的課再送一次
            # ExtraJoin，那門課就會從清單消失。
            page = self._get_signed_in(stage.list_url)
            if page is None:
                return False, "session 已失效且重新登入失敗，沒有送出加選"

            enrolled = read_enrolled(stage, page.text)
            if enrolled is None:
                logger.error("[%s] 拿到的不是選課清單頁面，不送出 %s",
                             stage.name, course_no)
                return False, "讀不到選課清單，沒有送出加選"
            if course_no.upper() in enrolled:
                logger.info("[%s] %s %s",
                            stage.name, course_no, ALREADY_ENROLLED)
                return True, ALREADY_ENROLLED

            try:
                resp = self._client.post(
                    stage.join_url,
                    data={"CourseNo": course_no, "type": "3"},
                    headers=JOIN_HEADERS | {"Referer": stage.page_url},
                )
                self._store.save_from(self._account, self._client)
                if _is_login_page(resp):
                    logger.error("[%s] 送出 %s 時被導回登入頁",
                                 stage.name, course_no)
                    return False, "送出加選時 session 失效，這次沒有送出成功"

                body = resp.text
                logger.info("[%s] 加選 %s 回應: %s | %s",
                            stage.name, course_no, resp.status_code,
                            body[:200])
                return resp.is_success, body
            except Exception as e:
                logger.error("加選 %s 發生錯誤: %s", course_no, e)
                return False, str(e)

    def submitted_courses(self, period: str | None = None) -> set[str] | None:
        """讀出目前「已選上或已送出」的課碼。

        「清單裡沒有」跟「讀不到清單」必須分得開：讀不到還照送的話，電選
        課階段已經排進志願序的課會被再一次 ExtraJoin 取消。

        Args:
            period: 指定時段，None 代表看現在收不收加選。

        Returns:
            課碼集合（電選課含志願序）；非選課時段或讀不到清單時回傳
            None。
        """
        stage = stage_for(period)
        if stage is None:
            return None

        with self._lock:
            if not self._login():
                return None
            try:
                resp = self._get_signed_in(stage.list_url)
                if resp is None:
                    logger.error("[%s] 取不到選課清單", stage.name)
                    return None
                submitted = read_submitted(stage, resp.text)
                if submitted is None:
                    logger.error("[%s] 拿到的不是選課清單頁面", stage.name)
                    return None
                return submitted
            except Exception as e:
                logger.error("讀取選課清單時發生錯誤: %s", e)
                return None

    def verify_enrolled(
        self, course_no: str, period: str | None = None
    ) -> bool:
        """檢查課程是否真的出現在已選清單中。

        Args:
            course_no: 要確認的課程代碼。
            period: 指定時段，None 代表看現在收不收加選。

        Returns:
            課程已在清單（電選課含志願序清單）中為 True。取不到清單時一律
            回 False，寧可回報「沒確認到」也不要謊報成功。
        """
        stage = stage_for(period)
        if stage is None:
            logger.error("目前不是選課時段，無法確認 %s", course_no)
            return False

        submitted = self.submitted_courses(period)
        if submitted is None:
            logger.error("[%s] 讀不到選課清單，無法確認 %s",
                         stage.name, course_no)
            return False

        found = course_no.upper() in submitted
        logger.info("[%s] 驗證加選結果: %s %s", stage.name, course_no,
                    "已在清單中" if found else "未在清單中")
        return found

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
