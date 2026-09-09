"""clients.enrollment：加選走哪一組端點、已選清單怎麼認。

不連線，全部用 httpx 的 MockTransport 假裝選課系統。HTML 片段照抄
AddAndSub/B01/B01 真實頁面的結構。
"""

import httpx
import pytest

from ntust_class_notifier.clients import enrollment

# 登入後的每一頁都有這個登出表單，session 還在不在就是看它。
SIGNED_IN_HTML = """
<form action="https://courseselection.ntust.edu.tw/Account/Logout"
      id="logoutForm" method="post"></form>
"""

# 被登出時選課系統回的是登入頁，沒有登出表單。
SIGNED_OUT_HTML = """<html><body>請先登入</body></html>"""

# 加退選頁面的「選課清單」；<thead> 沒有收尾是原始頁面就這樣寫。
CART_TABLE_HTML = """
<div class="table" id="draggable">
  <div class="table-row">
    <div class="table-cell">課碼</div><div class="table-cell">課程名稱</div>
  </div>
  <div class="table-row">
    <div class="table-cell">FB3607701</div>
    <div class="table-cell">財務報表分析</div>
    <div class="table-cell"><span class="addbtn">加選</span></div>
  </div>
</div>
<table id="cartTable" style="width:70%">
  <thead>
    <tr>
      <td style="border: 1px solid;text-align:center"> 課碼 </td>
      <td style="border: 1px solid;text-align:center"> 課程名稱 </td>
      <td style="border: 1px solid;text-align:center"> 退選 </td>
    </tr>
  <tbody>
    <tr class="data" style="border: 1px solid;text-align:center">
      <td style="border: 1px solid;text-align:center"> CS2002302 </td>
      <td> 資料結構 </td>
      <td><span class="delbtn btn btn-xs btn-warning">退選</span></td>
    </tr>
    <tr class="data" style="border: 1px solid;text-align:center">
      <td style="border: 1px solid;text-align:center"> TCG175302 </td>
      <td> 技術與社會：從理論框架到永續實踐 </td>
      <td><span class="delbtn btn btn-xs btn-warning">退選</span></td>
    </tr>
  </tbody>
</table>
"""


class _Server:
    """記錄收到什麼請求的假選課系統。

    Attributes:
        requests: 收到的每一個請求。
        signed_out_gets: 還要再回幾次「已被登出」的頁面。
    """

    def __init__(self, signed_out_gets: int = 0) -> None:
        self.requests: list[httpx.Request] = []
        self.signed_out_gets = signed_out_gets

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("ExtraJoin"):
            # 加退選的 ExtraJoin 回 200 但沒有內容，這裡照著回。
            return httpx.Response(200, text="")
        if self.signed_out_gets:
            self.signed_out_gets -= 1
            return httpx.Response(200, text=SIGNED_OUT_HTML)
        return httpx.Response(200, text=SIGNED_IN_HTML + CART_TABLE_HTML)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


@pytest.fixture
def server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> _Server:
    """假選課系統，並把 cookie 資料庫導到暫存目錄。

    Args:
        monkeypatch: 用來換掉 httpx.Client 與資料目錄。
        tmp_path: pytest 的暫存目錄。

    Returns:
        會記錄請求的假伺服器。
    """
    monkeypatch.setenv("NTUST_DATA_DIR", str(tmp_path))
    handler = _Server()
    real_client = httpx.Client

    def fake_client(**kwargs) -> httpx.Client:
        kwargs.pop("http2", None)  # MockTransport 不吃 http2。
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(enrollment.httpx, "Client", fake_client)
    return handler


def _selector() -> enrollment.CourseSelector:
    selector = enrollment.CourseSelector("B11215000", "pw")
    assert selector.login()  # 假伺服器不會回登入頁，登入必定成功。
    return selector


def test_open_period_posts_to_add_and_sub_endpoint(server: _Server) -> None:
    selector = _selector()

    success, body = selector.select_course("EE3005301", "open")

    join = server.requests[-1]
    assert (success, body) == (True, "")
    assert str(join.url) == enrollment.OPEN_SELECT_JOIN_URL
    assert join.content == b"CourseNo=EE3005301&type=3"
    assert join.headers["X-Requested-With"] == "XMLHttpRequest"
    selector.close()


def test_dept_period_still_posts_to_first_endpoint(server: _Server) -> None:
    selector = _selector()

    selector.select_course("EE3005301", "dept")

    assert str(server.requests[-1].url) == enrollment.DEPT_SELECT_JOIN_URL
    selector.close()


def test_a_course_already_in_the_list_is_never_sent_again(
    server: _Server,
) -> None:
    selector = _selector()

    success, body = selector.select_course("TCG175302", "open")

    # 已選上的課再送一次 ExtraJoin 會被系統當成取消，等於自己退掉搶到的課。
    assert (success, body) == (True, enrollment.ALREADY_ENROLLED)
    assert not any(path.endswith("ExtraJoin") for path in server.paths)
    selector.close()


def test_outside_selection_period_sends_nothing(server: _Server) -> None:
    selector = _selector()
    before = len(server.requests)

    success, body = selector.select_course("TCG175302", "unknown")

    assert success is False
    assert "不是選課時段" in body
    assert len(server.requests) == before  # 完全沒有送出請求。
    selector.close()


def test_verify_reads_the_add_and_sub_cart_table(server: _Server) -> None:
    selector = _selector()

    assert selector.verify_enrolled("TCG175302", "open") is True
    assert selector.verify_enrolled("tcg175302", "open") is True  # 大小寫
    assert server.paths[-1] == "/AddAndSub/B01/B01"
    selector.close()


