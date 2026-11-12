"""우리집 물건관리 | Python 3.11+ | streamlit run app.py
GitHub에 app.py와 requirements.txt 업로드 후 Streamlit Community Cloud 배포.
GitHub Pages는 지원하지 않습니다.
선택 Secrets: DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
외부 PostgreSQL 미설정 시 SQLite: Cloud 재시작/재배포 시 유실 가능.
로그인·가족 계정 기능 없음: 민감한 정보를 입력하지 말고 배포 접근 권한을 제한하세요.
목적별 네 메뉴: 공동구매 / 소진 예측 / 중복 구매 확인 / 보관함 QR 생성.
기존 버전과 별도 테이블을 사용하므로 이전 데이터가 자동 표시되지는 않습니다.
"""
from __future__ import annotations
import io
import re
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit
import qrcode
import os
import sqlite3
import statistics
import uuid
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="우리집 물건관리", page_icon="🏠", layout="centered")
TZ = ZoneInfo("Asia/Seoul")
CATEGORIES = ["생필품", "식품", "생활물품", "가전·필터", "기타"]


def today():
    return datetime.now(TZ).date()


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def config(key):
    try:
        return str(st.secrets.get(key, os.environ.get(key, "")))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(key, "")


PG = bool(config("DATABASE_URL"))


@contextmanager
def db():
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(config("DATABASE_URL"), row_factory=dict_row, connect_timeout=12)
    else:
        conn = sqlite3.connect("home_inventory.sqlite3", timeout=20)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sql(conn, query, args=()):
    return conn.execute(query.replace("?", "%s") if PG else query, args)


def read(query, args=()):
    with db() as conn:
        return [dict(r) for r in sql(conn, query, args).fetchall()]


def initialize():
    blob = "BYTEA" if PG else "BLOB"
    with db() as conn:
        sql(conn, f"""CREATE TABLE IF NOT EXISTS simple_items(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL,
            category TEXT NOT NULL, location TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>=0), unit TEXT NOT NULL,
            expiry TEXT, interval_days INTEGER NOT NULL,
            last_maintenance TEXT, memo TEXT NOT NULL, photo {blob})""")
        sql(conn, """CREATE TABLE IF NOT EXISTS purpose_orders(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>0), status TEXT NOT NULL,
            buyer TEXT NOT NULL, created TEXT NOT NULL)""")
        sql(conn, """CREATE TABLE IF NOT EXISTS simple_cycles(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            started TEXT NOT NULL, finished TEXT)""")


def lock(conn, item_id):
    if not PG:
        sql(conn, "BEGIN IMMEDIATE")
    row = sql(conn, "SELECT * FROM simple_items WHERE id=?" + (" FOR UPDATE" if PG else ""), (item_id,)).fetchone()
    if not row:
        raise ValueError("물건을 찾을 수 없습니다. 다시 선택하세요.")
    return dict(row)


def change_stock(item_id, delta):
    with db() as conn:
        item = lock(conn, item_id)
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("사용 중인 주기가 있어요. 소진 예측에서 완료하거나 취소한 뒤 수량을 변경하세요.")
        if item["qty"] + delta < 0:
            raise ValueError("현재 수량보다 많이 차감할 수 없습니다.")
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (delta, item_id))


def start_cycle(item_id):
    with db() as conn:
        item = lock(conn, item_id)
        if item["qty"] < 1:
            raise ValueError("재고가 있어야 사용을 시작할 수 있어요.")
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("이미 사용 중입니다.")
        sql(conn, "INSERT INTO simple_cycles VALUES(?,?,?,?)", (uuid.uuid4().hex, item_id, now(), None))


