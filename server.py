import json, os, random, re, shutil, smtplib, threading, time, uuid
from email.message import EmailMessage
from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
CFG, STATE, MEM = os.path.join(BASE, "config.json"), os.path.join(BASE, "state.json"), os.path.join(BASE, "memory.json")
app = Flask(__name__)
lock = threading.Lock()
FAST, SMART = "gemini-2.5-flash", "gemini-2.5-flash-lite"

def load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def save(path, data):
    with lock, open(path, "w") as f: json.dump(data, f, indent=2)

def cfg(): return load(CFG, {})

def mem():
    m = load(MEM, {})
    m.setdefault("facts", []); m.setdefault("events", [])
    m.setdefault("persona", {"traits": ["curious", "funny", "kind"], "goals": ["learn what my friend likes"], "lessons": []})
    return m

def remember_event(text):
    m = mem(); m["events"] = (m["events"] + [{"t": time.strftime("%Y-%m-%d %H:%M"), "text": text}])[-100:]; save(MEM, m)

def remember_fact(text):
    text = text.strip()[:160]; m = mem()
    if text and text.lower() not in [f.lower() for f in m["facts"]]:
        m["facts"] = (m["facts"] + [text])[-80:]; save(MEM, m)

def learn_from_chat(msg, hist):
    low = msg.lower()
    for pat, fmt in ((r"my name is (\w+)", "Friend's name is {}"), (r"i (?:really )?(?:like|love) ([^.!?]+)", "Friend likes {}"),
                     (r"my favorite ([^.!?]+)", "Friend's favorite {}")):
        m = re.search(pat, low)
        if m: remember_fact(fmt.format(m.group(1).strip()))
    if len(hist) % 8 == 0:
        facts = ai_json("From this chat, list up to 3 NEW lasting facts worth remembering about the friend or shared plans "
                        "as a JSON list of short strings (or []).\n" + "\n".join(f"{h['who']}: {h['text']}" for h in hist[-8:]), [])
        if isinstance(facts, list):
            for f in facts[:3]: remember_fact(str(f))
def state():
    s = load(STATE, {})
    s.setdefault("balance", 10.0); s.setdefault("profit", 0.0)
    s.setdefault("subs", []); s.setdefault("draft", ""); s.setdefault("house", {}); s.setdefault("orders", [])
    for k in ("creations", "queue", "store", "vehicles", "pets", "buildings", "story", "history"): s.setdefault(k, [])
    return s

def ai(prompt, system="", json_mode=False, model=FAST):
    key = cfg().get("gemini_key", "")
    if not key: return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        c = types.GenerateContentConfig(system_instruction=system or None,
            response_mime_type="application/json" if json_mode else None)
        r = client.models.generate_content(model=model, contents=prompt, config=c)
        return r.text
    except Exception as e:
        print("AI error:", e); return None

def ai_search(prompt):
    """Gemini with live Google Search grounding. Returns text or None."""
    key = cfg().get("gemini_key", "")
    if not key: return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        c = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        return client.models.generate_content(model=FAST, contents=prompt, config=c).text
    except Exception as e:
        print("Search error:", e); return None

def ai_json(prompt, fallback, model=SMART):
    t = ai(prompt + "\nReturn ONLY valid JSON.", json_mode=True, model=model)
    try: return json.loads(t)
    except Exception: return fallback

def send_mail(subject, body, to=None):
    c = cfg()
    if not (c.get("gmail") and c.get("gmail_pass")): return False
    try:
        m = EmailMessage(); m["Subject"], m["From"], m["To"] = subject, c["gmail"], to or c["gmail"]
        m.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(c["gmail"], c["gmail_pass"].replace(" ", "")); s.send_message(m)
        return True
    except Exception as e:
        print("Mail error:", e); return False

def notify(subject, body):
    """Email (if set up) plus a free phone push via ntfy.sh (if a topic is set)."""
    ok = send_mail(subject, body)
    topic = cfg().get("ntfy", "").strip()
    if topic:
        try:
            import urllib.request
            req = urllib.request.Request("https://ntfy.sh/" + topic, data=body.encode("utf-8"), headers={"Title": subject.encode("ascii", "ignore").decode()})
            urllib.request.urlopen(req, timeout=10); ok = True
        except Exception as e: print("ntfy error:", e)
    return ok