def test_verify_ignores_courses_not_in_the_cart_table(
    server: _Server,
) -> None:
    selector = _selector()

    # FB3607701 只在「待選清單」，還沒加選，不能算數。
    assert selector.verify_enrolled("FB3607701", "open") is False
    assert selector.verify_enrolled("CS9999999", "open") is False
    selector.close()


def test_parse_open_enrolled_takes_only_the_cart_table() -> None:
    assert enrollment.parse_open_enrolled(CART_TABLE_HTML) == {
        "CS2002302", "TCG175302",
    }


def test_parse_open_enrolled_survives_a_page_without_the_table() -> None:
    assert enrollment.parse_open_enrolled("<html><body>維護中</body></html>") \
        == set()


def test_stage_for_maps_each_period() -> None:
    assert enrollment.stage_for("open") is enrollment.OPEN_STAGE
    assert enrollment.stage_for("dept") is enrollment.DEPT_STAGE
    assert enrollment.stage_for("unknown") is None


def test_an_expired_session_is_renewed_before_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("NTUST_DATA_DIR", str(tmp_path))
    handler = _Server()
    real_client = httpx.Client
    monkeypatch.setattr(
        enrollment.httpx, "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "http2"}))
    selector = enrollment.CourseSelector("B11215000", "pw")
    assert selector.login()
    handler.signed_out_gets = 1  # 下一次拿清單時已經被登出。

    success, body = selector.select_course("EE3005301", "open")

    # 重新登入後照樣送出，不能因為 session 過期就默默什麼都沒做。
    assert (success, body) == (True, "")
    assert any(path.endswith("ExtraJoin") for path in handler.paths)
    selector.close()


def test_nothing_is_sent_when_the_session_cannot_be_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("NTUST_DATA_DIR", str(tmp_path))
    handler = _Server(signed_out_gets=99)
    real_client = httpx.Client
    monkeypatch.setattr(
        enrollment.httpx, "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "http2"}))
    selector = enrollment.CourseSelector("B11215000", "pw")

    success, body = selector.select_course("EE3005301", "open")

    # 登不回去就不能送——這時清單讀不到，會誤判成「還沒選上」而重複送出。
    assert success is False
    assert "session" in body
    assert not any(path.endswith("ExtraJoin") for path in handler.paths)
    assert selector.verify_enrolled("EE3005301", "open") is False
    assert selector.keepalive() is False
    selector.close()


def _selector_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path, handler: _Server
) -> enrollment.CourseSelector:
    """用指定的假伺服器建一個已登入的 client。

    Args:
        monkeypatch: 替換工具。
        tmp_path: cookie 資料庫的位置。
        handler: 假伺服器。

    Returns:
        已登入的 client。
    """
    monkeypatch.setenv("NTUST_DATA_DIR", str(tmp_path))
    real_client = httpx.Client
    monkeypatch.setattr(
        enrollment.httpx, "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "http2"}))
    return enrollment.CourseSelector("B11215000", "pw")


def test_an_unreadable_list_never_becomes_a_blind_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class _HomePage(_Server):
        """登入狀態正常，但被導回首頁——沒有選課清單。"""

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("ExtraJoin"):
                return httpx.Response(200, text="")
            return httpx.Response(
                200, text=SIGNED_IN_HTML + "<h1>首頁</h1>")

    handler = _HomePage()
    selector = _selector_serving(monkeypatch, tmp_path, handler)

    success, body = selector.select_course("TCG175302", "open")

    # 讀不到清單就等於看不見自己選了什麼，這時送出可能反而把課弄掉。
    assert success is False
    assert "讀不到選課清單" in body
    assert not any(path.endswith("ExtraJoin") for path in handler.paths)
    assert selector.verify_enrolled("TCG175302", "open") is False
    selector.close()


def test_read_enrolled_separates_empty_from_unreadable() -> None:
    empty_table = SIGNED_IN_HTML + (
        '<table id="cartTable"><thead><tr><td>課碼</td></tr></table>'
    )
    stage = enrollment.OPEN_STAGE

    # 空清單是「一門都沒選」，首頁是「不知道選了什麼」，兩者不能混為一談。
    assert enrollment.read_enrolled(stage, empty_table) == set()
    assert enrollment.read_enrolled(stage, "<h1>首頁</h1>") is None


# 電選課頁面：CS2002302 已經選上，EE3005301 只排進志願序。
DEPT_LIST_HTML = SIGNED_IN_HTML + """
<div class="table-row"><div class="table-cell">CS2002302</div></div>
<table>
  <tr class="data"><td>1</td><td>EE3005301</td><td>資料結構</td></tr>
</table>
"""


def test_a_wish_listed_course_is_not_treated_as_enrolled() -> None:
    stage = enrollment.DEPT_STAGE

    # 志願序只是排隊，還沒選上。拿它擋加選的話，排了志願的課永遠不會送出。
    assert enrollment.read_enrolled(stage, DEPT_LIST_HTML) == {"CS2002302"}
    assert enrollment.read_submitted(stage, DEPT_LIST_HTML) == {
        "CS2002302", "EE3005301",
    }


def test_keepalive_accepts_any_signed_in_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class _Redirects(_Server):
        """登入狀態正常，但首頁一律轉去 /Home/Index。"""

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200, text=SIGNED_IN_HTML + "<h1>首頁</h1>",
                request=httpx.Request("GET", "https://x.tw/Home/Index"))

    handler = _Redirects()
    selector = _selector_serving(monkeypatch, tmp_path, handler)
    monkeypatch.setattr(
        enrollment.periods, "get_current_period", lambda *a: "unknown")

    # 非選課時段打的是首頁，本來就會轉址；要求停在原網址的話會每 3 分鐘
    # 判定失效並重跑一次 SSO，整晚都在重登。
    assert selector.keepalive() is True
    selector.close()