def end_cycle(item_id, cancel=False):
    with db() as conn:
        item = lock(conn, item_id)
        cycle = sql(conn, "SELECT * FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone()
        if not cycle:
            raise ValueError("사용 중인 주기가 없어요.")
        if cancel:
            sql(conn, "DELETE FROM simple_cycles WHERE id=?", (cycle["id"],))
        else:
            if item["qty"] < 1:
                raise ValueError("차감할 재고가 없어요.")
            sql(conn, "UPDATE simple_cycles SET finished=? WHERE id=?", (now(), cycle["id"]))
            sql(conn, "UPDATE simple_items SET qty=qty-1 WHERE id=?", (item_id,))


def predict(cycles):
    completed = sorted([c for c in cycles if c["finished"]], key=lambda c: c["finished"])
    durations = [(datetime.fromisoformat(c["finished"]).date() - datetime.fromisoformat(c["started"]).date()).days for c in completed]
    durations = [d for d in durations if d > 0]
    current = next((c for c in cycles if not c["finished"]), None)
    median = statistics.median(durations[-6:]) if len(durations) >= 2 else None
    due = datetime.fromisoformat(current["started"]).date() + timedelta(days=round(median)) if current and median is not None else None
    return len(durations), median, current, due


def navigate(page):
    st.session_state["page"] = page


def perform(fn, message):
    try:
        fn()
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("저장하지 못했습니다. DB 연결이나 사진 형식을 확인하고 다시 시도하세요.")
    else:
        st.session_state["notice"] = message
        st.rerun()


def choose(items, key):
    if not items:
        st.info("등록된 물건이 없어요. 홈의 ‘물건 등록’에서 먼저 등록하세요.")
        return None
    lookup = {i["id"]: i for i in items}
    options = list(lookup)
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]
    iid = st.selectbox("물건 선택", options, format_func=lambda x: f'{lookup[x]["name"]} · {lookup[x]["location"]}', key=key)
    return lookup[iid]


def as_date(value):
    return date.fromisoformat(value) if value else None