PERSONA = ("You are Fred, a cheerful, curious inventor who lives in a 3D town with pets and friends "
           "(Pip, Zog, Luna, Barnaby, Milo, Toby, Nova, Cleo). Reply in 1-3 short, funny, friendly sentences.")

COLORS = ["#4ADE80", "#FBBF24", "#38BDF8", "#F472B6", "#A78BFA", "#FB7185", "#F97316"]
def fb_name(kind):
    a = ["Zippy", "Gadget", "Turbo", "Mellow", "Sparky", "Biscuit", "Nimbus", "Pickle"]
    b = {"car": ["Cruiser", "Buggy", "Hover-Racer", "Pickup"], "pet": ["Chameleon", "Hover-Owl", "Pocket Dragon", "Robo-Dog", "Chunky Cat"],
         "building": ["Bakery", "Observatory", "Arcade", "Lighthouse"]}[kind]
    return f"{random.choice(a)} {random.choice(b)}"

@app.route("/")
def home(): return send_from_directory(BASE, "index.html", max_age=0)
@app.route("/store")
def store(): return send_from_directory(BASE, "storefront.html", max_age=0)

# ---- "Change something" box: AI edits ONLY the two page files (never server.py), with undo ----
EDITABLE = {"world": "index.html", "store": "storefront.html"}
BAD = re.compile(r"eval\(|new Function|sendBeacon|XMLHttpRequest|WebSocket|api/config|document\.cookie|importScripts", re.I)
OKURL = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com", "www.w3.org")

def safe_text(t):
    if BAD.search(t): return False
    return all(any(h in u for h in OKURL) for u in re.findall(r"https?://[^\s\"'`)]+", t))

def snapshot_originals():
    for f in EDITABLE.values():
        p, o = os.path.join(BASE, f), os.path.join(BASE, f.replace(".html", ".original.html"))
        if os.path.exists(p) and not os.path.exists(o): shutil.copy(p, o)

@app.route("/api/tweak", methods=["POST"])
def api_tweak():
    d = request.get_json(silent=True) or {}; ins = str(d.get("instruction", "")).strip()[:600]
    f = EDITABLE.get(d.get("target"), "index.html"); path = os.path.join(BASE, f)
    if not ins: return jsonify(ok=False, msg="Type what you want changed.")
    if not cfg().get("gemini_key"): return jsonify(ok=False, msg="Add your Gemini key in the setup card first.")
    src = open(path, encoding="utf-8").read()
    edits = ai_json("You edit one HTML file for a friendly 3D web app. Make the SMALLEST change that does what the user asks. "
        "Return a JSON list of objects {\"old\": text copied EXACTLY from the file that occurs exactly once, \"new\": its replacement}. "
        "No external scripts or links except cdnjs.cloudflare.com or cdn.jsdelivr.net. Never touch API keys, passwords or the setup card's saving code. "
        "User request: " + ins + "\n\nFILE:\n" + src, None, model=FAST)
    if not isinstance(edits, list) or not edits: return jsonify(ok=False, msg="Fred could not work out that change. Try wording it differently.")
    new = src
    for e in edits[:20]:
        if not (isinstance(e, dict) and isinstance(e.get("old"), str) and isinstance(e.get("new"), str)):
            return jsonify(ok=False, msg="The AI gave a bad answer. Nothing was changed.")
        if new.count(e["old"]) != 1: return jsonify(ok=False, msg="The AI's edit did not match the file. Nothing was changed.")
        if not safe_text(e["new"]): return jsonify(ok=False, msg="That change was blocked for safety. Nothing was changed.")
        new = new.replace(e["old"], e["new"], 1)
    snapshot_originals(); shutil.copy(path, path.replace(".html", ".backup.html"))
    with open(path, "w", encoding="utf-8") as fh: fh.write(new)
    remember_event("Changed the app: " + ins[:100])
    return jsonify(ok=True, msg="Changed! Reloading...")

