# -*- coding: utf-8 -*-
"""
Банкетный фронт «Любава» — автономный сервер.
Хостит веб-фронт в локальной сети, хранит банкеты в SQLite,
синхронизирует цены из iiko и создаёт акты реализации по iiko API.
Только стандартная библиотека Python — никаких зависимостей.

Запуск:  py server.py
"""
import json, sqlite3, hashlib, threading, time, datetime, os, sys, re
import urllib.request, urllib.parse
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
UPDATABLE = ("server.py", "front.html")
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
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

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
    url = CFG.get("update_url")
    if not url:
        return "адрес обновлений не задан"
    man = json.loads(urllib.request.urlopen(url + ("&" if "?" in url else "?") + "t=%d" % int(time.time()),
                                            timeout=30).read().decode("utf-8"))
    ver = str(man.get("version", ""))
    if not ver:
        return "манифест без версии"
    if ver == local_version() and not force:
        return "установлена последняя версия " + ver
    changed = []
    for name, info in (man.get("files") or {}).items():
        if name not in UPDATABLE:
            continue
        data = urllib.request.urlopen(info["url"], timeout=60).read()
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

def sync_employees():
    """Тянет сотрудников с PIN-кодами из iiko — для входа во фронт, как в iikoFront."""
    ii = Iiko(CFG)
    try:
        ii.auth()
        emps, tagstats = ii.employees()
    finally:
        ii.logout()
    # PIN-коды, заданные вручную в настройках фронта, важнее кода из iiko
    ov = (doc_get("config/pin_overrides") or {}).get("items") or {}
    for e in emps:
        if ov.get(e.get("id")):
            e["pin"] = ov[e["id"]]
    with_pin = [e for e in emps if e.get("pin")]
    doc_set("config/employees", {"items": emps, "fields": tagstats,
                                 "syncedAt": datetime.datetime.now().isoformat()})
    res = "сотрудников: %d, из них с PIN: %d" % (len(emps), len(with_pin))
    SYNC_STATE["employees"] = res
    log("Синхронизация сотрудников: " + res)
    if not with_pin:
        log("ВНИМАНИЕ: ни у одного сотрудника не найден PIN в API — вход во фронт будет без PIN, пришлите logs Клоду.")
    return res

# ---------------- акты реализации ----------------
def bq_totals(b):
    sub = sum(float(i.get("price") or 0) * float(i.get("qty") or 0) for i in b.get("items", []))
    service = sub * float(b.get("servicePct") or 0) / 100
    discount = sub * float(b.get("discountPct") or 0) / 100 + float(b.get("discountRub") or 0)
    return round(sub + service - discount, 2)

def run_akty(shift_id=None):
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
    dry = bool(CFG.get("dry_run", True))
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
                "</document>") % (b.get("date"), store_id, conc_id, comment, "".join(items_xml))
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
                b["iikoStatus"] = "uploaded"
                doc_set("banquets/" + x["id"], b)
                done += 1
            except Exception as e:
                skipped.append("банкет %s: ошибка iiko: %s" % (b.get("date"), e))
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
def run_payments():
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
    dry = bool(CFG.get("dry_run", True))
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
            comment = "Предоплата банкета №%s от %s, %s (%s)" % (
                b.get("num", ""), b.get("date"), (b.get("client") or {}).get("name", ""), p.get("method"))
            xml_body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<document>"
                "<dateIncoming>%s</dateIncoming>"
                "<departmentId>%s</departmentId>"
                "<sum>%s</sum>"
                "<comment>%s</comment>"
                "</document>") % (p.get("date"), dep_id, p.get("amount"), comment)
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
    while True:
        try:
            now = datetime.datetime.now()
            if CFG.get("iiko_login") and CFG.get("iiko_password"):
                if time.time() - last_price_sync > float(CFG.get("menu_sync_hours", 6)) * 3600:
                    last_price_sync = time.time()
                    try: sync_menu()
                    except Exception as e: log("Ошибка синка меню: %s" % e)
                    try: sync_employees()
                    except Exception as e: log("Ошибка синка сотрудников: %s" % e)
            if time.time() - last_update_check > 3600:
                last_update_check = time.time()
                try: check_update()
                except Exception as e: log("Проверка обновлений: %s" % e)
                hh, mm = CFG.get("akt_time", "23:00").split(":")
                if now.hour == int(hh) and now.minute >= int(mm) and last_akt_day != now.date():
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
        if p.path == "/api/status":
            return self._send(200, json.dumps({
                "dry_run": CFG.get("dry_run", True),
                "iiko_configured": bool(CFG.get("iiko_login") and CFG.get("iiko_password")),
                "version": local_version(),
                **SYNC_STATE}, ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

    def do_PUT(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/api/doc/"):
            path = urllib.parse.unquote(p.path[len("/api/doc/"):])
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln).decode("utf-8")
            doc_set(path, json.loads(body))
            return self._send(200, '{"ok":true}')
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        try:
            if p.path == "/api/sync":
                r = sync_menu() + "; " + sync_employees()
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
def self_restart(reason):
    """Перезапуск сервера тем же процессом-заменой (без участия пользователя)."""
    log("Перезапуск сервера: " + reason)
    try:
        if HTTPD:
            HTTPD.server_close()
    except Exception:
        pass
    try:
        args = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]
        os.execv(sys.executable, args + ["--no-browser"])
    except Exception as e:
        log("execv не удался (%s), пробую запуск копии" % e)
        import subprocess
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=BASE)
        os._exit(0)

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

def main():
    global HTTPD
    port = int(CFG.get("port", 8100))
    no_browser = "--no-browser" in sys.argv
    if port_busy(port):
        # сервер уже работает (автозапуск) — просто открыть окно фронта
        if not no_browser:
            open_front_window()
        return
    seed_if_empty()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=code_watcher, daemon=True).start()
    log("Банкетный фронт запущен: http://localhost:%d" % port)
    if not (CFG.get("iiko_login") and CFG.get("iiko_password")):
        log("ВНИМАНИЕ: iiko_login/iiko_password не заполнены в config.json — синк цен и акты отключены.")
    if CFG.get("dry_run", True):
        log("Режим DRY-RUN: акты формируются в файлы logs/akt-*.xml, но в iiko НЕ отправляются.")
    for attempt in range(20):  # порт может освобождаться пару секунд после перезапуска
        try:
            HTTPD = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(1)
    if not no_browser:
        threading.Timer(1.5, open_front_window).start()
    HTTPD.serve_forever()

if __name__ == "__main__":
    main()