def show_registration(items):
    with st.form("register", clear_on_submit=True):
        name = st.text_input("물건 이름 *", max_chars=80)
        aliases = st.text_input("검색 별칭", placeholder="예: 건전지, 배터리", max_chars=200)
        category = st.selectbox("물건 분류", CATEGORIES)
        location = st.text_input("보관 장소 *", placeholder="예: 다용도실 선반 두 번째 칸", max_chars=120)
        qty = st.number_input("현재 수량", min_value=0, max_value=9999, value=1)
        unit = st.selectbox("단위", ["개", "팩", "병", "봉", "상자", "롤"])
        photo = st.file_uploader("사진 (선택, 2MB 이하)", type=["jpg", "jpeg", "png"])
        memo = st.text_area("메모", max_chars=2000)
        submit = st.form_submit_button("등록", use_container_width=True)
    st.caption("보관 장소에는 보관함 이름을 입력하세요. 예: 주방 2번 서랍. 같은 장소 이름끼리 QR로 묶입니다.")
    if submit:
        def create():
            if not name.strip() or not location.strip():
                raise ValueError("이름과 보관 장소를 입력하세요.")
            data = None
            if photo:
                if photo.size > 2 * 1024 * 1024:
                    raise ValueError("사진은 2MB 이하로 올려 주세요.")
                image = ImageOps.exif_transpose(Image.open(photo))
                image.thumbnail((1000, 1000))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=80)
                data = buffer.getvalue()
            with db() as conn:
                if sql(conn, "SELECT id FROM simple_items WHERE LOWER(name)=LOWER(?) AND LOWER(location)=LOWER(?)", (name.strip(), location.strip())).fetchone():
                    raise ValueError("같은 장소에 같은 이름이 있어요. 기존 물건을 수정하거나 이름을 구분하세요.")
                sql(conn, "INSERT INTO simple_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, name.strip(), aliases.strip(), category, location.strip(), qty, unit, None, 0, None, memo, data))
        perform(create, "등록했습니다.")


def show_prediction(items):
    item = choose(items, "predict_item")
    if not item:
        return
    cycles = read("SELECT * FROM simple_cycles WHERE item_id=?", (item["id"],))
    count, median, current, due = predict(cycles)
    st.caption("현재 사용 중인 1개의 소진 시점을 예상합니다. 재고 전체의 소진일은 아닙니다.")
    if median is None:
        st.info(f"예측 데이터 부족: 유효한 사용 완료 주기 {count}/2회")
    elif due:
        st.success(f"현재 제품 소진 예상일: {due}")
        st.caption(f"최근 최대 6회의 사용 기간 중앙값: {median:g}일")
    else:
        st.info("사용 시작을 누르면 예상 소진일이 표시됩니다.")
    if current:
        st.write(f'사용 시작일: {current["started"][:10]}')
        if st.button("다 썼어요 · 재고 1개 차감", use_container_width=True):
            perform(lambda: end_cycle(item["id"]), "사용 완료와 재고 차감을 반영했습니다.")
        if st.button("사용 시작 취소", use_container_width=True):
            perform(lambda: end_cycle(item["id"], cancel=True), "주기를 취소했습니다. 재고는 바뀌지 않았어요.")
    elif st.button("사용 시작", disabled=item["qty"] < 1, use_container_width=True):
        perform(lambda: start_cycle(item["id"]), "사용을 시작했습니다. 다 쓸 때 재고에서 차감됩니다.")
    st.caption("한 품목당 한 번에 하나만 기록합니다. 같은 날 시작·완료한 주기는 학습에서 제외합니다. 별도 활동 기록·통계 화면은 없습니다.")


def normalize(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def matches(item, query):
    target = normalize(query)
    return bool(target) and any(target in normalize(v) for v in [item["name"]] + item["aliases"].replace("，", ",").split(","))


def add_order(item_id, quantity):
    with db() as conn:
        lock(conn, item_id)
        if sql(conn, "SELECT id FROM purpose_orders WHERE item_id=? AND status<>?", (item_id, "도착")).fetchone():
            raise ValueError("이미 구매 예정이거나 주문한 물건입니다. 기존 목록을 확인하세요.")
        sql(conn, "INSERT INTO purpose_orders VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, item_id, quantity, "구매 예정", "", now()))


def advance_order(order_id, buyer=""):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] == "구매 예정":
            if not buyer.strip():
                raise ValueError("구매 담당자의 별명을 입력하세요.")
            sql(conn, "UPDATE purpose_orders SET status=?,buyer=? WHERE id=?", ("주문 완료", buyer.strip(), order_id))
        elif order["status"] == "주문 완료":
            # This action is explicit; stale clicks never change a different step.
            raise ValueError("이미 주문됐어요. 최신 목록에서 도착 확인을 누르세요.")
        else:
            raise ValueError("이미 도착 처리된 항목입니다.")


def receive_order(order_id):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] != "주문 완료":
            raise ValueError("이미 처리됐거나 아직 주문 전입니다. 새로고침하세요.")
        sql(conn, "UPDATE purpose_orders SET status=? WHERE id=?", ("도착", order_id))
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (order["qty"], order["item_id"]))


def show_purchases(items):
    st.write("살 것을 함께 정하고, 누가 주문했는지 확인하세요.")
    with st.expander("구매할 물건 추가"):
        item = choose(items, "purchase_item")
        if item:
            st.caption(f'집에 {item["qty"]}{item["unit"]} · 위치: {item["location"]}')
            if item["qty"] > 0:
                st.warning("집에 재고가 있어요. 더 구매할지 확인하세요.")
            amount = st.number_input("구매 수량", min_value=1, max_value=999, step=1)
            if st.button("공동구매 목록에 추가", use_container_width=True):
                perform(lambda: add_order(item["id"], amount), "구매 목록에 추가했습니다.")
    orders = read("SELECT * FROM purpose_orders WHERE status<>? ORDER BY created DESC", ("도착",))
    item_map = {i["id"]: i for i in items}
    if not orders:
        st.info("진행 중인 구매가 없습니다.")
    for order in orders:
        item = item_map.get(order["item_id"])
        if not item:
            continue
        with st.container(border=True):
            st.subheader(item["name"])
            st.write(f'{order["qty"]}{item["unit"]} 구매 · {order["status"]}')
            st.caption(f'현재 재고 {item["qty"]}{item["unit"]} · {item["location"]}')
            if order["status"] == "구매 예정":
                buyer = st.text_input("주문 담당자 별명", key="buyer_" + order["id"], max_chars=30)
                if st.button("내가 주문했어요", key="order_" + order["id"], use_container_width=True):
                    perform(lambda oid=order["id"], b=buyer: advance_order(oid, b), "주문 상태를 공유했습니다.")
                if st.button("구매 예정 취소", key="cancel_" + order["id"]):
                    def cancel(order=order):
                        with db() as conn:
                            lock(conn, order["item_id"])
                            cur = sql(conn, "DELETE FROM purpose_orders WHERE id=? AND status=?", (order["id"], "구매 예정"))
                            if cur.rowcount != 1:
                                raise ValueError("상태가 변경됐어요. 새로고침하세요.")
                    perform(cancel, "구매 예정을 취소했습니다.")
            else:
                st.write(f'주문 담당: {order["buyer"]}')
                if st.button("도착 확인 · 재고 반영", key="receive_" + order["id"], use_container_width=True):
                    perform(lambda oid=order["id"]: receive_order(oid), "도착 수량을 재고에 반영했습니다.")
    st.caption("결제·배송 연동은 없으며 가족이 상태를 직접 변경하는 공유 구매 목록입니다.")