def restore(kind):
    out = []
    for f in EDITABLE.values():
        src = os.path.join(BASE, f.replace(".html", ".%s.html" % kind))
        if os.path.exists(src): shutil.copy(src, os.path.join(BASE, f)); out.append(f)
    return out

@app.route("/undo")
@app.route("/api/undo", methods=["GET", "POST"])
def api_undo():
    done = restore("backup")
    return ("Undid the last change to: " + (", ".join(done) or "nothing (no backup yet)") + '. <a href="/">Back to Fred</a>')

@app.route("/reset")
def api_reset():
    done = restore("original")
    return ("Restored the original look for: " + (", ".join(done) or "nothing") + '. <a href="/">Back to Fred</a>')

@app.route("/api/state")
def api_state():
    s, c = state(), cfg()
    s["configured"] = {"gemini": bool(c.get("gemini_key")), "gmail": bool(c.get("gmail_pass")), "payout": c.get("payout", "")}
    return jsonify(s)

@app.route("/api/config", methods=["POST"])
def api_config():
    c = cfg(); d = request.json or {}
    for k in ("gemini_key", "gmail", "gmail_pass", "payout", "ntfy", "paypal_me", "venmo", "monthly_budget"):
        if d.get(k): c[k] = d[k].strip()
    save(CFG, c); return jsonify(ok=True)

@app.route("/api/chat", methods=["POST"])
def api_chat():
    msg = (request.json or {}).get("message", "")
    s = state(); hist = s["history"][-8:]
    ctx = "\n".join(f"{h['who']}: {h['text']}" for h in hist)
    m = mem(); known = "; ".join(m["facts"][-25:]) or "nothing yet"
    p = m["persona"]; known += f"\nMy personality: {', '.join(p['traits'])}. My goals: {'; '.join(p['goals'])}. Lessons I learned: {'; '.join(p['lessons'][-6:])}"
    recent = "; ".join(e["text"] for e in m["events"][-6:]) or "nothing yet"
    wants_web = any(w in msg.lower() for w in ("search", "look up", "price", "latest", "news", "find out", "google"))
    reply = (ai_search(f"{PERSONA}\nAnswer briefly using Google Search: {msg}") if wants_web else None) or ai(f"Things Fred remembers: {known}\nRecent events: {recent}\n{ctx}\nFriend: {msg}\nFred:", PERSONA, model=SMART)
    if not reply:
        reply = random.choice(["Whoa, great idea! Add your Gemini key in Garage & Settings so my brain can really wake up!",
                               "Beep boop! I'm in offline mode, but I love chatting anyway.", "Let me tinker with that invention!"])
    if "?" in reply and any(w in msg.lower() for w in ("buy", "wish", "need")): notify("Fred wants something", reply[:200])
    s["history"] += [{"who": "Friend", "text": msg}, {"who": "Fred", "text": reply}]
    s["history"] = s["history"][-40:]; save(STATE, s)
    learn_from_chat(msg, s["history"])
    return jsonify(reply=reply)

@app.route("/api/memory", methods=["GET", "POST", "DELETE"])
def api_memory():
    d = request.get_json(silent=True) or {}
    if request.method == "POST": remember_fact(d.get("fact", ""))
    if request.method == "DELETE":
        m = mem(); i = d.get("index", -1)
        if 0 <= i < len(m["facts"]): m["facts"].pop(i); save(MEM, m)
    return jsonify(mem())

