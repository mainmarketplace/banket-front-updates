# -*- coding: utf-8 -*-
"""
Банкетный фронт «Любава» — автономный сервер.
Хостит веб-фронт в локальной сети, хранит банкеты в SQLite,
синхронизирует цены из iiko и создаёт акты реализации по iiko API.
Только стандартная библиотека Python — никаких зависимостей.

Запуск:  py server.py
"""
import json, sqlite3, hashlib, threading, time, datetime, os, sys, re
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket as _socket
# Только IPv4: на Windows с VPN/без IPv6-маршрута Python иначе виснет на AAAA-адресах (браузер умеет откат)
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    v4 = [r for r in res if r[0] == _socket.AF_INET]
    return v4 or res
_socket.getaddrinfo = _ipv4_getaddrinfo

FROZEN = getattr(sys, "frozen", False)
# server.py всегда лежит рядом с BanketFront.exe (exe — тонкая оболочка, запускающая этот файл)
BASE = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(BASE, "version.txt")
UPDATABLE = ("server.py", "front.html", "front.ico", "favicon.png")
OWNER_ID = "5d1a33e6-028e-4f2d-9939-0e764f515581"  # Хозяйкин А. В. — только он выдаёт PIN-коды
UPDATING = [False]  # идёт установка обновления — code_watcher не перезапускает сам
ICON_PATH = os.path.join(BASE, "front.ico")
CONFIG_PATH = os.path.join(BASE, "config.json")
DB_PATH = os.path.join(BASE, "banket.db")
FRONT_PATH = os.path.join(BASE, "front.html")
LOG_DIR = os.path.join(BASE, "logs")
SEED_DIR = os.path.join(BASE, "seeds")
os.makedirs(LOG_DIR, exist_ok=True)