def show_duplicates(items):
    st.write("사기 전에, 집에 있는지와 이미 주문했는지를 확인하세요.")
    query = st.text_input("구매하려는 물건", placeholder="예: 세제, 배터리")
    if not query.strip():
        st.info("물건 이름이나 등록한 별칭을 입력하세요.")
        return
    found = [i for i in items if matches(i, query)]
    if not found:
        st.info("등록된 이름·별칭에서 찾지 못했어요. 집에 없다는 의미는 아닙니다.")
    for item in found:
        orders = read("SELECT * FROM purpose_orders WHERE item_id=? AND status<>?", (item["id"], "도착"))
        with st.container(border=True):
            st.subheader(item["name"])
            st.write(f'보관 장소: {item["location"]}')
            if item["qty"] > 0:
                st.warning(f'현재 {item["qty"]}{item["unit"]} 있어요. 추가 구매가 필요한지 확인하세요.')
            else:
                st.info("등록된 재고는 0입니다.")
            for order in orders:
                st.warning(f'{order["status"]} {order["qty"]}{item["unit"]} · 담당 {order["buyer"] or "미정"}')
            if not orders:
                st.caption("진행 중인 구매 없음")
    st.caption("등록 데이터 기준의 이름·별칭 검색이며, 상품 자동 인식이나 완전한 중복 판정은 아닙니다.")