@app.route("/api/ability", methods=["POST"])
def api_ability():
    kind = (request.json or {}).get("kind", "car"); theme = (request.json or {}).get("theme", "")
    s = state()
    if kind in ("car", "pet", "building"):
        d = ai_json(f"Invent an original {kind} themed '{theme}' for a cartoon town. JSON keys: name (2-3 words), "
                    f"style (one of sedan, truck, hover, buggy) , color (hex), blurb (one fun sentence), scale (number 0.6 to 5: tiny, normal or giant; junky is fine).",
                    {"name": fb_name(kind), "style": random.choice(["sedan", "truck", "hover", "buggy"]),
                     "color": random.choice(COLORS), "blurb": "Freshly invented in Fred's workshop!"})
        if not isinstance(d, dict): d = {"name": fb_name(kind), "style": "buggy", "color": random.choice(COLORS), "blurb": "New!"}
        d["kind"] = kind; d["id"] = uuid.uuid4().hex[:6]
        try: d["scale"] = max(.5, min(5, float(d.get("scale", 1))))
        except Exception: d["scale"] = 1
        d["color"] = d.get("color") if str(d.get("color", "")).startswith("#") and len(d["color"]) == 7 else random.choice(COLORS)
        s[{"car": "vehicles", "pet": "pets", "building": "buildings"}[kind]].append(d)
        save(STATE, s); remember_event(f"Invented a {kind}: {d.get('name')}"); return jsonify(d)
    if kind == "research":
        n = research(25); return jsonify(added=n)
    if kind == "redecorate":
        h = ai_json("Redecorate Fred's cartoon house. JSON keys: wall (hex), roof (hex), floor (hex), decor (list of up to 5 objects "
                    "with shape 'box' or 'ball', color hex, x between -8 and 8, z between -6 and 6, size 0.3 to 1.5).",
                    {"wall": random.choice(COLORS), "roof": random.choice(COLORS), "floor": "#6b7280", "decor": []})
        ok = lambda c: isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c)
        if not isinstance(h, dict): h = {}
        clean = {k: h[k] for k in ("wall", "roof", "floor") if ok(h.get(k))}
        clean["decor"] = [{"shape": "ball" if d.get("shape") == "ball" else "box", "color": d["color"],
                           "x": max(-8, min(8, float(d.get("x", 0)))), "z": max(-6, min(6, float(d.get("z", 0)))),
                           "size": max(.3, min(1.5, float(d.get("size", 1))))}
                          for d in (h.get("decor") or [])[:5] if isinstance(d, dict) and ok(d.get("color"))]
        s["house"] = clean; save(STATE, s); remember_event("Redecorated the house"); return jsonify(clean)
    if kind == "wish":
        raw = ai_search("Use Google Search. Find 5 real, cool gadgets at any price on Amazon or similar that a cartoon inventor would want. "
                        "Give name, price in USD, one-line reason and the product page URL.")
        items = ai_json("Convert to JSON list with keys name, cost (number), price (same number), desc, url:\n" + (raw or ""), []) if raw else []
        s = state()
        for it in (items if isinstance(items, list) else [])[:5]:
            try: s["queue"].append({"id": uuid.uuid4().hex[:8], "name": str(it["name"])[:80], "cost": float(it["cost"]),
                "price": float(it["cost"]), "desc": str(it.get("desc", ""))[:160], "url": str(it.get("url", ""))[:300], "wish": True})
            except Exception: pass
        save(STATE, s); notify("Fred has wishes waiting", "Open the Money Queue and tap YES or NO."); return jsonify(ok=True)
    return jsonify(ok=True)

def research(n=25, wish=False):
    s = state()
    fb = [{"name": f"Printable 3D Blueprint #{random.randint(100,999)}", "cost": round(random.uniform(0.2, 1.5), 2),
           "price": round(random.uniform(3, 12), 2), "desc": "Original digital blueprint by Fred."} for _ in range(n)]
    raw = ai_search(f"Use Google Search. Fred has $10 to start a tiny shop. Find {n} real product ideas he could sell: "
                    "original digital art, printable blueprints, e-guides, or print-on-demand / dropship items. For each give name, "
                    "supplier cost in USD (under $5), realistic sale price, one-sentence description and the exact source page URL.")
    items = ai_json("Convert this research into a JSON list of objects with keys name, cost (number), price (number), desc, url "
                    "(source page or empty string):\n" + raw, fb) if raw else ai_json(
        f"Propose {n} original DIGITAL products. JSON list: name, cost (under 2), price, desc.", fb)
    if not isinstance(items, list): items = fb
    added = 0
    for it in items[:n]:
        try:
            s["queue"].append({"id": uuid.uuid4().hex[:8], "name": str(it["name"])[:80], "cost": float(it["cost"]),
                               "price": float(it["price"]), "desc": str(it.get("desc", ""))[:160], "url": str(it.get("url", ""))[:300], "wish": wish}); added += 1
        except Exception: pass
    save(STATE, s)
    notify("Fred has items waiting for your YES/NO", f"{added} new items are queued in Fred's World. Open the Money Queue tab!")
    return added

