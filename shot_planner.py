#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gimmick Shot Planner — a tiny local shot planner for AI film / TVC production.

  python3 shot_planner.py            เปิดเครื่องมือ (เซิร์ฟเวอร์ + เด้งเบราว์เซอร์)  /  start the planner
  python3 shot_planner.py daemon     เหมือนกันแต่ไม่เด้งเบราว์เซอร์               /  no browser (for LaunchAgent)
  python3 shot_planner.py scan       อัปเดต planner_data.js สำหรับโหมดอ่านอย่างเดียว /  refresh offline data
  python3 shot_planner.py build      สร้างไฟล์ prompt ให้ทุกช็อตที่ยังไม่มี         /  write missing prompts
  python3 shot_planner.py build -f   สร้างใหม่ทับของเดิมทั้งหมด                    /  rebuild every prompt

ทุกอย่างที่เป็นของโปรเจกต์อยู่ใน project.json ไฟล์เดียว — ตัวโค้ดไม่มีอะไรผูกกับหนังเรื่องไหน
Everything project-specific lives in project.json. The code knows nothing about your film.
"""
import base64
import datetime
import http.server
import json
import os
import re
import shutil
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(HERE, "project.json")
OUT_JS = os.path.join(HERE, "planner_data.js")
IMG_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.I)

# ค่าตั้งต้น ถ้า project.json ไม่ได้ใส่มา / defaults for anything project.json leaves out
DEFAULTS = {
    "name": "Shot Planner",
    "lang": "th",
    "port": 8765,
    "shots_dir": "shots",
    "sheets_dir": "sheets",
    "script_file": "script.txt",
    "channels": [{"id": "main", "label": "Main", "prefix": "main_", "target": 10, "style": "full"}],
    "product": {"label": "", "block": "", "short": ""},
    "prompt": {"head": "", "tail": "", "tail_short": ""},
    "options": {"sizes": [], "angles": [], "moves": [], "locs": [], "states": []},
    "cast_presets": [],
    "takes": [],
    "shots": [],
}
SHOT_KEYS = ("folder", "sec", "size", "angle", "move", "loc", "state", "cast", "desc", "action", "picks")

# โครง prompt สำเร็จรูป — เขียน "template" ของตัวเองใน channel ก็ได้
# built-in prompt shapes; a channel may override with its own "template"
TEMPLATES = {
    "full": "{head}\n\n{camera_line} {action_line}\n\n{product}\n\n{state}\n\n"
            "{location_line} {people_line}\n\n{tail}",
    "tags": "{size} {angle}, {product_short}, {state_short}, {action}, {loc_short}, {tail_short}",
}


class Blank(dict):
    """placeholder ที่ไม่รู้จัก = ว่าง แทนที่จะพัง / unknown placeholders render empty"""

    def __missing__(self, key):
        return ""


# ---------------------------------------------------------------- config
def load_conf():
    conf = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        return conf
    except ValueError as e:
        print(f"!! project.json ผิดรูปแบบ / invalid JSON: {e}")
        return conf
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(conf.get(k), dict):
            conf[k].update(v)
        else:
            conf[k] = v
    conf["shots"] = [normalise(s) for s in conf.get("shots", [])]
    return conf


def normalise(shot):
    s = {"folder": "", "sec": 0, "size": "", "angle": "", "move": "", "loc": "",
         "state": "", "cast": "", "desc": "", "action": "", "picks": []}
    s.update({k: v for k, v in shot.items() if k in SHOT_KEYS})
    return s


def save_conf(conf):
    """เขียนแบบ temp+rename กันไฟล์พังถ้าดับกลางคัน / atomic write"""
    out = {k: v for k, v in conf.items() if k != "_runtime"}
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONF_PATH)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def shots_dir(conf):
    return os.path.join(HERE, conf["shots_dir"])


# ---------------------------------------------------------------- prompts
def opt(conf, group, oid):
    for o in conf["options"].get(group, []):
        if o.get("id") == oid:
            return o
    return {"id": oid or "", "label": oid or "", "en": ""}


def short_of(o):
    return o.get("short") or (o.get("en") or "").split(" — ")[0]


def has_product(conf, shot):
    block = (conf.get("product") or {}).get("block") or ""
    return bool(block) and shot.get("state") not in (None, "", "na")


def tidy(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tidy_tags(text):
    seen = [t.strip() for t in tidy(text).replace("\n", " ").split(",")]
    return ", ".join(t for t in seen if t)


def build_prompt(conf, shot, ch):
    z, a = opt(conf, "sizes", shot.get("size")), opt(conf, "angles", shot.get("angle"))
    m, l = opt(conf, "moves", shot.get("move")), opt(conf, "locs", shot.get("loc"))
    st = opt(conf, "states", shot.get("state"))
    prod = conf.get("product") or {}
    on = has_product(conf, shot)
    action = (shot.get("action") or shot.get("desc") or "").strip()
    cast = (shot.get("cast") or "").strip()
    p = conf.get("prompt") or {}
    parts = {
        "head": p.get("head", ""), "tail": p.get("tail", ""), "tail_short": p.get("tail_short", ""),
        "product": prod.get("block", "") if on else "",
        "product_short": prod.get("short", "") if on else "",
        "state": st.get("en", "") if on else "", "state_short": short_of(st) if on else "",
        "size": z.get("en", ""), "angle": a.get("en", ""), "move": m.get("en", ""),
        "loc": l.get("en", ""), "loc_short": short_of(l),
        "cast": cast, "action": action, "desc": shot.get("desc", ""),
        "folder": shot.get("folder", ""), "sec": shot.get("sec", 0),
    }
    cam = ", ".join(x for x in (parts["size"], parts["angle"], parts["move"]) if x)
    parts["camera_line"] = f"CAMERA: {cam}." if cam else ""
    parts["action_line"] = f"ACTION: {action}" if action else ""
    parts["location_line"] = f"LOCATION: {parts['loc']}." if parts["loc"] else ""
    parts["people_line"] = f"PEOPLE: {cast}." if cast else ""
    style = ch.get("style", "full")
    tpl = ch.get("template") or TEMPLATES.get(style, TEMPLATES["full"])
    text = tpl.format_map(Blank(parts))
    return tidy_tags(text) if style == "tags" and not ch.get("template") else tidy(text)


def build_all(conf, shot):
    return {ch["id"]: build_prompt(conf, shot, ch) for ch in conf["channels"]}


# ---------------------------------------------------------------- scan
def natural_key(name):
    m = re.search(r"(\d+)", name)
    return (int(m.group(1)) if m else 10 ** 6, name.lower())


def scan(conf=None, write_js=True):
    conf = conf or load_conf()
    root = shots_dir(conf)
    os.makedirs(root, exist_ok=True)
    on_disk = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")]
    listed = [s["folder"] for s in conf["shots"]]
    order = listed + sorted([d for d in on_disk if d not in listed], key=natural_key)

    by_folder = {s["folder"]: s for s in conf["shots"]}
    shots = []
    for folder in order:
        fpath = os.path.join(root, folder)
        base = dict(by_folder.get(folder) or normalise({"folder": folder}))
        refdir = os.path.join(fpath, "ref")
        imgs = sorted([f for f in os.listdir(fpath) if IMG_RE.search(f)]) if os.path.isdir(fpath) else []
        base["refs"] = sorted([r for r in os.listdir(refdir) if IMG_RE.search(r)]) if os.path.isdir(refdir) else []
        base["renders"] = {ch["id"]: [f for f in imgs if f.lower().startswith(ch["prefix"].lower())]
                           for ch in conf["channels"]}
        base["prompts"] = {ch["id"]: read(os.path.join(fpath, "prompt_%s.txt" % ch["id"])).strip()
                           for ch in conf["channels"]}
        base["picks"] = [p for p in (base.get("picks") or []) if p in imgs]
        base["exists"] = os.path.isdir(fpath)
        shots.append(base)

    sheetdir = os.path.join(HERE, conf["sheets_dir"])
    sheets = ([conf["sheets_dir"] + "/" + f for f in sorted(os.listdir(sheetdir)) if IMG_RE.search(f)]
              if os.path.isdir(sheetdir) else [])

    data = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "name": conf["name"], "lang": conf["lang"], "port": conf["port"],
        "root": conf["shots_dir"], "sheets": sheets,
        "takes": conf["takes"], "script": read(os.path.join(HERE, conf["script_file"])),
        "options": conf["options"], "castPresets": conf["cast_presets"],
        "channels": conf["channels"], "product": conf.get("product") or {},
        "prompt": conf.get("prompt") or {},          # หน้าเว็บใช้ตอนทำงานแบบไม่มีเซิร์ฟเวอร์
        "shots": shots,
    }
    if write_js:
        with open(OUT_JS, "w", encoding="utf-8") as f:
            f.write("window.PLANNER = ")
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write(";\n")
    return data


# ---------------------------------------------------------------- writing
def write_prompts(conf, fpath, shot):
    for ch in conf["channels"]:
        new = (shot.get("prompts") or {}).get(ch["id"], "")
        new = (new or "").strip()
        target = os.path.join(fpath, "prompt_%s.txt" % ch["id"])
        if new and new != read(target).strip():
            with open(target, "w", encoding="utf-8") as f:
                f.write(new + "\n")


def store_shot(conf, shot):
    row = normalise(shot)
    for i, s in enumerate(conf["shots"]):
        if s["folder"] == row["folder"]:
            merged = dict(s)
            merged.update({k: v for k, v in row.items() if k in shot})
            conf["shots"][i] = merged
            break
    else:
        conf["shots"].append(row)
    save_conf(conf)


def safe_folder(name):
    return re.sub(r"[^\w\-]+", "_", os.path.basename(name or "")).strip("_")


def save_attachments(conf, fpath, shot):
    """รูปที่ลาก/อัปโหลดมา (base64) + ชีทที่เลือกจากโฟลเดอร์ sheets/"""
    refdir = os.path.join(fpath, "ref")
    os.makedirs(refdir, exist_ok=True)
    copied, missing = [], []
    for f in shot.get("files", []):
        name = os.path.basename(f.get("name", "")) or "upload.png"
        try:
            with open(os.path.join(refdir, name), "wb") as out:
                out.write(base64.b64decode(f.get("b64", "")))
            copied.append(name)
        except (ValueError, OSError):
            missing.append(name)
    sheetdir = os.path.join(HERE, conf["sheets_dir"])
    for rel in shot.get("sheets", []):
        src = os.path.join(sheetdir, os.path.basename(rel))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(refdir, os.path.basename(rel)))
            copied.append(os.path.basename(rel))
        else:
            missing.append(os.path.basename(rel))
    return copied, missing


# ---------------------------------------------------------------- api
def api_save(conf, shot):
    folder = safe_folder(shot.get("folder"))
    fpath = os.path.join(shots_dir(conf), folder)
    if not folder or not os.path.isdir(fpath):
        return {"ok": False, "error": "unknown folder"}
    write_prompts(conf, fpath, shot)
    store_shot(conf, shot)
    return {"ok": True}


def api_newshot(conf, shot):
    folder = safe_folder(shot.get("folder"))
    if not folder:
        return {"ok": False, "error": "no folder name"}
    fpath = os.path.join(shots_dir(conf), folder)
    if os.path.isdir(fpath) and not shot.get("overwrite"):
        return {"ok": False, "exists": True, "error": "shot %s already exists" % folder}
    shot["folder"] = folder
    copied, missing = save_attachments(conf, fpath, shot)
    write_prompts(conf, fpath, shot)
    store_shot(conf, shot)
    return {"ok": True, "folder": folder, "copied": copied, "missing": missing}


def api_upload(conf, data):
    folder = safe_folder(data.get("folder"))
    fpath = os.path.join(shots_dir(conf), folder)
    if not folder or not os.path.isdir(fpath):
        return {"ok": False, "error": "unknown folder"}
    copied, missing = save_attachments(conf, fpath, data)
    refdir = os.path.join(fpath, "ref")
    return {"ok": True, "copied": copied, "missing": missing,
            "refs": sorted([r for r in os.listdir(refdir) if IMG_RE.search(r)])}


def api_delete(conf, data):
    """เอาช็อตออกจากลิสต์ + เก็บโฟลเดอร์ให้เรียบร้อย ไม่งั้นสแกนรอบหน้ามันโผล่กลับมา
    Drop a shot. An empty folder is removed; one holding images is moved to _deleted/
    so it stays recoverable and stops reappearing on the next scan."""
    folder = safe_folder(data.get("folder"))
    if not folder:
        return {"ok": False, "error": "no folder name"}
    conf["shots"] = [s for s in conf["shots"] if s["folder"] != folder]
    save_conf(conf)

    fpath, moved = os.path.join(shots_dir(conf), folder), False
    if os.path.isdir(fpath):
        keep = [f for f in os.listdir(fpath) if IMG_RE.search(f)]
        refdir = os.path.join(fpath, "ref")
        if os.path.isdir(refdir):
            keep += [f for f in os.listdir(refdir) if IMG_RE.search(f)]
        if keep:
            trash = os.path.join(HERE, "_deleted")
            os.makedirs(trash, exist_ok=True)
            dest = os.path.join(trash, folder)
            for n in range(2, 99):                       # ชื่อซ้ำก็ต่อเลขไป
                if not os.path.exists(dest):
                    break
                dest = os.path.join(trash, "%s_%d" % (folder, n))
            shutil.move(fpath, dest)
            moved = True
        else:
            shutil.rmtree(fpath, ignore_errors=True)
    return {"ok": True, "folder": folder, "moved": moved}


# ---------------------------------------------------------------- server
class Handler(http.server.SimpleHTTPRequestHandler):
    conf = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path in ("", "/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/planner.html")
            self.end_headers()
        elif self.path.startswith("/api/data"):
            Handler.conf = load_conf()          # อ่าน project.json ใหม่ทุกครั้ง แก้ด้วยมือแล้วกด 🔄 เห็นเลย
            self.send_json(scan(Handler.conf, write_js=False))
        else:
            super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.send_error(400)
            return
        conf = Handler.conf or load_conf()
        routes = {
            "/api/save": lambda: api_save(conf, data),
            "/api/newshot": lambda: api_newshot(conf, data),
            "/api/upload": lambda: api_upload(conf, data),
            "/api/delete": lambda: api_delete(conf, data),
            "/api/prompts": lambda: {"ok": True, "prompts": build_all(conf, normalise(data))},
        }
        if self.path in routes:
            self.send_json(routes[self.path]())
        else:
            self.send_error(404)


def serve(open_browser=True):
    conf = load_conf()
    Handler.conf = conf
    url = "http://127.0.0.1:%d/" % conf["port"]
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", conf["port"]), Handler)
    except OSError:
        print("Planner เปิดอยู่แล้ว / already running -> " + url)
        if open_browser:
            webbrowser.open(url)
        return
    scan(conf)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    line = "  %s  ·  %s  " % (conf["name"], url)
    print("\n┌" + "─" * len(line) + "┐")
    print("│" + line + "│")
    print("└" + "─" * len(line) + "┘")
    print("  แก้ในหน้าเว็บ = บันทึกลง project.json ให้เอง · ปิดหน้าต่างนี้เมื่อเลิกใช้")
    print("  Edits in the browser are saved to project.json · close this window to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")


def cli_build(force=False):
    conf = load_conf()
    root = shots_dir(conf)
    n = 0
    for shot in scan(conf, write_js=False)["shots"]:
        fpath = os.path.join(root, shot["folder"])
        os.makedirs(fpath, exist_ok=True)
        built = build_all(conf, shot)
        keep = {k: (built[k] if (force or not v) else v) for k, v in shot["prompts"].items()}
        write_prompts(conf, fpath, {"prompts": keep})
        n += sum(1 for k, v in keep.items() if v != shot["prompts"][k])
    scan(conf)
    print("เขียน prompt ไป %d ไฟล์ / wrote %d prompt files" % (n, n))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "scan":
        scan()
        print("scan เสร็จ -> planner_data.js")
    elif cmd == "build":
        cli_build(force=any(a in ("-f", "--force") for a in sys.argv[2:]))
    elif cmd == "daemon":
        serve(open_browser=False)
    else:
        serve()