def qr_url(base, location):
    parts = urlsplit(base.strip())
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("https://로 시작하는 실제 앱 배포 주소를 입력하세요.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"box": location}), ""))


def show_qr(items):
    st.write("보관함 QR을 만들어 붙이면, 스캔할 때 그 안의 물건 목록이 열립니다.")
    locations = sorted(set(i["location"] for i in items))
    if not locations:
        st.info("물건을 먼저 등록해 보관 장소를 지정하세요.")
        return
    incoming = st.query_params.get("box", "")
    if incoming in locations and st.session_state.get("qr_incoming") != incoming:
        st.session_state["qr_place"] = incoming
        st.session_state["qr_incoming"] = incoming
    if st.session_state.get("qr_place") not in locations:
        st.session_state["qr_place"] = locations[0]
    place = st.selectbox("보관함 / 보관 장소", locations, key="qr_place")
    with st.container(border=True):
        st.subheader("📦 " + place)
        for item in items:
            if item["location"] == place:
                st.write(f'**{item["name"]}** · {item["qty"]}{item["unit"]}')
                if item["photo"]:
                    st.image(bytes(item["photo"]), width=160)
    base = st.text_input("앱 배포 주소", value=config("APP_URL"), placeholder="https://your-app.streamlit.app")
    if base:
        try:
            link = qr_url(base, place)
            buffer = io.BytesIO()
            qrcode.make(link).save(buffer, format="PNG")
            st.image(buffer.getvalue(), width=240, caption=place + " QR")
            st.download_button("보관함 QR 이미지 받기", buffer.getvalue(), "storage_qr.png", "image/png", use_container_width=True)
            st.link_button("QR 연결 확인", link)
        except ValueError as exc:
            st.error(str(exc))
    st.caption("휴대폰 기본 카메라로 스캔하세요. 장소 이름을 바꾸면 QR을 다시 발급하세요. QR은 접근 권한을 제한하지 않습니다.")


def support_registration(items):
    with st.expander("처음 쓰거나 새 물건이 있나요? 물건 등록"):
        show_registration(items)


def main():
    st.markdown('''<style>
    .block-container{max-width:960px;padding-top:2rem;}
    .stApp{background:#f6f7fb;}
    h1,h2,h3{color:#192b42;}
    div.st-key-tile_purchase button,div.st-key-tile_predict button,
    div.st-key-tile_duplicate button,div.st-key-tile_qr button{
        min-height:170px!important;width:100%;border:0!important;border-radius:24px!important;
        padding:24px!important;box-shadow:0 5px 16px #172b4010;
        transition:transform .15s ease;}
    div.st-key-tile_purchase button p,div.st-key-tile_predict button p,
    div.st-key-tile_duplicate button p,div.st-key-tile_qr button p{
        font-size:25px!important;font-weight:750!important;white-space:pre-line;}
    div.st-key-tile_purchase button{background:#dceaff!important;color:#153f77!important;}
    div.st-key-tile_predict button{background:#e5defa!important;color:#4b2c7c!important;}
    div.st-key-tile_duplicate button{background:#d9f2e5!important;color:#145437!important;}
    div.st-key-tile_qr button{background:#ffead3!important;color:#75400e!important;}
    div.st-key-tile_purchase button:hover,div.st-key-tile_predict button:hover,
    div.st-key-tile_duplicate button:hover,div.st-key-tile_qr button:hover{transform:translateY(-3px);}
    button:focus-visible{outline:3px solid #193c67!important;outline-offset:3px;}
    </style>''', unsafe_allow_html=True)
    try:
        initialize()
        items = read("SELECT * FROM simple_items ORDER BY name,location")
    except Exception:
        st.error("DB에 연결할 수 없습니다. DATABASE_URL과 접속 설정을 확인하세요.")
        return
    incoming = st.query_params.get("box", "")
    if incoming and st.session_state.get("processed_box") != incoming:
        st.session_state["page"] = "보관함 QR 생성"
        st.session_state["processed_box"] = incoming
    page = st.session_state.get("page", "홈")
    if page == "홈":
        st.title("🏠 우리집 생활 도우미")
        st.write("지금 무엇을 하고 싶으세요?")
        first, second = st.columns(2, gap="large")
        tiles = [(first,"purchase","🛒 공동구매","공동구매"),(second,"predict","📉 소진 예측","소진 예측"),(first,"duplicate","🔎 중복 구매 확인","중복 구매 확인"),(second,"qr","📦 보관함 QR 생성","보관함 QR 생성")]
        for column, key, text, target in tiles:
            with column:
                with st.container(key="tile_" + key):
                    st.button(text, key="go_" + key, on_click=navigate, args=(target,), use_container_width=True)
        st.caption("원하는 박스를 누르면 해당 기능만 열립니다.")
        st.caption("외부 DB 연결 중" if PG else "데모 저장 모드 · Cloud 재시작 시 유실 가능. 영구 보관은 DATABASE_URL을 설정하세요.")
        st.caption("로그인 없는 앱입니다. 배포 접근 권한을 제한하고 민감한 개인정보는 등록하지 마세요.")
        return
    st.button("← 목적 선택으로", on_click=navigate, args=("홈",))
    st.title(page)
    if st.button("최신 데이터 새로고침"):
        st.rerun()
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    handlers = {"공동구매":show_purchases,"소진 예측":show_prediction,"중복 구매 확인":show_duplicates,"보관함 QR 생성":show_qr}
    if page in handlers:
        handlers[page](items)
        st.divider()
        support_registration(items)
    else:
        navigate("홈")
        st.rerun()


if __name__ == "__main__":
    main()