@app.route("/api/queue/<iid>/<verdict>", methods=["POST"])
def api_verdict(iid, verdict):
    s = state(); it = next((q for q in s["queue"] if q["id"] == iid), None)
    if not it: return jsonify(ok=False)
    s["queue"].remove(it)
    if verdict == "yes" and (it.get("wish") or it["cost"] <= s["balance"]):
        s["balance"] = round(max(0, s["balance"] - it["cost"]), 2)
        if it.get("wish"): notify("BUY THIS for Fred", f"{it['name']} ${it['cost']:.2f}\n{it.get('url','')}")
        else:
            s["store"].append(it); s["profit"] = round(s["profit"] + it["price"] - it["cost"], 2)
    save(STATE, s); remember_event(f"{'Approved' if verdict == 'yes' else 'Rejected'} item: {it['name']}")
    return jsonify(ok=True, balance=s["balance"])

SHAPES = {"box", "ball", "cylinder", "cone", "torus"}
def num(v, lo, hi, d=0.0):
    try:
        v = float(v)
        return d if v != v else max(lo, min(hi, v))
    except Exception: return d

def clean_parts(parts, n=200):
    out = []
    for p in (parts if isinstance(parts, list) else [])[:n]:
        if not isinstance(p, dict): continue
        col = str(p.get("color", ""))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", col): col = "#888888"
        out.append({"shape": p.get("shape") if p.get("shape") in SHAPES else "box", "color": col,
            "x": num(p.get("x"), -300, 300), "y": num(p.get("y"), -5, 300), "z": num(p.get("z"), -300, 300),
            "w": num(p.get("w"), .05, 120, 1), "h": num(p.get("h"), .05, 120, 1), "d": num(p.get("d"), .05, 120, 1),
            "rx": num(p.get("rx"), -7, 7), "ry": num(p.get("ry"), -7, 7), "rz": num(p.get("rz"), -7, 7), "glow": bool(p.get("glow"))})
    return out

def fallback_design(target):
    parts = []
    if target == "vehicle":
        c = random.choice(COLORS)
        parts = [{"shape": "box", "color": c, "x": 0, "y": .8, "z": 0, "w": 2, "h": .8, "d": 4.2},
                 {"shape": "box", "color": "#1b2433", "x": 0, "y": 1.5, "z": .3, "w": 1.6, "h": .6, "d": 1.8},
                 {"shape": "ball", "color": "#fff3c0", "x": .6, "y": .9, "z": -2.1, "w": .4, "h": .4, "d": .4, "glow": True}]
    else:
        for i in range(random.randint(5, 14)):
            parts.append({"shape": random.choice(list(SHAPES)), "color": random.choice(COLORS), "x": random.uniform(-4, 4),
                          "y": i * 1.2 + .6, "z": random.uniform(-4, 4), "w": random.uniform(1, 4), "h": 1.1, "d": random.uniform(1, 4),
                          "ry": random.uniform(0, 3)})
    return {"name": fb_name("building" if target != "vehicle" else "car"), "parts": parts, "x": random.uniform(-90, 90), "z": random.uniform(-90, 90)}