def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(os.path.join(LOG_DIR, "server.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_config():
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # -sig: допускаем BOM от PowerShell
        return json.load(f)

CFG = load_config()

# ---------------- хранилище документов (как в облачной версии) ----------------
_dblock = threading.Lock()
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, json TEXT, updated TEXT)")
    return c

def doc_get(path):
    with _dblock:
        c = _conn()
        row = c.execute("SELECT json FROM docs WHERE path=?", (path,)).fetchone()
        c.close()
    return json.loads(row[0]) if row else None

def doc_set(path, data):
    with _dblock:
        c = _conn()
        c.execute("REPLACE INTO docs (path,json,updated) VALUES (?,?,?)",
                  (path, json.dumps(data, ensure_ascii=False), datetime.datetime.now().isoformat()))
        c.commit(); c.close()

def doc_del(path):
    with _dblock:
        c = _conn()
        c.execute("DELETE FROM docs WHERE path=?", (path,))
        c.commit(); c.close()

def col_get(prefix):
    with _dblock:
        c = _conn()
        rows = c.execute("SELECT path,json FROM docs WHERE path LIKE ?", (prefix + "/%",)).fetchall()
        c.close()
    out = []
    for p, j in rows:
        rest = p[len(prefix) + 1:]
        if "/" in rest:  # только прямые документы коллекции
            continue
        out.append({"id": rest, "data": json.loads(j)})
    return out

def seed_if_empty():
    if doc_get("config/structure"):
        return
    log("База пустая — загружаю стартовые данные из seeds/")
    m = {"structure.json": "config/structure",
         "menu_gogolya217.json": "menus/gogolya217",
         "menu_zhukovskogo.json": "menus/zhukovskogo",
         "menu_zorge12.json": "menus/zorge12",
         "menu_zorge14.json": "menus/zorge14",
         "menu_stanislavskogo34.json": "menus/stanislavskogo34"}
    for fn, path in m.items():
        fp = os.path.join(SEED_DIR, fn)
        if os.path.exists(fp):
            with open(fp, encoding="utf-8-sig") as f:
                doc_set(path, json.load(f))
            log("  залито: " + path)

# ---------------- клиент iiko API ----------------
class Iiko:
    def __init__(self, cfg):
        self.url = cfg["iiko_url"].rstrip("/")
        self.login = cfg["iiko_login"]
        self.password = cfg["iiko_password"]
        self.key = None

    def _req(self, path, params=None, data=None, headers=None, method=None):
        q = dict(params or {})
        if self.key:
            q["key"] = self.key
        url = self.url + path + ("?" + urllib.parse.urlencode(q) if q else "")
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # iiko вместе с кодом отказа присылает объяснение — сохраняем его, иначе причину не узнать
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
            raise Exception("iiko ответил отказом %s на %s%s" % (e.code, path, (": " + body[:600]) if body else " (без объяснения)"))

    def auth(self):
        if not self.login or not self.password:
            raise RuntimeError("В config.json не заполнены iiko_login / iiko_password")
        sha = hashlib.sha1(self.password.encode("utf-8")).hexdigest()
        self.key = None
        self.key = self._req("/resto/api/auth", {"login": self.login, "pass": sha}).strip()
        return self.key

    def logout(self):
        if self.key:
            try:
                self._req("/resto/api/logout")
            except Exception:
                pass
            self.key = None

    def departments(self):
        xmls = self._req("/resto/api/corporation/departments")
        out = {}
        for el in ET.fromstring(xmls):
            name = el.findtext("name"); did = el.findtext("id")
            if name and did:
                out[name.strip()] = did
        return out

    def stores(self):
        xmls = self._req("/resto/api/corporation/stores")
        out = {}
        for el in ET.fromstring(xmls):
            name = el.findtext("name"); sid = el.findtext("id")
            if name and sid:
                out[name.strip()] = sid
        return out

    def conceptions(self):
        raw = self._req("/resto/api/v2/entities/list", {"rootType": "Conception"})
        out = {}
        for e in json.loads(raw):
            if e.get("name") and e.get("id"):
                out[e["name"].strip()] = e["id"]
        return out

    def products(self):
        raw = self._req("/resto/api/v2/entities/products/list", {"includeDeleted": "false"})
        return json.loads(raw)

    def groups(self):
        raw = self._req("/resto/api/v2/entities/products/group/list", {"includeDeleted": "false"})
        return json.loads(raw)

    def prices(self, department_id, day):
        """Действующие цены из приказов (то, что видит iikoFront) на дату day для подразделения."""
        raw = self._req("/resto/api/v2/price", {"dateFrom": day, "dateTo": day,
                                                "departmentId": department_id, "type": "BASE"})
        data = json.loads(raw)
        if isinstance(data, dict):
            if isinstance(data.get("response"), list):
                return data["response"]
            for k, v in data.items():
                if isinstance(v, list) and k != "errors":
                    return v
            return []
        return data

    def employees(self):
        xmls = self._req("/resto/api/employees")
        out, tagstats = [], {}
        for el in ET.fromstring(xmls):
            d = {c.tag: (c.text or "").strip() for c in el}
            for k, v in d.items():
                tagstats.setdefault(k, 0)
                if v: tagstats[k] += 1
            name = d.get("name") or " ".join(
                filter(None, [d.get("lastName"), d.get("firstName"), d.get("middleName")])).strip()
            pin = d.get("pinCode") or d.get("pin") or d.get("code") or ""
            if name and d.get("deleted") != "true":
                out.append({"id": d.get("id"), "name": name, "pin": pin})
        return out, tagstats

    def import_sales_act(self, xml_body):
        ep = CFG.get("akt_endpoint", "/resto/api/documents/import/salesDocument")
        return self._req(ep, data=xml_body.encode("utf-8"),
                         headers={"Content-Type": "application/xml"})

# ---------------- синхронизация цен из iiko ----------------
SYNC_STATE = {"last_sync": None, "last_sync_result": "", "last_akt": None, "last_akt_result": ""}

def sync_prices():
    """Обновляет цены существующих позиций меню точным прейскурантом из номенклатуры iiko (по имени)."""
    ii = Iiko(CFG)
    try:
        ii.auth()
        prods = ii.products()
    finally:
        ii.logout()
    price_by_name, names_all = {}, set()
    for p in prods:
        nm = (p.get("name") or "").strip()
        pr = p.get("defaultSalePrice") or 0
        if nm:
            names_all.add(nm)
            if pr:
                price_by_name[nm] = pr
    updated, missing = 0, []
    st = doc_get("config/structure") or {}
    for r in st.get("rms", []):
        path = "menus/" + r["id"]
        menu = doc_get(path)
        if not menu:
            continue
        changed = False
        for it in menu.get("items", []):
            nm = (it.get("name") or "").strip()
            if nm in price_by_name:
                if it.get("price") != price_by_name[nm]:
                    it["price"] = price_by_name[nm]; changed = True; updated += 1
            elif nm not in names_all:  # позиции со свободной ценой (в номенклатуре, но без цены) — не трогаем
                missing.append(nm)
        if changed:
            doc_set(path, menu)
    res = "обновлено цен: %d; не найдено в номенклатуре: %d" % (updated, len(set(missing)))
    if missing:
        import difflib
        all_names = list(names_all)
        lines = []
        for nm in sorted(set(missing)):
            close = difflib.get_close_matches(nm, all_names, n=1, cutoff=0.6)
            lines.append(nm + ("  ->  " + close[0] if close else "  ->  (похожих нет)"))
        with open(os.path.join(LOG_DIR, "sync-missing.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    SYNC_STATE["last_sync"] = datetime.datetime.now().isoformat()
    SYNC_STATE["last_sync_result"] = res
    log("Синхронизация цен: " + res)
    return res

def sync_margins():
    """Фудкост блюд из iiko (OLAP «Продажи», банкетные заказы за 12 месяцев) → цветные метки
    маржинальности на плитках фронта. Хранится в doc config/margins: {dep: {dish: foodcost}}."""
    ii = Iiko(CFG)
    today = datetime.date.today()
    body = {"reportType": "SALES", "buildSummary": False,
            "groupByRowFields": ["Department", "DishName"],
            "aggregateFields": ["DishDiscountSumInt", "ProductCostBase.ProductCost"],
            "filters": {"OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                                           "from": (today - datetime.timedelta(days=365)).isoformat(), "to": today.isoformat()},
                        "Banquet": {"filterType": "IncludeValues", "values": ["TRUE"]},
                        "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
                        "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]}}}
    try:
        ii.auth()
        raw = ii._req("/resto/api/v2/reports/olap", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    finally:
        ii.logout()
    data = json.loads(raw).get("data") or []
    by = {}
    for r in data:
        dep = (r.get("Department") or "").strip(); name = (r.get("DishName") or "").strip()
        rev = float(r.get("DishDiscountSumInt") or 0); cost = float(r.get("ProductCostBase.ProductCost") or 0)
        if not dep or not name or rev <= 0:
            continue
        by.setdefault(dep, {})[name] = round(cost / rev, 4)
    doc_set("config/margins", {"byDep": by, "syncedAt": datetime.datetime.now().isoformat(),
                               "green": float(CFG.get("margin_green", 0.20)), "orange": float(CFG.get("margin_orange", 0.30))})
    res = "фудкост: %d блюд в %d производствах" % (sum(len(v) for v in by.values()), len(by))
    SYNC_STATE["margins"] = res
    log("Маржинальность: " + res)
    return res

def sync_menu():
    """Строит меню каждого РМС из действующего прайс-листа iiko (приказы),
    с теми же группами и ценами, что видит iikoFront."""
    ii = Iiko(CFG)
    today = datetime.date.today().isoformat()
    try:
        ii.auth()
        deps = ii.departments()
        prods = {p["id"]: p for p in ii.products() if p.get("id")}
        groups = {g["id"]: g for g in ii.groups() if g.get("id")}
        st = doc_get("config/structure") or {}
        report = []
        for r in st.get("rms", []):
            dep_id = deps.get((r.get("iikoName") or "").strip())
            if not dep_id:
                report.append("%s: подразделение не найдено" % r.get("name")); continue
            rows = ii.prices(dep_id, today)
            items = []
            for row in rows:
                pid = row.get("productId")
                p = prods.get(pid)
                if not p or p.get("deleted"):
                    continue
                cur = None
                for pr in (row.get("prices") or []):
                    if pr.get("included") is False:
                        continue
                    df, dt = pr.get("dateFrom") or "0000", pr.get("dateTo") or "9999"
                    if df <= today < dt or (df <= today and not pr.get("dateTo")):
                        cur = pr
                if not cur and row.get("prices"):
                    cur = row["prices"][-1]
                    if cur.get("included") is False:
                        cur = None
                if not cur:
                    continue
                # цена null в прайс-листе = свободная цена (услуги): позиция есть, цену вводят на фронте
                # категория = группа верхнего уровня (как плитки групп в iikoFront)
                gid, cat, hops = p.get("parent"), "Прочее", 0
                while gid and gid in groups and hops < 10:
                    cat = groups[gid].get("name") or cat
                    gid = groups[gid].get("parent"); hops += 1
                items.append({"id": pid, "productId": pid, "name": (p.get("name") or "").strip(),
                              "category": cat, "price": cur.get("price") or 0, "freePrice": not cur.get("price")})
            # услуги (тип SERVICE) продаются без приказа — как в iikoFront, всегда в меню со свободной ценой
            have = {i["id"] for i in items}
            for pid, p in prods.items():
                if p.get("type") == "SERVICE" and pid not in have and not p.get("deleted"):
                    gid, cat, hops = p.get("parent"), "Услуги", 0
                    while gid and gid in groups and hops < 10:
                        cat = groups[gid].get("name") or cat
                        gid = groups[gid].get("parent"); hops += 1
                    # только продаваемые услуги: группа «Услуги, оснащение предприятия» (плитка iikoFront)
                    # или флаг «включено в меню по умолчанию»
                    # продаваемая услуга: разрешена свободная цена или есть цена продажи;
                    # расходные услуги для накладных (коммуналка, реклама) этого не имеют
                    if not (p.get("canSetOpenPrice") or (p.get("defaultSalePrice") or 0) > 0):
                        continue
                    items.append({"id": pid, "productId": pid, "name": (p.get("name") or "").strip(),
                                  "category": cat, "price": p.get("defaultSalePrice") or 0,
                                  "freePrice": not p.get("defaultSalePrice")})
            items.sort(key=lambda i: (i["category"], i["name"]))
            if items:
                doc_set("menus/" + r["id"], {"items": items, "syncedAt": datetime.datetime.now().isoformat(),
                                             "source": "iiko price list"})
            report.append("%s: %d позиций" % (r.get("name"), len(items)))
    finally:
        ii.logout()
    res = "; ".join(report)
    SYNC_STATE["last_sync"] = datetime.datetime.now().isoformat()
    SYNC_STATE["last_sync_result"] = "меню из прайс-листа iiko — " + res
    log("Синхронизация меню: " + res)
    return res

def local_version():
    try:
        return open(VERSION_PATH, encoding="utf-8").read().strip()
    except Exception:
        return "0"

def check_update(force=False):
    """Проверяет обновление по update_url, скачивает и ставит файлы, затем перезапуск."""
    try:
        return _check_update(force)
    except Exception:
        UPDATING[0] = False
        raise

_utok_cache = {"t": "", "at": 0}
def update_token():
    """Ключ GitHub «только чтение» для скачивания обновлений из закрытого репозитория. Лежит в общем gist
    в закодированном виде; утечка такого ключа позволяет только читать код программы."""
    t = CFG.get("update_token") or ""
    if t:
        return t
    if _utok_cache["at"] and time.time() - _utok_cache["at"] < 600:
        return _utok_cache["t"]
    try:
        import base64
        s = json.loads(gist_raw("secrets.json")) or {}
        t = base64.b64decode(s["update_token_b64"][::-1]).decode("utf-8").strip() if s.get("update_token_b64") else ""
        _utok_cache.update(t=t, at=time.time())
        return t
    except Exception:
        return _utok_cache["t"]

_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/([^?]+)(\?.*)?$")
def fetch_update_file(url, timeout=60):
    """Файл обновления: при наличии ключа — через API GitHub (закрытый репозиторий, всегда свежий файл),
    иначе или при ошибке — по прямой ссылке, как раньше."""
    tok = update_token()
    m = _RAW_RE.match(url)
    if tok and m:
        api = "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (m.group(1), m.group(2), m.group(4), m.group(3))
        req = urllib.request.Request(api)
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Accept", "application/vnd.github.raw")
        req.add_header("User-Agent", "banket-front")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            log("Обновление через API GitHub не удалось (%s) — пробую прямую ссылку" % e)
    return urllib.request.urlopen(url, timeout=timeout).read()

def _check_update(force=False):
    url = CFG.get("update_url")
    if not url:
        return "адрес обновлений не задан"
    man = json.loads(fetch_update_file(url + ("&" if "?" in url else "?") + "t=%d" % int(time.time()), timeout=30).decode("utf-8"))
    ver = str(man.get("version", ""))
    if not ver:
        return "манифест без версии"
    if ver == local_version() and not force:
        return "установлена последняя версия " + ver
    if _ver_tuple(ver) < _ver_tuple(local_version()):
        return "на сервере обновлений версия %s старее установленной %s — не откатываю" % (ver, local_version())
    changed = []
    UPDATING[0] = True
    for name, info in (man.get("files") or {}).items():
        if name not in UPDATABLE:
            continue
        data = fetch_update_file(info["url"], timeout=60)
        if hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise RuntimeError("контрольная сумма не совпала: " + name)
        dst = os.path.join(BASE, name)
        try:
            if open(dst, "rb").read() == data:
                continue
        except Exception:
            pass
        tmp = dst + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        changed.append(name)
    with open(VERSION_PATH, "w", encoding="utf-8") as f:
        f.write(ver)
    SYNC_STATE["version"] = ver
    res = "обновлено до %s: %s" % (ver, ", ".join(changed) or "файлы уже совпадали")
    log("Обновление: " + res)
    if "server.py" in changed:
        threading.Timer(1.0, self_restart, args=("обновление " + ver,)).start()
    else:
        UPDATING[0] = False
    return res

# ---------------- выпуск обновления в своё S3-хранилище (Timeweb) ----------------
def s3_put(key, data, content_type="application/octet-stream"):
    """PUT объекта в S3 (AWS Signature V4) стандартной библиотекой."""
    import hmac
    endpoint = CFG["s3_endpoint"].rstrip("/"); region = CFG.get("s3_region", "ru-1")
    bucket = CFG["s3_bucket"]; ak = CFG["s3_access_key"]; sk = CFG["s3_secret_key"]
    host = endpoint.split("://", 1)[1]
    path = "/%s/%s" % (bucket, urllib.parse.quote(key))
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ"); date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "x-amz-acl": "public-read", "x-amz-content-sha256": payload_hash,
               "x-amz-date": amz_date, "content-type": content_type}
    signed = ";".join(sorted(headers))
    canon_headers = "".join("%s:%s\n" % (k, headers[k]) for k in sorted(headers))
    canonical = "PUT\n%s\n\n%s\n%s\n%s" % (path, canon_headers, signed, payload_hash)
    scope = "%s/%s/s3/aws4_request" % (date, region)
    sts = "AWS4-HMAC-SHA256\n%s\n%s\n%s" % (amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest())
    def hm(k, msg): return hmac.new(k, msg.encode("utf-8"), hashlib.sha256).digest()
    kdate = hm(("AWS4" + sk).encode("utf-8"), date)
    ksign = hm(hm(hm(kdate, region), "s3"), "aws4_request")
    sig = hmac.new(ksign, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (ak, scope, signed, sig)
    req = urllib.request.Request(endpoint + path, data=data, method="PUT")
    for k, v in headers.items():
        if k != "host":
            req.add_header(k, v)
    req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status

def release(version):
    """Выкладывает текущие server.py/front.html и манифест в S3 — обновление для всех фронтов."""
    if not (CFG.get("s3_access_key") and CFG.get("s3_secret_key") and CFG.get("s3_bucket")):
        return "S3-ключи не заданы в config.json"
    prefix = CFG.get("s3_prefix", "banket-front/")
    base = "%s/%s/%s" % (CFG["s3_endpoint"].rstrip("/"), CFG["s3_bucket"], prefix)
    man = {"version": version, "files": {}, "releasedAt": datetime.datetime.now().isoformat()}
    for name in UPDATABLE:
        data = open(os.path.join(BASE, name), "rb").read()
        s3_put(prefix + name, data, "text/plain; charset=utf-8")
        man["files"][name] = {"url": base + name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    s3_put(prefix + "manifest.json", json.dumps(man, ensure_ascii=False, indent=1).encode("utf-8"),
           "application/json; charset=utf-8")
    with open(VERSION_PATH, "w", encoding="utf-8") as f:
        f.write(version)
    res = "выпущена версия %s: %s" % (version, ", ".join(UPDATABLE))
    log("Релиз: " + res)
    return res

GIST_ID = "06a9426f7c6177bcac1b70496115af2c"
GIST_RAW = "https://gist.githubusercontent.com/mainmarketplace/%s/raw/" % GIST_ID

def gist_raw(name, timeout=30):
    url = GIST_RAW + name + "?t=%d" % int(time.time())
    return urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8-sig")

def load_shared_pins():
    """Общий список PIN всех фронтов: {id сотрудника: pin}. iiko не отдаёт PIN-коды по API.
    При наличии ключа читаем через API (без кэша), иначе — по raw-ссылке."""
    name = CFG.get("pins_file", "pins.json")
    try:
        if gist_token():
            f = (gist_api("GET").get("files") or {}).get(name) or {}
            txt = f.get("content") or "{}"
            if f.get("truncated") and f.get("raw_url"):
                txt = urllib.request.urlopen(f["raw_url"], timeout=60).read().decode("utf-8")
            return _pins_decode(json.loads(txt) or {})
        return _pins_decode(json.loads(gist_raw(name)) or {})
    except Exception as ex:
        log("Общий список PIN недоступен: %s" % ex)
        return None

def _pins_decode(obj):
    if is_box(obj):
        if not fleet_key():
            raise Exception("PIN-коды зашифрованы, а ключа кафе на этом компьютере ещё нет — нужен допуск владельца")
        return fleet_open(obj) or {}
    return obj

_tok_cache = {"t": "", "at": 0}
def gist_token():
    """Ключ GitHub для записи в общий список. В gist хранится в закодированном виде (GitHub
    автоматически отзывает ключи, выложенные открытым текстом даже в секретный gist)."""
    t = CFG.get("github_token") or ""
    if t:
        return t
    if _tok_cache["t"] and time.time() - _tok_cache["at"] < 600:
        return _tok_cache["t"]
    try:
        import base64
        s = json.loads(gist_raw("secrets.json")) or {}
        t = s.get("github_token") or ""
        if not t and s.get("github_token_b64"):
            t = base64.b64decode(s["github_token_b64"][::-1]).decode("utf-8").strip()
        _tok_cache.update(t=t, at=time.time())
        return t
    except Exception:
        return ""

def gist_api(method="GET", files=None, token=None):
    tok = token or gist_token()
    if not tok:
        raise Exception("нет ключа GitHub")
    data = json.dumps({"files": files}).encode("utf-8") if files else None
    req = urllib.request.Request("https://api.github.com/gists/" + GIST_ID, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "banket-front")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def _secrets_merge(upd, token=None):
    g = gist_api("GET", token=token)
    f = (g.get("files") or {}).get("secrets.json") or {}
    try:
        cur = json.loads(f.get("content") or "{}") or {}
    except Exception:
        cur = {}
    cur.update(upd)
    gist_api("PATCH", files={"secrets.json": {"content": json.dumps(cur, indent=1)}}, token=token)

def setup_update_token(token):
    """Проверить ключ «только чтение» на репозиторий обновлений и сохранить его для всех фронтов."""
    import base64
    token = (token or "").strip()
    if not token:
        raise Exception("ключ пустой")
    m = _RAW_RE.match((CFG.get("update_url") or "").split("?")[0] + "")
    if not m:
        raise Exception("адрес обновлений не на GitHub")
    req = urllib.request.Request("https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (m.group(1), m.group(2), m.group(4), m.group(3)))
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github.raw")
    req.add_header("User-Agent", "banket-front")
    with urllib.request.urlopen(req, timeout=30) as r:
        man = json.loads(r.read().decode("utf-8"))
    if not man.get("version"):
        raise Exception("ключ открыл репозиторий, но манифест без версии")
    enc = base64.b64encode(token.encode("utf-8")).decode("ascii")[::-1]
    _secrets_merge({"update_token_b64": enc})
    _utok_cache.update(t=token, at=time.time())
    return "ключ проверен (видна версия %s) и сохранён для всех фронтов" % man["version"]

SETUP_UPDATE_TOKEN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Ключ обновлений</title>
<style>body{font:17px/1.5 'Segoe UI',sans-serif;background:#3F4040;color:#eee;display:flex;justify-content:center;padding:40px}
.b{background:#323539;border:1px solid #494D53;border-radius:8px;padding:28px 32px;max-width:640px}
input{width:100%;font-size:18px;padding:10px;border-radius:4px;border:1px solid #777;background:#222;color:#fff;font-family:Consolas,monospace}
button{margin-top:14px;font-size:18px;padding:12px 26px;background:#E3E982;border:none;border-radius:4px;cursor:pointer;font-weight:700}
#m{margin-top:14px;min-height:24px}</style></head><body><div class="b">
<h2>Ключ для скачивания обновлений (только чтение)</h2>
<p>Вставьте ключ (начинается с <b>github_pat_</b>) и нажмите «Сохранить». Программа проверит, что через него видна последняя версия, и положит его в общее хранилище — остальные фронты подхватят сами.</p>
<input id="t" placeholder="github_pat_..." autofocus autocomplete="off">
<button id="s">Сохранить</button><div id="m"></div>
<script>document.getElementById('s').onclick=async()=>{const m=document.getElementById('m');m.textContent='Проверяю…';
try{const r=await fetch('/api/setup-update-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('t').value})}).then(x=>x.json());
m.style.color=r.ok?'#9BE29B':'#FF8A80';m.textContent=r.result||r.error;if(r.ok)document.getElementById('t').value='';}catch(e){m.style.color='#FF8A80';m.textContent='Ошибка: '+e;}};</script>
</div></body></html>"""

def setup_token(token):
    """Проверить ключ и сохранить его в gist в закодированном виде — для всех фронтов."""
    import base64
    token = (token or "").strip()
    if not token:
        raise Exception("ключ пустой")
    g = gist_api("GET", token=token)
    if GIST_ID not in (g.get("id") or ""):
        raise Exception("ключ не даёт доступа к gist")
    enc = base64.b64encode(token.encode("utf-8")).decode("ascii")[::-1]
    _secrets_merge({"github_token_b64": enc}, token=token)
    _tok_cache.update(t=token, at=time.time())
    return "ключ проверен и сохранён для всех фронтов"

SETUP_TOKEN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Ключ общего списка</title>
<style>body{font:17px/1.5 'Segoe UI',sans-serif;background:#3F4040;color:#eee;display:flex;justify-content:center;padding:40px}
.b{background:#323539;border:1px solid #494D53;border-radius:8px;padding:28px 32px;max-width:640px}
input{width:100%;font-size:18px;padding:10px;border-radius:4px;border:1px solid #777;background:#222;color:#fff;font-family:Consolas,monospace}
button{margin-top:14px;font-size:18px;padding:12px 26px;background:#E3E982;border:none;border-radius:4px;cursor:pointer;font-weight:700}
#m{margin-top:14px;min-height:24px}</style></head><body><div class="b">
<h2>Ключ GitHub для общего списка (PIN-коды, банкеты всех залов)</h2>
<p>Вставьте ключ (начинается с <b>ghp_</b>) и нажмите «Сохранить». Программа проверит его и положит в общее хранилище в закодированном виде — остальные фронты подхватят сами.</p>
<input id="t" placeholder="ghp_..." autofocus autocomplete="off">
<button id="s">Сохранить</button><div id="m"></div>
<script>document.getElementById('s').onclick=async()=>{const m=document.getElementById('m');m.textContent='Проверяю…';
try{const r=await fetch('/api/setup-token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.getElementById('t').value})}).then(x=>x.json());
m.style.color=r.ok?'#9BE29B':'#FF8A80';m.textContent=r.result||r.error;if(r.ok)document.getElementById('t').value='';}catch(e){m.style.color='#FF8A80';m.textContent='Ошибка: '+e;}};</script>
</div></body></html>"""

_pin_lock = threading.Lock()
def set_shared_pin(emp_id, pin):
    """Запись PIN в общий список — сразу для всех фронтов (по одному, без гонок)."""
    with _pin_lock:
        return _set_shared_pin(emp_id, pin)

def _set_shared_pin(emp_id, pin):
    tok = gist_token()
    if not tok:
        raise Exception("нет доступа к общему списку PIN — код сохранён только на этом компьютере")
    shared = load_shared_pins()
    if shared is None:
        raise Exception("общий список PIN недоступен — код сохранён только на этом компьютере")
    pin = str(pin or "").strip()
    if pin:
        shared[emp_id] = pin
    else:
        shared.pop(emp_id, None)
    body = fleet_seal(shared) if fleet_key() else shared
    gist_api("PATCH", files={CFG.get("pins_file", "pins.json"): {"content": json.dumps(body, ensure_ascii=False, indent=1)}}, token=tok)
    return shared

def fleet_enable():
    """Владелец включает шифрование: создаётся ключ кафе, допускаются все работающие компьютеры, PIN-коды перешифровываются."""
    if not is_owner_admin():
        raise Exception("включать шифрование можно только с компьютера владельца (там, где включён удалённый доступ)")
    sync_banquets(force=True)
    pcs = (doc_get("shared/banquets") or {}).get("pcs") or {}
    behind, ok = [], {}
    for pcid, d in pcs.items():
        if d.get("stale"):
            continue
        ver = _ver_tuple((d.get("state") or {}).get("version"))
        if ver < FLEET_MIN_VERSION or not d.get("devicePub"):
            behind.append("%s (версия %s)" % (d.get("host") or pcid, (d.get("state") or {}).get("version") or "старая"))
        else:
            ok[pcid] = {"pub": d["devicePub"], "host": d.get("host")}
    if behind:
        raise Exception("сначала должны обновиться: " + ", ".join(behind) + " — подождите до часа и попробуйте снова")
    with _pin_lock:
        plain = load_shared_pins()
        if plain is None:
            raise Exception("не удалось прочитать PIN-коды — шифрование не включено")
        if not fleet_key():
            doc_set("secure/fleet", {"key": os.urandom(32).hex(), "at": datetime.datetime.now().isoformat(timespec="seconds")})
        gr = doc_get("secure/fleet_grants") or {"items": {}}
        gr.setdefault("items", {}).update(ok)
        doc_set("secure/fleet_grants", gr)
        gist_api("PATCH", files={CFG.get("pins_file", "pins.json"): {"content": json.dumps(fleet_seal(plain), ensure_ascii=False, indent=1)}})
    sync_banquets(force=True)
    names = [v.get("host") for v in ok.values()]
    return "общий список зашифрован; допущены: %s" % (", ".join(names) if names else "только этот компьютер")

def fleet_approve(pcid):
    if not (is_owner_admin() and fleet_key()):
        raise Exception("сначала включите шифрование на компьютере владельца")
    d = ((doc_get("shared/banquets") or {}).get("pcs") or {}).get(pcid) or {}
    if not d.get("devicePub"):
        raise Exception("этот компьютер ещё не прислал свой ключ — пусть обновится до новой версии")
    gr = doc_get("secure/fleet_grants") or {"items": {}}
    gr.setdefault("items", {})[pcid] = {"pub": d["devicePub"], "host": d.get("host")}
    doc_set("secure/fleet_grants", gr)
    sync_banquets(force=True)
    return "компьютер %s допущен — получит ключ кафе в течение нескольких минут" % (d.get("host") or pcid)

SHARED = {"settings": {}, "at": 0}
def load_shared_settings():
    """Общие настройки всех фронтов (settings.json в gist): dry_run и т.п. Управляются централизованно."""
    try:
        f = (gist_api("GET").get("files") or {}).get("settings.json") or {}
        d = json.loads(f.get("content") or "{}") or {}
        if d != SHARED["settings"]:
            log("Общие настройки: %s" % json.dumps(d, ensure_ascii=False))
        SHARED["settings"] = d
        SHARED["at"] = time.time()
        doc_set("config/shared_settings", d)
    except Exception as ex:
        if not SHARED["settings"]:
            SHARED["settings"] = doc_get("config/shared_settings") or {}
        log("Общие настройки недоступны: %s" % ex)
    return SHARED["settings"]

def dry_run():
    """Боевой режим включается централизованно (settings.json), иначе — config.json этого компьютера."""
    st = SHARED["settings"] or (doc_get("config/shared_settings") or {})
    if "dry_run" in st:
        return bool(st["dry_run"])
    return bool(CFG.get("dry_run", True))

def apply_pins(emps, shared):
    for e in emps:
        e["pin"] = ""
        pin = (shared or {}).get(e.get("id") or "") or (shared or {}).get(e["name"]) or (shared or {}).get(e["name"].strip())
        if pin:
            e["pin"] = str(pin).strip()
    ov = (doc_get("config/pin_overrides") or {}).get("items") or {}
    for e in emps:
        if ov.get(e.get("id")):
            e["pin"] = ov[e["id"]]
    return emps

def refresh_pins(shared=None):
    """Перечитать общий список PIN без обращения к iiko (каждые 10 минут)."""
    doc = doc_get("config/employees") or {}
    emps = doc.get("items") or []
    if not emps:
        return "сотрудники ещё не загружены"
    if shared is None:
        shared = load_shared_pins()
    if shared is None:
        return "общий список PIN недоступен"
    apply_pins(emps, shared)
    doc["items"] = emps
    doc_set("config/employees", doc)
    return "PIN обновлены: %d" % len([e for e in emps if e.get("pin")])

# ---------------- общий список банкетов всех залов ----------------
SELF_PC = {}  # этот компьютер — для экрана «Компьютеры с фронтом»
# ==================== Удалённый доступ через RustDesk ====================
# Пароль RustDesk каждого компьютера шифруется «запечатанным конвертом» на открытый ключ
# управляющего компьютера (компьютера владельца). В общий список уходит только шифртекст —
# прочитать пароль может лишь компьютер владельца, у которого есть закрытый ключ. Ключ записи
# к общему списку доступен всем фронтам, поэтому класть туда пароль открытым текстом нельзя.
import hmac as _hmac, secrets as _secrets
_P25519 = 2**255 - 19
_X_BASE = (9).to_bytes(32, "little")
def _x25519(k_bytes, u_bytes):
    def _ds(k):
        k = bytearray(k); k[0] &= 248; k[31] &= 127; k[31] |= 64
        return int.from_bytes(k, "little")
    def _du(u):
        u = bytearray(u); u[31] &= 127
        return int.from_bytes(u, "little")
    k = _ds(k_bytes); x1 = _du(u_bytes)
    x2, z2, x3, z3, swap, a24 = 1, 0, _du(u_bytes), 1, 0, 121665
    for t in range(254, -1, -1):
        kt = (k >> t) & 1; swap ^= kt
        if swap: x2, x3 = x3, x2; z2, z3 = z3, z2
        swap = kt
        A = (x2 + z2) % _P25519; AA = (A*A) % _P25519
        B = (x2 - z2) % _P25519; BB = (B*B) % _P25519
        E = (AA - BB) % _P25519
        C = (x3 + z3) % _P25519; D = (x3 - z3) % _P25519
        DA = (D*A) % _P25519; CB = (C*B) % _P25519
        x3 = pow((DA+CB) % _P25519, 2, _P25519); z3 = (x1 * pow((DA-CB) % _P25519, 2, _P25519)) % _P25519
        x2 = (AA*BB) % _P25519; z2 = (E * ((AA + a24*E) % _P25519)) % _P25519
    if swap: x2, x3 = x3, x2; z2, z3 = z3, z2
    return ((x2 * pow(z2, _P25519-2, _P25519)) % _P25519).to_bytes(32, "little")
def _hkdf(ikm, salt, info, length=64):
    prk = _hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""; t = b""; i = 1
    while len(okm) < length:
        t = _hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest(); okm += t; i += 1
    return okm[:length]
_SEAL_INFO = b"banket-rustdesk-v1"
def _keystream(key, nonce, n):
    out = b""; c = 0
    while len(out) < n:
        out += _hmac.new(key, nonce + c.to_bytes(4, "big"), hashlib.sha256).digest(); c += 1
    return out[:n]
def seal_secret(recipient_pub_hex, plaintext):
    import base64
    rpub = bytes.fromhex(recipient_pub_hex)
    esk = os.urandom(32); epk = _x25519(esk, _X_BASE); shared = _x25519(esk, rpub)
    kk = _hkdf(shared, epk + rpub, _SEAL_INFO, 64); ek, mk = kk[:32], kk[32:]
    nonce = os.urandom(16); pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(ek, nonce, len(pt))))
    tag = _hmac.new(mk, epk + nonce + ct, hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "epk": b(epk), "nonce": b(nonce), "ct": b(ct), "tag": b(tag)}
def unseal_secret(recipient_priv_hex, box):
    import base64
    d = lambda x: base64.b64decode(x)
    priv = bytes.fromhex(recipient_priv_hex)
    epk, nonce, ct, tag = d(box["epk"]), d(box["nonce"]), d(box["ct"]), d(box["tag"])
    rpub = _x25519(priv, _X_BASE); shared = _x25519(priv, epk)
    kk = _hkdf(shared, epk + rpub, _SEAL_INFO, 64); ek, mk = kk[:32], kk[32:]
    if not _hmac.compare_digest(_hmac.new(mk, epk + nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("метка не совпала")
    return bytes(a ^ b for a, b in zip(ct, _keystream(ek, nonce, len(ct)))).decode("utf-8")

def admin_keys():
    d = doc_get("secure/remote_admin") or {}
    return d.get("priv"), d.get("pub")
def ensure_admin_keys():
    priv, pub = admin_keys()
    if priv and pub:
        return priv, pub
    sk = os.urandom(32); pk = _x25519(sk, _X_BASE)
    d = doc_get("secure/remote_admin") or {}
    d.update(priv=sk.hex(), pub=pk.hex(), createdAt=datetime.datetime.now().isoformat(timespec="seconds"))
    doc_set("secure/remote_admin", d)
    return d["priv"], d["pub"]
def is_admin_pc():
    priv, pub = admin_keys()
    return bool(priv and pub)
def remote_on():
    return bool((doc_get("secure/remote_admin") or {}).get("on"))
# Открытый ключ компьютера владельца (DESKTOP-P5FCAAV). Зашит в программу: менять программу на GitHub
# может только владелец, а в общий список писать может любой, кто знает его адрес. Поэтому пароли
# RustDesk и выдачу ключа кафе компьютеры доверяют только этому ключу. Если компьютер владельца
# переустановят — нужно выпустить обновление с новым ключом.
OWNER_ADMIN_PUB = "b91ab0e8147a53d80874a7161ba43b410388f183f546ccfacf9165c6819f7e62"
def is_owner_admin():
    priv, pub = admin_keys()
    return bool(priv and pub == OWNER_ADMIN_PUB)
def known_admin_pub():
    if is_owner_admin() and remote_on():
        return OWNER_ADMIN_PUB
    for d in ((doc_get("shared/banquets") or {}).get("pcs") or {}).values():
        a = d.get("admin") or {}
        if a.get("on") and a.get("pub") == OWNER_ADMIN_PUB:
            return OWNER_ADMIN_PUB
    return None

# ==================== Ключ кафе: шифрование общего списка ====================
# Банкеты (телефоны клиентов), состояние компьютеров и PIN-коды в общем gist шифруются ключом кафе.
# Ключ кафе есть только у допущенных компьютеров. Новый компьютер получает его, когда владелец
# нажимает «Допустить»: ключ запечатывается на ключ устройства, и проверить, что выдал его именно
# владелец, можно по OWNER_ADMIN_PUB (подделать выдачу нельзя).
_FLEET_INFO = b"banket-fleet-v1"
_GRANT_INFO = b"banket-fleet-grant-v1"
FLEET_MIN_VERSION = (2026, 11, 23, 3)
def _b64e(x):
    import base64; return base64.b64encode(x).decode()
def _b64d(x):
    import base64; return base64.b64decode(x)
def _xor(a, b):
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big") if a else b""
def _sym_seal(key, data, info):
    nonce = os.urandom(16); kk = _hkdf(key, nonce, info, 64); ek, mk = kk[:32], kk[32:]
    ct = _xor(data, _keystream(ek, nonce, len(data)))
    tag = _hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    return {"fv": 1, "nonce": _b64e(nonce), "ct": _b64e(ct), "tag": _b64e(tag)}
def _sym_open(key, box, info):
    nonce, ct, tag = _b64d(box["nonce"]), _b64d(box["ct"]), _b64d(box["tag"])
    kk = _hkdf(key, nonce, info, 64); ek, mk = kk[:32], kk[32:]
    if not _hmac.compare_digest(_hmac.new(mk, nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("не подходит ключ кафе")
    return _xor(ct, _keystream(ek, nonce, len(ct)))
def is_box(d):
    return isinstance(d, dict) and d.get("fv") == 1 and "ct" in d
def fleet_key():
    k = (doc_get("secure/fleet") or {}).get("key")
    return bytes.fromhex(k) if k else None
def fleet_seal(obj):
    return _sym_seal(fleet_key(), json.dumps(obj, ensure_ascii=False).encode("utf-8"), _FLEET_INFO)
def fleet_open(box):
    return json.loads(_sym_open(fleet_key(), box, _FLEET_INFO).decode("utf-8"))
def device_keys():
    d = doc_get("secure/device") or {}
    if not (d.get("priv") and d.get("pub")):
        sk = os.urandom(32)
        d = {"priv": sk.hex(), "pub": _x25519(sk, _X_BASE).hex(), "at": datetime.datetime.now().isoformat(timespec="seconds")}
        doc_set("secure/device", d)
    return d["priv"], d["pub"]
def _grant_key(my_priv_hex, other_pub_hex, device_pub_hex):
    shared = _x25519(bytes.fromhex(my_priv_hex), bytes.fromhex(other_pub_hex))
    return _hkdf(shared, bytes.fromhex(OWNER_ADMIN_PUB) + bytes.fromhex(device_pub_hex), _GRANT_INFO, 32)
def make_grant(device_pub_hex):
    priv, pub = admin_keys()
    return _sym_seal(_grant_key(priv, device_pub_hex, device_pub_hex), fleet_key().hex().encode(), _GRANT_INFO)
def open_grant(box):
    dpriv, dpub = device_keys()
    return bytes.fromhex(_sym_open(_grant_key(dpriv, OWNER_ADMIN_PUB, dpub), box, _GRANT_INFO).decode())
def fleet_grants_out():
    """Выдачи ключа кафе (только на компьютере владельца), с кэшем, чтобы не менять файл без нужды."""
    if not (is_owner_admin() and fleet_key()):
        return None
    gr = (doc_get("secure/fleet_grants") or {}).get("items") or {}
    cache = doc_get("secure/fleet_grant_cache") or {}
    out = {}; changed = False
    for pcid, g in gr.items():
        c = cache.get(pcid) or {}
        if c.get("pub") == g.get("pub") and c.get("box"):
            out[pcid] = c["box"]
        else:
            out[pcid] = make_grant(g["pub"]); cache[pcid] = {"pub": g["pub"], "box": out[pcid]}; changed = True
    if changed:
        doc_set("secure/fleet_grant_cache", cache)
    return out
def _ver_tuple(v):
    try: return tuple(int(x) for x in str(v).split("."))
    except Exception: return (0,)
# ==================== конец блока ключа кафе ====================

RD_DIR = os.path.join(BASE, "rustdesk")
RD_EXE = os.path.join(RD_DIR, "rustdesk.exe")
RD = {"id": "", "pw": "", "status": "выключено", "ok": False, "ver": "", "at": 0}
_RD_LOCK = threading.Lock()
_RD_PROC = [None]
def _rd_run(args, timeout=60, capture=False):
    import subprocess
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.run([RD_EXE] + args, cwd=RD_DIR, capture_output=capture, text=True,
                          timeout=timeout, creationflags=flags)
def _rd_download():
    os.makedirs(RD_DIR, exist_ok=True)
    rel = json.loads(urllib.request.urlopen("https://api.github.com/repos/rustdesk/rustdesk/releases/latest", timeout=60).read().decode())
    ver = rel.get("tag_name", ""); url = None
    for a in rel.get("assets") or []:
        if re.match(r"^rustdesk-[0-9.]+-x86_64\.exe$", a.get("name") or ""):
            url = a.get("browser_download_url"); break
    if not url:
        raise Exception("в последнем выпуске RustDesk не найден установщик для Windows")
    data = urllib.request.urlopen(url, timeout=600).read()
    with open(RD_EXE, "wb") as f:
        f.write(data)
    RD["ver"] = ver
    log("RustDesk %s скачан (%d КБ)" % (ver, len(data)//1024))
def _rd_read_id():
    try:
        r = _rd_run(["--get-id"], timeout=30, capture=True)
        out = (r.stdout or "").strip().splitlines()
        if out and out[-1].strip().isdigit():
            return out[-1].strip()
    except Exception:
        pass
    for p in [os.path.join(os.environ.get("APPDATA", ""), "RustDesk", "config", "RustDesk.toml"),
              r"C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\RustDesk\config\RustDesk.toml"]:
        try:
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("id") and "=" in line:
                    v = line.split("=", 1)[1].strip().strip("'\"")
                    if v.isdigit():
                        return v
        except Exception:
            pass
    return ""
def _rd_ensure_running():
    import subprocess
    p = _RD_PROC[0]
    if p and p.poll() is None:
        return
    flags = 0x08000000 | 0x00000008 if os.name == "nt" else 0  # CREATE_NO_WINDOW|DETACHED
    try:
        _RD_PROC[0] = subprocess.Popen([RD_EXE, "--service"], cwd=RD_DIR, creationflags=flags, close_fds=True)
    except Exception as e:
        log("RustDesk запуск фоновой службы: %s" % e)
def rd_setup():
    """Поставить (если надо) RustDesk, задать постоянный пароль, узнать ID, запустить приём подключений.
    Пароль — случайный, свой на каждом компьютере, хранится локально и уходит в общий список только зашифрованным."""
    if os.name != "nt":
        RD.update(status="только Windows", ok=False); return
    with _RD_LOCK:
        sec = doc_get("secure/rustdesk") or {}
        pw = sec.get("pw") or "".join(_secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789") for _ in range(16))
        try:
            if not os.path.exists(RD_EXE):
                RD["status"] = "скачиваю RustDesk…"; _rd_download()
            try: _rd_run(["--password", pw], timeout=40)
            except Exception as e: log("RustDesk --password: %s" % e)
            _rd_ensure_running()
            rid = _rd_read_id()
            doc_set("secure/rustdesk", {"pw": pw, "id": rid, "savedAt": datetime.datetime.now().isoformat(timespec="seconds")})
            RD.update(id=rid, pw=pw, ok=bool(rid), at=time.time(),
                      status=("готов" if rid else "ID не прочитан — возможно, нужны права администратора (поставить RustDesk один раз вручную)"))
        except Exception as e:
            RD.update(ok=False, status="ошибка установки: %s" % str(e)[:200])
            log("RustDesk установка: %s" % e)
def rd_tick():
    if not (remote_on() or known_admin_pub()):
        if RD["status"] != "выключено": RD.update(status="выключено", ok=False)
        return
    if os.name != "nt":
        RD.update(status="только Windows"); return
    if not RD.get("ok") or time.time() - RD.get("at", 0) > 1800:
        rd_setup()
    else:
        _rd_ensure_running()
def rd_payload():
    """Запись этого компьютера для общего списка: ID (не секрет) и пароль в запечатанном конверте."""
    if not RD.get("ok") or not RD.get("id"):
        return None
    out = {"id": RD["id"], "status": RD["status"]}
    pub = known_admin_pub()
    if pub and RD.get("pw"):
        c = doc_get("secure/rd_enc_cache") or {}
        if c.get("pub") == pub and c.get("id") == RD["id"] and c.get("pw") == RD["pw"] and c.get("box"):
            out["enc"] = c["box"]
        else:
            box = seal_secret(pub, RD["pw"])
            doc_set("secure/rd_enc_cache", {"pub": pub, "id": RD["id"], "pw": RD["pw"], "box": box})
            out["enc"] = box
    return out
def rd_connect(target_pc):
    """На компьютере владельца: расшифровать пароль цели своим закрытым ключом и запустить подключение RustDesk."""
    priv, pub = admin_keys()
    if not priv:
        raise Exception("этот компьютер не управляющий — включите удалённый доступ на нём")
    d = ((doc_get("shared/banquets") or {}).get("pcs") or {}).get(target_pc) or {}
    rem = d.get("remote") or {}
    if not rem.get("id"):
        raise Exception("у этого компьютера ещё нет RustDesk ID")
    if not rem.get("enc"):
        raise Exception("пароль ещё не передан — подождите, пока тот компьютер обновит своё состояние")
    pw = unseal_secret(priv, rem["enc"])
    if os.name != "nt":
        raise Exception("подключаться можно только с Windows")
    if not os.path.exists(RD_EXE):
        rd_setup()
    import subprocess
    flags = 0x08000000 if os.name == "nt" else 0
    try:  # запасной путь: пароль в буфер обмена, если версия RustDesk не примет его из команды
        subprocess.run("clip", input=pw, text=True, timeout=5)
    except Exception:
        pass
    subprocess.Popen([RD_EXE, "--connect", rem["id"], "--password", pw], cwd=RD_DIR, creationflags=flags, close_fds=True)
    log("RustDesk: подключение к %s (ID %s)" % (d.get("host") or target_pc, rem["id"]))
    return {"host": d.get("host"), "id": rem["id"]}
# ==================== конец блока удалённого доступа ====================

def pc_id():
    d = doc_get("config/pc") or {}
    if not d.get("id"):
        import uuid
        d = {"id": uuid.uuid4().hex[:12], "host": _socket.gethostname()}
        doc_set("config/pc", d)
    return d["id"]

def bq_summary(b, bid):
    c = b.get("client") or {}
    sub = sum(float(i.get("price") or 0) * float(i.get("qty") or 0) for i in b.get("items", []))
    paid = sum(float(p.get("amount") or 0) for p in b.get("payments", []))
    return {"id": bid, "num": b.get("num"), "date": b.get("date"), "time": b.get("time"),
            "frontId": b.get("frontId"), "status": b.get("status"), "iikoStatus": b.get("iikoStatus"),
            "iikoError": (str(b.get("iikoError"))[:700] if b.get("iikoError") else None),
            "closedAt": b.get("closedAt"), "client": {"name": c.get("name"), "phone": c.get("phone"), "guests": c.get("guests")},
            "guests": c.get("guests"), "total": bq_totals(b), "paid": round(paid, 2), "sub": round(sub, 2),
            "comment": (b.get("comment") or "")[:120]}

STARTED_AT = datetime.datetime.now().isoformat(timespec="seconds")
def pc_state():
    """Состояние этого компьютера для общего списка: версия, что ждёт отправки в iiko, последняя ошибка."""
    try:
        akts, pays, oldest, errors = iiko_queue()
    except Exception:
        akts, pays, oldest, errors = 0, 0, None, []
    return {"version": local_version(), "dry": dry_run(),
            "iiko": bool(CFG.get("iiko_login") and CFG.get("iiko_password")),
            "aktWait": akts, "payWait": pays, "oldestWait": oldest, "errors": errors[:5],
            "lastAkt": SYNC_STATE.get("last_akt"), "lastAktResult": str(SYNC_STATE.get("last_akt_result") or "")[:900],
            "lastPay": SYNC_STATE.get("last_pay"), "lastPayResult": str(SYNC_STATE.get("last_pay_result") or "")[:900],
            "startedAt": STARTED_AT, "updKey": bool(update_token())}

def _state_sign(st):
    """Что в состоянии считается изменением (время последней попытки — нет, иначе писали бы без конца)."""
    return json.dumps({k: st.get(k) for k in ("version", "dry", "iiko", "aktWait", "payWait", "errors",
                                                "lastAktResult", "lastPayResult", "startedAt")},
                      ensure_ascii=False, sort_keys=True)

HEARTBEAT_SEC = 1800  # даже без изменений раз в полчаса сообщаем «я на связи»
_bq_push_hash = [""]
_bq_push_at = [0]
def push_banquets(force=False):
    """Выгрузить сводку банкетов этого компьютера в общий список (файл bq-<pc>.json в gist)."""
    tok = gist_token()
    if not tok:
        return "нет доступа к общему списку"
    pid = pc_id()
    fronts = {f["id"]: f.get("name") for f in ((doc_get("config/structure") or {}).get("fronts") or [])}
    items = []
    for x in col_get("banquets"):
        b = x.get("data") or {}
        if not b.get("date"):
            continue
        s = bq_summary(b, x["id"])
        s["frontName"] = fronts.get(s["frontId"], "")
        items.append(s)
    items.sort(key=lambda s: (s["date"] or "", s["time"] or ""))
    state = pc_state()
    payload = {"pc": pid, "host": _socket.gethostname(), "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
               "state": state, "items": items}
    try:
        rp = rd_payload()
    except Exception as e:
        rp = None; log("RustDesk запись: %s" % e)
    if rp:
        payload["remote"] = rp
    _apriv, _apub = admin_keys()
    if _apub and remote_on():
        payload["admin"] = {"pub": _apub, "on": True}
    _dpriv, _dpub = device_keys()
    grants = fleet_grants_out()
    rsig = "%s|%s|%s|%s|%s" % ((rp or {}).get("id", ""), (payload.get("admin") or {}).get("pub", ""),
                               "enc" if (rp or {}).get("enc") else "", ",".join(sorted((grants or {}).keys())), "F" if fleet_key() else "")
    if fleet_key():
        outer = {"pc": pid, "host": payload["host"], "updatedAt": payload["updatedAt"], "devicePub": _dpub,
                 "enc": fleet_seal(payload)}
        if payload.get("admin"): outer["admin"] = payload["admin"]
        if grants: outer["grants"] = grants
        body_txt = json.dumps(outer, ensure_ascii=False, indent=0)
    else:
        payload["devicePub"] = _dpub
        body_txt = json.dumps(payload, ensure_ascii=False, indent=0)
    h = hashlib.sha256((json.dumps(items, ensure_ascii=False, sort_keys=True) + _state_sign(state) + rsig).encode()).hexdigest()
    SELF_PC.update({k: payload[k] for k in ("pc", "host", "state")})
    if h == _bq_push_hash[0] and not force and time.time() - _bq_push_at[0] < HEARTBEAT_SEC:
        return "без изменений"
    try:
        gist_api("PATCH", files={"bq-%s.json" % pid: {"content": body_txt}}, token=tok)
    except Exception as e:
        SELF_PC["pushError"] = str(e)[:200]
        raise
    _bq_push_hash[0] = h
    _bq_push_at[0] = time.time()
    SELF_PC["updatedAt"] = payload["updatedAt"]
    SELF_PC.pop("pushError", None)
    return "выгружено банкетов: %d" % len(items)

def pull_banquets():
    """Скачать сводки банкетов остальных компьютеров → doc shared/banquets."""
    tok = gist_token()
    if not tok:
        return "нет доступа к общему списку"
    pid = pc_id()
    g = gist_api("GET", token=tok)
    pcs = {}
    for name, f in (g.get("files") or {}).items():
        if not (name.startswith("bq-") and name.endswith(".json")):
            continue
        try:
            txt = f.get("content")
            if f.get("truncated") and f.get("raw_url"):
                txt = urllib.request.urlopen(f["raw_url"], timeout=60).read().decode("utf-8")
            d = json.loads(txt)
        except Exception as ex:
            log("Общий список банкетов, файл %s: %s" % (name, ex))
            continue
        if d.get("pc") == pid:
            continue
        if not fleet_key() and (d.get("grants") or {}).get(pid):
            try:
                k = open_grant(d["grants"][pid])
                doc_set("secure/fleet", {"key": k.hex(), "at": datetime.datetime.now().isoformat(timespec="seconds")})
                log("Получен ключ кафе от компьютера владельца — общий список теперь шифруется")
            except Exception as ex:
                log("Выдача ключа кафе не подошла: %s" % ex)
        if d.get("enc"):
            outer = {k: d.get(k) for k in ("pc", "host", "updatedAt", "devicePub", "admin", "grants")}
            inner = None
            if fleet_key():
                try: inner = fleet_open(d["enc"])
                except Exception as ex: log("Файл %s не расшифрован: %s" % (name, ex))
            if inner is None:
                d = dict(outer, items=[], locked=True)
            else:
                d = dict(inner); d.update({k: v for k, v in outer.items() if v is not None}); d["sealed"] = True
        else:
            d["plain"] = True
        try:  # брошенные файлы (компьютер переустановлен) старше 30 дней не показываем
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(d.get("updatedAt"))
            if age.days > 30:
                if age.days > 180:
                    continue
                d = {"pc": d.get("pc"), "host": d.get("host"), "updatedAt": d.get("updatedAt"),
                     "state": d.get("state"), "items": [], "stale": True}
        except Exception:
            pass
        pcs[d.get("pc") or name] = d
    doc_set("shared/banquets", {"pcs": pcs, "updatedAt": datetime.datetime.now().isoformat(timespec="seconds")})
    n = sum(len(p.get("items") or []) for p in pcs.values())
    return "компьютеров: %d, банкетов: %d" % (len(pcs), n)

def sync_banquets(force=False):
    try:
        r1 = push_banquets(force)
    except Exception as e:
        r1 = "не выгружено: %s" % e
        log("Общий список банкетов (выгрузка): %s" % e)
    r2 = pull_banquets()
    SYNC_STATE["banquets"] = "%s; %s" % (r1, r2)
    return SYNC_STATE["banquets"]

def sync_employees():
    """Тянет сотрудников из iiko, PIN — из общего списка (iiko не отдаёт PIN-коды по API)."""
    ii = Iiko(CFG)
    try:
        ii.auth()
        emps, tagstats = ii.employees()
    finally:
        ii.logout()
    apply_pins(emps, load_shared_pins())
    with_pin = [e for e in emps if e.get("pin")]
    doc_set("config/employees", {"items": emps, "fields": tagstats,
                                 "syncedAt": datetime.datetime.now().isoformat()})
    res = "сотрудников: %d, из них с PIN: %d" % (len(emps), len(with_pin))
    SYNC_STATE["employees"] = res
    log("Синхронизация сотрудников: " + res)
    if not with_pin:
        log("ВНИМАНИЕ: ни у одного сотрудника не найден PIN в API — вход во фронт будет без PIN, пришлите logs Клоду.")
    return res

def keep_server_marks(path, new):
    """Браузер сохраняет банкет целиком из своей копии, которая может быть старее серверной.
    Отметки «оплата выгружена» и «акт создан» ставит сервер — не даём их затереть (иначе дубли в iiko)."""
    try:
        old = doc_get(path) or {}
        if not isinstance(new, dict) or not old:
            return new
        sent = {p.get("id") for p in (old.get("payments") or []) if p.get("exported")}
        for p in (new.get("payments") or []):
            if p.get("id") in sent:
                p["exported"] = True
        same_close = new.get("status") == "closed" and new.get("closedAt") and new.get("closedAt") == old.get("closedAt")
        if same_close and new.get("iikoStatus") == "pending":
            if old.get("iikoStatus") == "uploaded":
                new["iikoStatus"] = "uploaded"
                if old.get("iikoSentAt"): new["iikoSentAt"] = old["iikoSentAt"]
                new.pop("iikoError", None)
            elif old.get("iikoError"):
                new["iikoError"] = old["iikoError"]
        if not same_close:
            new.pop("iikoError", None); new.pop("iikoSentAt", None)
    except Exception as e:
        log("Сверка отметок банкета: %s" % e)
    return new

# ---------------- акты реализации ----------------
def bq_totals(b):
    sub = sum(float(i.get("price") or 0) * float(i.get("qty") or 0) for i in b.get("items", []))
    service = sub * float(b.get("servicePct") or 0) / 100
    discount = sub * float(b.get("discountPct") or 0) / 100 + float(b.get("discountRub") or 0)
    return round(sub + service - discount, 2)

IIKO_LOCK = threading.RLock()  # акты и ордера отправляет только один поток — иначе возможны дубли
AKT_GRACE = 120  # секунд после закрытия банкета до отправки акта: время передумать и переоткрыть
QUEUE = {"next": 0, "fails": 0, "sig": ""}

def _queue_sig():
    """Состав очереди (кто ждёт). Появился новый банкет или оплата — отправляем сразу, не дожидаясь паузы."""
    ids = []
    for x in col_get("banquets"):
        b = x.get("data") or {}
        if b.get("status") == "closed" and b.get("iikoStatus") == "pending" and _iso_age(b.get("closedAt")) >= AKT_GRACE:
            ids.append("a:" + x["id"])
        ids += ["p:%s" % p.get("id") for p in (b.get("payments") or []) if not p.get("exported")]
    return "|".join(sorted(ids))

def _iso_age(iso):
    """Сколько секунд прошло с момента iso (время из браузера — UTC с Z)."""
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return (datetime.datetime.now() - d).total_seconds()
        return (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds()
    except Exception:
        return 10 ** 9

def iiko_queue(grace=0):
    """Что ждёт отправки в iiko: (актов, оплат, самое давнее закрытие, ошибки по банкетам)."""
    akts, pays, oldest, errors = 0, 0, None, []
    for x in col_get("banquets"):
        b = x.get("data") or {}
        if b.get("status") == "closed" and b.get("iikoStatus") == "pending":
            if _iso_age(b.get("closedAt")) >= grace:
                akts += 1
            if b.get("closedAt") and (oldest is None or b["closedAt"] < oldest):
                oldest = b["closedAt"]
            if b.get("iikoError"):
                errors.append("№%s: %s" % (b.get("num", ""), str(b["iikoError"])[:700]))
        pays += len([p for p in (b.get("payments") or []) if not p.get("exported")])
    return akts, pays, oldest, errors

def _set_bq_error(x, b, err):
    """Запомнить в самом банкете, почему акт не ушёл (видно во фронте и в общем списке)."""
    if b.get("iikoError") != err:
        b["iikoError"] = err
        doc_set("banquets/" + x["id"], b)

def run_akty(shift_id=None):
    with IIKO_LOCK:
        try:
            return _run_akty(shift_id)
        except Exception as e:
            SYNC_STATE["last_akt"] = datetime.datetime.now().isoformat()
            SYNC_STATE["last_akt_result"] = "нет связи с iiko: %s" % e
            log("Акты реализации: нет связи с iiko: %s" % e)
            raise

def _run_akty(shift_id=None):
    """Создаёт акты реализации по закрытым банкетам со статусом pending.
    shift_id — только по банкетам этой кассовой смены (вызов при закрытии смены, как в iiko)."""
    pend = [x for x in col_get("banquets")
            if x["data"].get("status") == "closed" and x["data"].get("iikoStatus") == "pending"
            and (shift_id is None or x["data"].get("shiftId") == shift_id)]
    if not pend:
        SYNC_STATE["last_akt"] = datetime.datetime.now().isoformat()
        SYNC_STATE["last_akt_result"] = "очередь пуста"
        return "очередь пуста"
    st = doc_get("config/structure") or {}
    rms_by_id = {r["id"]: r for r in st.get("rms", [])}
    front_by_id = {f["id"]: f for f in st.get("fronts", [])}
    dry = dry_run()
    ii = Iiko(CFG)
    done, skipped = 0, []
    try:
        ii.auth()
        deps = ii.departments(); stores = ii.stores(); concs = ii.conceptions()
        prods = {(p.get("name") or "").strip(): p["id"] for p in ii.products() if p.get("id")}
        for x in pend:
            b = x["data"]
            fr = front_by_id.get(b.get("frontId")) or {}
            rms = rms_by_id.get(fr.get("rmsId")) or {}
            store_id = stores.get((fr.get("store") or "").strip())
            conc_id = concs.get((fr.get("conception") or "").strip())
            problems = []
            if not store_id: problems.append("склад «%s» не найден" % fr.get("store"))
            if not conc_id: problems.append("концепция «%s» не найдена" % fr.get("conception"))
            items_xml = []
            for it in b.get("items", []):
                pid = it.get("productId") or prods.get((it.get("name") or "").strip())
                if not pid:
                    problems.append("позиция «%s» не найдена в номенклатуре" % it.get("name"))
                    continue
                s = round(float(it.get("price") or 0) * float(it.get("qty") or 0), 2)
                items_xml.append(
                    "<item><productId>%s</productId><amount>%s</amount><sum>%s</sum></item>"
                    % (pid, it.get("qty"), s))
            if problems:
                skipped.append("банкет %s (%s): %s" % (b.get("date"),
                    (b.get("client") or {}).get("name", ""), "; ".join(problems)))
                _set_bq_error(x, b, "; ".join(problems))
                continue
            comment = "Банкет %s, %s, фронт %s. Итог %s" % (
                b.get("date"), (b.get("client") or {}).get("name", ""), fr.get("name"), bq_totals(b))
            xml_body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<document>"
                "<dateIncoming>%sT20:00:00</dateIncoming>"
                "<status>NEW</status>"
                "<storeId>%s</storeId>"
                "<conceptionId>%s</conceptionId>"
                "<comment>%s</comment>"
                "<items>%s</items>"
                "</document>") % (b.get("date"), store_id, conc_id, xml_escape(comment), "".join(items_xml))
            fn = os.path.join(LOG_DIR, "akt-%s-%s.xml" % (b.get("date"), x["id"][:8]))
            with open(fn, "w", encoding="utf-8") as f:
                f.write(xml_body)
            if dry:
                skipped.append("банкет %s: DRY-RUN, акт не отправлен (файл %s)" % (b.get("date"), os.path.basename(fn)))
                continue
            try:
                resp = ii.import_sales_act(xml_body)
                with open(fn + ".response.txt", "w", encoding="utf-8") as f:
                    f.write(resp)
                if re.search(r"<valid>\s*false", resp or "", re.I):
                    raise Exception("документ не прошёл проверку iiko: " + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp)).strip()[:600])
                b["iikoStatus"] = "uploaded"
                b["iikoSentAt"] = datetime.datetime.now().isoformat(timespec="seconds")
                b.pop("iikoError", None)
                doc_set("banquets/" + x["id"], b)
                done += 1
            except Exception as e:
                skipped.append("банкет %s: ошибка iiko: %s" % (b.get("date"), e))
                _set_bq_error(x, b, "iiko не принял акт: %s" % str(e)[:700])
    finally:
        ii.logout()
    res = "создано актов: %d; пропущено: %d" % (done, len(skipped))
    if skipped:
        res += "\n" + "\n".join(skipped)
    SYNC_STATE["last_akt"] = datetime.datetime.now().isoformat()
    SYNC_STATE["last_akt_result"] = res
    log("Акты реализации: " + res.replace("\n", " | "))
    return res

# ---------------- оплаты для бухгалтерии (приходные кассовые ордера) ----------------
PAY_METHODS = {"cash": "Наличные", "qr": "QR код", "otkr": "Открытие", "sber": "Сбербанк",
               "acq": "Эквайринг", "account": "Безналичный расчет", "card": "Банковская карта"}
def xml_escape(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def run_payments():
    with IIKO_LOCK:
        try:
            r = _run_payments()
        except Exception as e:
            SYNC_STATE["last_pay"] = datetime.datetime.now().isoformat()
            SYNC_STATE["last_pay_result"] = "нет связи с iiko: %s" % e
            log("Кассовые ордера по оплатам: нет связи с iiko: %s" % e)
            raise
        if r != "новых оплат нет":
            SYNC_STATE["last_pay"] = datetime.datetime.now().isoformat()
            SYNC_STATE["last_pay_result"] = r
        return r

def flush_iiko():
    """Очередь в iiko: акты по закрытым банкетам (через AKT_GRACE после закрытия) и неотправленные оплаты.
    При неудаче повторяет: через 3, 6, 12… минут, но не реже раза в час."""
    if not (CFG.get("iiko_login") and CFG.get("iiko_password")) or dry_run():
        return
    if time.time() < QUEUE["next"] and _queue_sig() == QUEUE["sig"]:
        return
    akts, pays, _, _ = iiko_queue(grace=AKT_GRACE)
    if not (akts or pays):
        QUEUE["fails"] = 0
        return
    if akts:
        try: run_akty()
        except Exception: pass
    if pays:
        try: run_payments()
        except Exception: pass
    akts2, pays2, _, _ = iiko_queue(grace=AKT_GRACE)
    if akts2 or pays2:
        QUEUE["fails"] += 1
        QUEUE["sig"] = _queue_sig()
        delay = min(180 * 2 ** (QUEUE["fails"] - 1), 3600)
        QUEUE["next"] = time.time() + delay
        log("Очередь в iiko: не ушло актов: %d, оплат: %d. Повтор через %d мин." % (akts2, pays2, delay // 60))
    else:
        QUEUE["fails"] = 0
        QUEUE["next"] = 0
    try: sync_banquets()  # результат сразу виден остальным компьютерам
    except Exception: pass

def _run_payments():
    """Выгружает новые предоплаты банкетов в iiko приходными кассовыми ордерами,
    чтобы бухгалтер видел оплаты в фин.модуле, как при работе через iikoFront."""
    st = doc_get("config/structure") or {}
    rms_by_id = {r["id"]: r for r in st.get("rms", [])}
    front_by_id = {f["id"]: f for f in st.get("fronts", [])}
    todo = []
    for x in col_get("banquets"):
        for p in (x["data"].get("payments") or []):
            if not p.get("exported"):
                todo.append((x, p))
    if not todo:
        return "новых оплат нет"
    dry = dry_run()
    ii = Iiko(CFG)
    done, skipped = 0, []
    try:
        ii.auth()
        deps = ii.departments()
        for x, p in todo:
            b = x["data"]
            fr = front_by_id.get(b.get("frontId")) or {}
            rms = rms_by_id.get(fr.get("rmsId")) or {}
            dep_id = deps.get((rms.get("iikoName") or "").strip())
            if not dep_id:
                skipped.append("оплата %s: подразделение «%s» не найдено" % (p.get("id"), rms.get("iikoName")))
                continue
            mname = PAY_METHODS.get(p.get("method"), p.get("method") or "")
            comment = "Банкет №%s от %s, %s. %s: %s ₽, способ: %s. Принял(а): %s, фронт %s" % (
                b.get("num", ""), b.get("date"), (b.get("client") or {}).get("name", ""),
                "Оплата" if b.get("status") == "closed" else "Предоплата",
                p.get("amount"), mname, p.get("by") or "—", fr.get("name", ""))
            extra = "".join("<%s>%s</%s>" % (k, v, k) for k, v in ((CFG.get("pko_fields") or {}).get(p.get("method")) or {}).items())
            xml_body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<document>"
                "<dateIncoming>%s</dateIncoming>"
                "<departmentId>%s</departmentId>"
                "<sum>%s</sum>"
                "<comment>%s</comment>%s"
                "</document>") % (p.get("date"), dep_id, p.get("amount"), xml_escape(comment), extra)
            fn = os.path.join(LOG_DIR, "pko-%s-%s.xml" % (p.get("date"), str(p.get("id"))[:8]))
            with open(fn, "w", encoding="utf-8") as f:
                f.write(xml_body)
            if dry:
                skipped.append("оплата %s ₽ (%s): DRY-RUN, файл %s" % (p.get("amount"), p.get("date"), os.path.basename(fn)))
                continue
            try:
                ep = CFG.get("pko_endpoint", "/resto/api/documents/import/incomingCashOrder")
                resp = ii._req(ep, data=xml_body.encode("utf-8"), headers={"Content-Type": "application/xml"})
                with open(fn + ".response.txt", "w", encoding="utf-8") as f:
                    f.write(resp)
                p["exported"] = True
                doc_set("banquets/" + x["id"], b)
                done += 1
            except Exception as e:
                skipped.append("оплата %s: ошибка iiko: %s" % (p.get("id"), e))
    finally:
        ii.logout()
    res = "выгружено оплат: %d; пропущено: %d" % (done, len(skipped))
    if skipped:
        res += "\n" + "\n".join(skipped)
    log("Кассовые ордера по оплатам: " + res.replace("\n", " | "))
    return res

# ---------------- планировщик ----------------
def scheduler():
    last_akt_day = None
    last_price_sync = 0
    last_update_check = 0
    last_pins = 0
    last_bq = 0
    while True:
        try:
            now = datetime.datetime.now()
            if time.time() - last_pins > 600:
                last_pins = time.time()
                try: refresh_pins()
                except Exception as e: log("Обновление PIN: %s" % e)
                try: load_shared_settings()
                except Exception as e: log("Общие настройки: %s" % e)
            if time.time() - last_bq > float(CFG.get("banquets_sync_min", 3)) * 60:
                last_bq = time.time()
                try: sync_banquets()
                except Exception as e: log("Общий список банкетов: %s" % e)
            if CFG.get("iiko_login") and CFG.get("iiko_password"):
                if time.time() - last_price_sync > float(CFG.get("menu_sync_hours", 6)) * 3600:
                    last_price_sync = time.time()
                    try: sync_employees()
                    except Exception as e: log("Ошибка синка сотрудников: %s" % e)
                    try: sync_menu()
                    except Exception as e: log("Ошибка синка меню: %s" % e)
                    try: sync_margins()
                    except Exception as e: log("Ошибка синка маржинальности: %s" % e)
            if time.time() - last_update_check > 3600:
                last_update_check = time.time()
                try: check_update()
                except Exception as e: log("Проверка обновлений: %s" % e)
            try: flush_iiko()
            except Exception as e: log("Очередь в iiko: %s" % e)
            try: rd_tick()
            except Exception as e: log("Удалённый доступ: %s" % e)
            hh, mm = CFG.get("akt_time", "23:00").split(":")
            if CFG.get("iiko_login") and now.hour == int(hh) and now.minute >= int(mm) and last_akt_day != now.date():
                last_akt_day = now.date()
                try: run_akty()
                except Exception as e: log("Ошибка актов: %s" % e)
                try: run_payments()
                except Exception as e: log("Ошибка выгрузки оплат: %s" % e)
        except Exception as e:
            log("Планировщик: %s" % e)
        time.sleep(30)

# ---------------- HTTP ----------------
SHIM = """<script>
window.claude={use:async function(n){return {
 doc:function(p){return {
   get:async function(){var r=await fetch('/api/doc/'+encodeURIComponent(p));if(!r.ok)return null;var t=await r.text();return t?JSON.parse(t):null;},
   set:async function(d){var r=await fetch('/api/doc/'+encodeURIComponent(p),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok)throw new Error('save failed');}
 };},
 collection:function(p){return {
   get:async function(){var r=await fetch('/api/col/'+encodeURIComponent(p));return r.ok?await r.json():[];},
   onSnapshot:function(cb){var poll=async function(){try{var r=await fetch('/api/col/'+encodeURIComponent(p));if(r.ok)cb(await r.json());}catch(e){}};setInterval(poll,10000);}
 };}
};}};
</script>
"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # тихий access-log
        pass

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path in ("/", "/index.html"):
            with open(FRONT_PATH, encoding="utf-8") as f:
                html = f.read()
            html = html.replace("<script>", SHIM + "<script>", 1)
            return self._send(200, html, "text/html; charset=utf-8")
        if p.path.startswith("/api/doc/"):
            path = urllib.parse.unquote(p.path[len("/api/doc/"):])
            d = doc_get(path)
            return self._send(200, json.dumps(d, ensure_ascii=False) if d is not None else "")
        if p.path.startswith("/api/col/"):
            prefix = urllib.parse.unquote(p.path[len("/api/col/"):])
            return self._send(200, json.dumps(col_get(prefix), ensure_ascii=False))
        if p.path == "/api/debug/log":
            qq = urllib.parse.parse_qs(p.query)
            n = int((qq.get("n") or ["120"])[0])
            fname = os.path.basename((qq.get("f") or ["server.log"])[0])
            try:
                with open(os.path.join(LOG_DIR, fname), encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-n:]
            except Exception as e:
                lines = ["нет лога: %s" % e]
            return self._send(200, "".join(lines), "text/plain; charset=utf-8")
        if p.path == "/setup-update-token":
            return self._send(200, SETUP_UPDATE_TOKEN_HTML, "text/html; charset=utf-8")
        if p.path == "/setup-token":
            return self._send(200, SETUP_TOKEN_HTML, "text/html; charset=utf-8")
        if p.path in ("/favicon.png", "/favicon.ico", "/front.ico"):
            fn = "front.ico" if p.path.endswith(".ico") else "favicon.png"
            try:
                data = open(os.path.join(BASE, fn), "rb").read()
            except Exception:
                return self._send(404, "")
            return self._send(200, data, "image/x-icon" if fn.endswith(".ico") else "image/png")
        if p.path == "/api/debug/employee":
            # все поля сотрудника в том виде, как их отдаёт iiko (поиск по части ФИО)
            q = (urllib.parse.parse_qs(p.query).get("q") or [""])[0].lower().strip()
            try:
                ii = Iiko(CFG); ii.auth()
                try:
                    xmls = ii._req("/resto/api/employees")
                finally:
                    ii.logout()
                out = []
                for el in ET.fromstring(xmls):
                    d = {}
                    for c in el:
                        d[c.tag] = (c.text or "").strip() if len(c) == 0 else [(x.text or "").strip() for x in c]
                    nm = (d.get("name") or "") if isinstance(d.get("name"), str) else ""
                    if q and q in nm.lower():
                        out.append(d)
                return self._send(200, json.dumps(out, ensure_ascii=False, indent=1))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        if p.path == "/api/debug/net":
            out = {}
            q = urllib.parse.parse_qs(p.query)
            for u in (q.get("u") or ["https://ya.ru", "https://timeweb.cloud", "https://s3.twcstorage.ru", "https://lyubava-co.iiko.it/resto/get_server_info.jsp"]):
                t0 = time.time()
                try:
                    host = urllib.parse.urlparse(u).hostname
                    ips = sorted({r[4][0] for r in _orig_getaddrinfo(host, 443)})
                    with urllib.request.urlopen(u, timeout=8) as r:
                        out[u] = {"status": r.status, "ms": int((time.time()-t0)*1000), "ips": ips}
                except Exception as e:
                    out[u] = {"error": str(e)[:120], "ms": int((time.time()-t0)*1000)}
            out["proxies"] = urllib.request.getproxies()
            return self._send(200, json.dumps(out, ensure_ascii=False))
        if p.path == "/api/debug/product":
            q = urllib.parse.parse_qs(p.query)
            names = set(q.get("name", []))
            try:
                ii = Iiko(CFG); ii.auth()
                try:
                    out = [x for x in ii.products() if (x.get("name") or "").strip() in names]
                finally:
                    ii.logout()
                return self._send(200, json.dumps(out, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        if p.path == "/api/debug/price":
            # диагностика: сырой ответ iiko по ценам первого подразделения (только для отладки)
            try:
                ii = Iiko(CFG); ii.auth()
                try:
                    deps = ii.departments()
                    st = doc_get("config/structure") or {}
                    r0 = (st.get("rms") or [{}])[0]
                    dep_id = deps.get((r0.get("iikoName") or "").strip())
                    day = datetime.date.today().isoformat()
                    raw = ii._req("/resto/api/v2/price", {"dateFrom": day, "dateTo": day,
                                                          "departmentId": dep_id, "type": "BASE"})
                    raw2 = ii._req("/resto/api/v2/price", {"dateFrom": day, "departmentId": dep_id})
                    out = {"dept": r0.get("iikoName"), "dep_id": dep_id,
                           "withDateToAndType_len": len(raw), "withDateToAndType_head": raw[:1500],
                           "onlyDateFrom_len": len(raw2), "onlyDateFrom_head": raw2[:1500]}
                finally:
                    ii.logout()
                return self._send(200, json.dumps(out, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        if p.path == "/api/pcs":
            # состояние всех компьютеров с фронтом: этот — живьём, остальные — из общего списка
            me = dict(SELF_PC) if SELF_PC.get("pc") else {"pc": pc_id(), "host": _socket.gethostname()}
            me["state"] = pc_state(); me["self"] = True
            if not gist_token():
                me["pushError"] = "нет доступа к общему списку (ключ GitHub)"
            sh = doc_get("shared/banquets") or {}
            others = []
            for d in (sh.get("pcs") or {}).values():
                its = d.get("items") or []
                rem = d.get("remote") or {}
                others.append({"pc": d.get("pc"), "host": d.get("host"), "updatedAt": d.get("updatedAt"),
                               "state": d.get("state"), "banquets": len(its),
                               "aktWait": len([i for i in its if i.get("status") == "closed" and i.get("iikoStatus") == "pending"]),
                               "remote": {"id": rem.get("id"), "status": rem.get("status"), "hasEnc": bool(rem.get("enc"))},
                               "devicePub": bool(d.get("devicePub")), "locked": bool(d.get("locked")), "plain": bool(d.get("plain")),
                               "granted": d.get("pc") in (((doc_get("secure/fleet_grants") or {}).get("items")) or {})})
            others.sort(key=lambda d: d.get("updatedAt") or "", reverse=True)
            me["remote"] = {"id": RD.get("id"), "status": RD.get("status")}
            return self._send(200, json.dumps({"now": datetime.datetime.now().isoformat(timespec="seconds"),
                                               "pulledAt": sh.get("updatedAt"),
                                               "remoteOn": bool(remote_on() or known_admin_pub()),
                                               "canConnect": is_admin_pc(),
                                               "fleetOn": bool(fleet_key()), "canGrant": is_owner_admin(),
                                               "pcs": [me] + others}, ensure_ascii=False))
        if p.path == "/api/status":
            return self._send(200, json.dumps({
                "dry_run": dry_run(),
                "iiko_configured": bool(CFG.get("iiko_login") and CFG.get("iiko_password")),
                "version": local_version(),
                **SYNC_STATE}, ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/api/doc/"):
            doc_del(urllib.parse.unquote(p.path[len("/api/doc/"):]))
            return self._send(200, '{"ok":true}')
        return self._send(404, '{"error":"not found"}')

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/api/doc/"):
            path = urllib.parse.unquote(p.path[len("/api/doc/"):])
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln).decode("utf-8")
            data = json.loads(body)
            if path.startswith("banquets/"):
                with IIKO_LOCK:
                    doc_set(path, keep_server_marks(path, data))
            else:
                doc_set(path, data)
            return self._send(200, '{"ok":true}')
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        try:
            if p.path == "/api/sync":
                r = sync_menu() + "; " + sync_employees()
                try: r += "; " + sync_margins()
                except Exception as e: r += "; маржинальность: %s" % e
                return self._send(200, json.dumps({"result": r}, ensure_ascii=False))
            if p.path == "/api/publish":
                # публикация СВОИХ файлов обновления по заранее выданным ссылкам загрузчика
                # (только server.py / front.html, никогда не config.json)
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                res = {}
                for name, spec in (body.get("files") or {}).items():
                    if name not in UPDATABLE:
                        res[name] = "запрещено"; continue
                    data = open(os.path.join(BASE, name), "rb").read()
                    req = urllib.request.Request(spec["url"], data=data, method="PUT")
                    req.add_header("Content-Type", spec.get("content_type") or "application/octet-stream")
                    try:
                        with urllib.request.urlopen(req, timeout=120) as r:
                            res[name] = {"status": r.status, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    except Exception as e:
                        res[name] = {"error": str(e)}
                return self._send(200, json.dumps(res, ensure_ascii=False))
            if p.path == "/api/release":
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                ver = body.get("version") or datetime.datetime.now().strftime("%Y.%m.%d.%H%M")
                return self._send(200, json.dumps({"result": release(ver)}, ensure_ascii=False))
            if p.path == "/api/update":
                return self._send(200, json.dumps({"result": check_update(force=True)}, ensure_ascii=False))
            if p.path == "/api/restart":
                threading.Timer(0.5, self_restart, args=("по запросу",)).start()
                return self._send(200, '{"result":"restarting"}')
            if p.path == "/api/akty":
                return self._send(200, json.dumps({"result": run_akty()}, ensure_ascii=False))
            if p.path == "/api/setup-update-token":
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                try:
                    return self._send(200, json.dumps({"ok": True, "result": setup_update_token(body.get("token"))}, ensure_ascii=False))
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": "Ключ не принят: %s" % e}, ensure_ascii=False))
            if p.path == "/api/setup-token":
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                try:
                    r = setup_token(body.get("token"))
                    # PIN-коды, сохранённые пока только на этом компьютере, — теперь в общий список
                    ov = doc_get("config/pin_overrides") or {"items": {}}
                    moved = 0
                    for emp_id, pin in list((ov.get("items") or {}).items()):
                        try:
                            set_shared_pin(emp_id, pin); ov["items"].pop(emp_id, None); moved += 1
                        except Exception as ex:
                            log("PIN в общий список: %s" % ex)
                    doc_set("config/pin_overrides", ov)
                    if moved: r += "; PIN-кодов перенесено в общий список: %d" % moved
                    try: refresh_pins()
                    except Exception: pass
                    try: sync_banquets(force=True)
                    except Exception: pass
                    return self._send(200, json.dumps({"ok": True, "result": r}, ensure_ascii=False))
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": "Ключ не принят: %s" % e}, ensure_ascii=False))
            if p.path == "/api/banquets/sync":
                # вызывается фронтом после оплаты/закрытия/смены статуса — чтобы другие залы увидели сразу
                return self._send(200, json.dumps({"result": sync_banquets(force=True)}, ensure_ascii=False))
            if p.path == "/api/pins":
                # PIN сотрудника из настроек фронта → общий список для всех фронтов
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                emp_id, pin = body.get("id") or "", str(body.get("pin") or "").strip()
                if not emp_id:
                    return self._send(400, '{"error":"нет id сотрудника"}')
                if (body.get("by") or "") not in CFG.get("pin_admins", [OWNER_ID]):
                    return self._send(403, json.dumps({"error": "PIN-коды выдаёт только владелец"}, ensure_ascii=False))
                try:
                    shared = set_shared_pin(emp_id, pin)
                    ov = doc_get("config/pin_overrides") or {"items": {}}
                    (ov.get("items") or {}).pop(emp_id, None)
                    doc_set("config/pin_overrides", ov)
                    refresh_pins(shared)
                    return self._send(200, json.dumps({"ok": True, "shared": True, "result": "PIN сохранён для всех фронтов"}, ensure_ascii=False))
                except Exception as e:
                    ov = doc_get("config/pin_overrides") or {"items": {}}
                    ov.setdefault("items", {})
                    if pin: ov["items"][emp_id] = pin
                    else: ov["items"].pop(emp_id, None)
                    doc_set("config/pin_overrides", ov)
                    refresh_pins()
                    log("PIN в общий список не записан: %s" % e)
                    return self._send(200, json.dumps({"ok": True, "shared": False, "result": str(e)}, ensure_ascii=False))
            if p.path in ("/api/fleet/enable", "/api/fleet/approve"):
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                if (body.get("by") or "") not in CFG.get("pin_admins", [OWNER_ID]):
                    return self._send(403, json.dumps({"error": "шифрованием управляет только владелец"}, ensure_ascii=False))
                try:
                    r = fleet_enable() if p.path == "/api/fleet/enable" else fleet_approve(body.get("pc"))
                    return self._send(200, json.dumps({"ok": True, "result": r}, ensure_ascii=False))
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            if p.path in ("/api/remote/enable", "/api/remote/disable", "/api/remote/connect"):
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                if (body.get("by") or "") not in CFG.get("pin_admins", [OWNER_ID]):
                    return self._send(403, json.dumps({"error": "удалённым доступом управляет только владелец"}, ensure_ascii=False))
                if p.path == "/api/remote/enable":
                    ensure_admin_keys()
                    dd = doc_get("secure/remote_admin") or {}; dd["on"] = True; doc_set("secure/remote_admin", dd)
                    threading.Thread(target=lambda: (rd_setup(), sync_banquets(force=True)), daemon=True).start()
                    return self._send(200, json.dumps({"ok": True, "result": "удалённый доступ включён, этот компьютер — управляющий"}, ensure_ascii=False))
                if p.path == "/api/remote/disable":
                    dd = doc_get("secure/remote_admin") or {}; dd["on"] = False; doc_set("secure/remote_admin", dd)
                    try: sync_banquets(force=True)
                    except Exception: pass
                    return self._send(200, json.dumps({"ok": True, "result": "удалённый доступ выключен"}, ensure_ascii=False))
                try:
                    r = rd_connect(body.get("pc"))
                    return self._send(200, json.dumps({"ok": True, "result": "подключаюсь к %s" % (r.get("host") or r.get("id"))}, ensure_ascii=False))
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            if p.path == "/api/push":
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                kind = body.get("kind")
                if not (CFG.get("iiko_login") and CFG.get("iiko_password")):
                    return self._send(200, '{"result":"iiko не настроен"}')
                if kind == "pko":  # предоплата внесена — сразу кассовый ордер в iiko
                    r = run_payments()
                elif kind == "shift_close":  # смена закрыта — акты по её банкетам + ордера, как в iiko
                    r = run_akty(shift_id=body.get("shiftId")) + "; " + run_payments()
                else:
                    r = "неизвестное событие"
                return self._send(200, json.dumps({"result": r}, ensure_ascii=False))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

HTTPD = None
RESTART = {"reason": ""}
def self_restart(reason):
    """Перезапуск: останавливаем serve_forever, дальше main() сам запустит новую копию и выйдет."""
    log("Перезапуск сервера: " + reason)
    RESTART["reason"] = reason
    try:
        if HTTPD:
            threading.Thread(target=HTTPD.shutdown, daemon=True).start()
    except Exception as e:
        log("shutdown: %s" % e)
        os._exit(0)

def spawn_new_copy():
    import subprocess
    args = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]
    args += ["--no-browser", "--restarted"]
    flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    subprocess.Popen(args, cwd=BASE, close_fds=True, creationflags=flags)
    log("Новая копия запущена, завершаю старую")

def code_watcher():
    """Следит за server.py и config.json: изменились — перезапуск сам."""
    paths = [os.path.abspath(__file__), CONFIG_PATH]
    stamp = {p: os.path.getmtime(p) for p in paths if os.path.exists(p)}
    while True:
        time.sleep(3)
        for p in paths:
            try:
                m = os.path.getmtime(p)
            except Exception:
                continue
            if stamp.get(p) is not None and m != stamp[p]:
                if UPDATING[0]:
                    stamp[p] = m
                    continue  # обновление само перезапустит сервер, когда скачает все файлы
                time.sleep(1)  # дождаться окончания записи файла
                self_restart("обновлён " + os.path.basename(p))
            stamp[p] = m

def open_front_window():
    """Открывает окно фронта отдельным приложением (без адресной строки), как iikoFront."""
    url = "http://localhost:%d" % int(CFG.get("port", 8100))
    import subprocess
    cands = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    for c in cands:
        if os.path.exists(c):
            subprocess.Popen([c, "--app=" + url, "--start-maximized"])
            return
    import webbrowser
    webbrowser.open(url)

def port_busy(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False

def ensure_branding():
    """Иконка фронта (логотип «Любава» + «фронт») на ярлыках рабочего стола и меню «Пуск»."""
    if os.name != "nt":
        return
    try:
        if not os.path.exists(ICON_PATH):
            check_update(force=True)  # докачать иконку, если версия пришла без неё
        if not os.path.exists(ICON_PATH):
            return
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        script = os.path.abspath(__file__)
        ps = (
            "$sh = New-Object -ComObject WScript.Shell\r\n"
            "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {\r\n"
            "  $l = $sh.CreateShortcut((Join-Path $d 'Банкетный фронт.lnk'))\r\n"
            "  $l.TargetPath = '%s'\r\n"
            "  $l.Arguments = '\"%s\"'\r\n"
            "  $l.WorkingDirectory = '%s'\r\n"
            "  $l.IconLocation = '%s,0'\r\n"
            "  $l.Description = 'Банкетный фронт Любава'\r\n"
            "  $l.Save()\r\n"
            "}\r\n"
            "ie4uinit.exe -show\r\n"
        ) % (exe.replace("'", "''"), script.replace("'", "''"), BASE.replace("'", "''"), ICON_PATH.replace("'", "''"))
        p = os.path.join(BASE, "logs", "shortcuts.ps1")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write(ps)
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p],
                       timeout=60, creationflags=0x08000000)
        log("Ярлыки «Банкетный фронт» с логотипом обновлены (рабочий стол и «Пуск»)")
    except Exception as e:
        log("Ярлыки: %s" % e)

def main():
    global HTTPD
    port = int(CFG.get("port", 8100))
    no_browser = "--no-browser" in sys.argv
    if "--restarted" in sys.argv:
        # предыдущая копия ещё завершается — ждём, пока порт освободится (до 90 с)
        log("Новая копия после перезапуска: жду освобождения порта %d" % port)
        for _ in range(180):
            if not port_busy(port):
                break
            time.sleep(0.5)
        else:
            log("Порт %d так и не освободился — выхожу" % port)
    if port_busy(port):
        # сервер уже работает (автозапуск) — просто открыть окно фронта
        if not no_browser:
            open_front_window()
        return
    seed_if_empty()
    threading.Thread(target=ensure_branding, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=code_watcher, daemon=True).start()
    log("Банкетный фронт запущен: http://localhost:%d" % port)
    if not (CFG.get("iiko_login") and CFG.get("iiko_password")):
        log("ВНИМАНИЕ: iiko_login/iiko_password не заполнены в config.json — синк цен и акты отключены.")
    try: load_shared_settings()
    except Exception: pass
    if dry_run():
        log("Режим DRY-RUN: акты формируются в файлы logs/akt-*.xml, но в iiko НЕ отправляются.")
    else:
        log("БОЕВОЙ РЕЖИМ: акты реализации и кассовые ордера отправляются в iiko.")
    for attempt in range(20):  # порт может освобождаться пару секунд после перезапуска
        try:
            HTTPD = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(1)
    if not no_browser:
        threading.Timer(1.5, open_front_window).start()
    try:
        HTTPD.serve_forever()
    except OSError as e:
        log("serve_forever прерван: %s" % e)
    try:
        HTTPD.server_close()
    except Exception:
        pass
    if RESTART["reason"]:
        try:
            spawn_new_copy()
        except Exception as e:
            log("Не удалось запустить новую копию: %s" % e)
    os._exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as _e:
        import traceback
        log("КРИТИЧЕСКАЯ ОШИБКА: %s\n%s" % (_e, traceback.format_exc()))
        raise