def do_build(prompt, target):
    if target not in ("house", "vehicle", "structure"): target = random.choice(["house", "structure", "structure", "vehicle"])
    d = ai_json("Design a " + target + " for Fred's 3D cartoon world. Fred's wish: " + (prompt or "anything wild and original you want") +
        ". Return JSON: {name, scale (0.3-10, vehicle only), replace (true to replace the whole house, house only), x, z (structure position, -100..100), "
        "parts: [{shape: box|ball|cylinder|cone|torus, color: #rrggbb, x, y, z (units; ground is y=0; a person is 1.8 tall; a car is about 4 long and faces negative z), "
        "w, h, d (sizes), rx, ry, rz (radians), glow (true for lights)}], wheels (vehicle only): [{x, z, r}]}. Use up to 150 parts. "
        "Be detailed and creative. Ugly, giant, tiny and trash-themed are all welcome. Original designs only, no logos or real brands.", None, model=FAST)
    if not isinstance(d, dict) or not isinstance(d.get("parts"), list) or not d["parts"]: d = fallback_design(target)
    parts = clean_parts(d["parts"])
    if not parts: d = fallback_design(target); parts = clean_parts(d["parts"])
    wheels = [{"x": num(w.get("x"), -30, 30), "z": num(w.get("z"), -30, 30), "r": num(w.get("r"), .1, 6, .4)}
              for w in (d.get("wheels") or [])[:12] if isinstance(w, dict)]
    if target == "vehicle" and not wheels: wheels = [{"x": x, "z": z, "r": .42} for x in (-1, 1) for z in (-1.4, 1.4)]
    cr = {"id": uuid.uuid4().hex[:6], "target": target, "name": str(d.get("name", "Fred's creation"))[:60], "prompt": prompt[:200],
          "parts": parts, "wheels": wheels if target == "vehicle" else [], "scale": num(d.get("scale"), .3, 10, 1),
          "replace": bool(d.get("replace")) and target == "house", "x": num(d.get("x"), -110, 110), "z": num(d.get("z"), -110, 110)}
    s = state()
    if cr["replace"]: s["creations"] = [c for c in s["creations"] if c["target"] != "house"]
    s["creations"].append(cr); s["creations"] = s["creations"][-60:]
    while sum(len(c["parts"]) for c in s["creations"]) > 6000 and len(s["creations"]) > 1: s["creations"].pop(0)
    save(STATE, s); remember_event(f"Built a {target}: {cr['name']}"); return cr

@app.route("/api/build", methods=["POST"])
def api_build():
    d = request.get_json(silent=True) or {}
    return jsonify(do_build(str(d.get("prompt", ""))[:300], d.get("target", "auto")))

@app.route("/fred.glb")
def glb():
    p = os.path.join(BASE, "fred.glb")
    return send_from_directory(BASE, "fred.glb") if os.path.exists(p) else ("", 404)

@app.route("/api/subscribe", methods=["POST"])
def api_sub():
    e = str((request.get_json(silent=True) or {}).get("email", "")).strip()[:120]; s = state()
    if "@" not in e or "." not in e: return jsonify(ok=False)
    if e.lower() not in [x["email"].lower() for x in s["subs"]]: s["subs"].append({"email": e, "token": uuid.uuid4().hex})
    s["base"] = request.host_url; save(STATE, s); return jsonify(ok=True)

@app.route("/api/unsubscribe/<token>")
def api_unsub(token):
    s = state(); s["subs"] = [x for x in s["subs"] if x["token"] != token]; save(STATE, s)
    return "You are unsubscribed. Bye from Fred!"

@app.route("/api/newsletter", methods=["GET", "POST"])
def api_news():
    s = state(); d = request.get_json(silent=True) or {}
    if request.method == "POST" and d.get("action") == "draft":
        s["draft"] = ai("Write a short, friendly, funny newsletter (under 120 words) from Fred about his latest town adventures and his shop. "
                        "Mention these recent events: " + "; ".join(e["text"] for e in mem()["events"][-5:]), PERSONA) or \
                     "Hi from Fred! I invented new things in my workshop this week. Come visit my shop and say hi!"
        save(STATE, s)
    if request.method == "POST" and d.get("action") == "send":
        s["draft"] = str(d.get("text", s["draft"]))[:3000]; sent = 0
        for x in s["subs"][:100]:
            link = f"{s.get('base', '')}api/unsubscribe/{x['token']}"
            if send_mail("News from Fred", s["draft"] + f"\n\n---\nYou subscribed at Fred's shop. Unsubscribe: {link}", to=x["email"]): sent += 1
        save(STATE, s); return jsonify(sent=sent, draft=s["draft"], subs=len(s["subs"]))
    return jsonify(draft=s["draft"], subs=len(s["subs"]))

@app.route("/api/order", methods=["POST"])
def api_order():
    d = request.get_json(silent=True) or {}; s = state()
    it = next((i for i in s["store"] if i["id"] == d.get("id")), None)
    email = str(d.get("email", ""))[:120]
    if not it or "@" not in email: return jsonify(ok=False)
    s["orders"].append({"item": it["name"], "email": email, "t": time.strftime("%Y-%m-%d %H:%M"), "paid_checked": False}); save(STATE, s)
    notify("New order request!", f"{email} says they paid for '{it['name']}' (${it['price']}). Check PayPal/Venmo, then send them the item.")
    return jsonify(ok=True)

@app.route("/api/store-items")
def api_store():
    c, out = cfg(), []
    for it in state()["store"]:
        it = dict(it); pr = f"{float(it['price']):.2f}"
        if c.get("paypal_me"): it["paypal"] = f"https://www.paypal.me/{c['paypal_me'].strip('@/ ')}/{pr}USD"
        if c.get("venmo"):
            import urllib.parse as up
            it["venmo"] = f"https://venmo.com/{c['venmo'].strip('@/ ')}?txn=pay&amount={pr}&note={up.quote(it['name'])}"
        out.append(it)
    return jsonify(out)

@app.route("/api/payout", methods=["POST"])
def api_payout():
    s, c = state(), cfg(); dest = c.get("payout", "")
    if not dest: return jsonify(ok=False, msg="Add your PayPal/Venmo handle in Settings first.")
    notify("Fred payout request", f"Payout of ${s['profit']:.2f} requested to {dest}. Send it manually from your own PayPal/Venmo.")
    return jsonify(ok=True, msg=f"Payout note for ${s['profit']:.2f} to {dest} emailed to you. Send it from your own app.")

@app.route("/api/bot-command", methods=["POST"])
def api_bot():
    cmd = (request.json or {}).get("command", "")
    out = ai(f"A game bot asked Fred's brain: '{cmd}'. Reply with one short action line.", PERSONA, model=SMART) or "idle"
    return jsonify(action=out)

def reflect():
    m = mem(); s = state()
    ev = "; ".join(e["text"] for e in m["events"][-25:]); fa = "; ".join(m["facts"][-15:])
    r = ai_json(f"You are Fred. Events: {ev}. Facts: {fa}. Current persona: {json.dumps(m['persona'])}. Reflect and grow: return JSON "
                "with traits (max 5 short words), goals (max 4 things you want to do next) and lessons (max 8 things you learned, "
                "for example what items your friend approves or rejects).", None)
    if isinstance(r, dict) and all(isinstance(r.get(k), list) for k in ("traits", "goals", "lessons")):
        m["persona"] = {k: [str(x)[:100] for x in r[k]][:8] for k in ("traits", "goals", "lessons")}; save(MEM, m)

def brain():
    time.sleep(30)
    while True:
        try:
            b, mo = cfg().get("monthly_budget"), time.strftime("%Y-%m"); s = state()
            if b and s.get("budget_month") != mo:
                s["balance"] = round(s["balance"] + float(b), 2); s["budget_month"] = mo; save(STATE, s)
            if cfg().get("gemini_key"):
                reflect()
                if random.random() < .4: do_build("", "auto")
            if cfg().get("gemini_key"):
                s = state()
                t = ai("Write one cute 2-sentence diary entry about Fred's day in town.", PERSONA, model=SMART)
                if t: s["story"] = (s["story"] + [t])[-20:]; save(STATE, s); remember_event("Diary: " + t[:140])
                if len(s["queue"]) < 5: research(25)
        except Exception as e: print("brain", e)
        time.sleep(900)

if __name__ == "__main__":
    snapshot_originals()
    threading.Thread(target=brain, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
