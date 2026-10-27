"""Sets: a list of sprites a game needs, many takes per key, one pick per key, exported into the game.

The Batch tab renders one picture per row straight into the game. A set renders N takes per row, shows
them beside the sprite the game has today (NOW), and lets you pick one per key before anything is
written — soundmon-gui's Sets, for pictures. A set lives in <sets>/<id>/set.json:

    {"id", "name", "created", "updated",
     "theme": {"base": {...render params + form, as the form was when the theme was set...},
               "takes": 6, "game_folder": "~/git/game/art", "style_mode": "both", "size_mode": "row",
               "fit": {"on": false, "dir": "", "scale": 0}},
     "rows":  [{"key": "monsters/bat", "style", "size", "prompt", "group"}],      # frozen list
     "picks": {key: "<job>/<file>" | "now"},  "flags": {key: [...]},  "notes": {key: "..."},
     "exported": {key: {"at", "path", "from"}}}

Takes are NOT stored here: each render carries params.set = {id, key}, so a key's takes are the
gallery's jobs for it. Prompts are composed in the page (composedPrompt), exactly like Batch rows.
"""
import glob
import json
import os
import re
import shutil
import threading
import time

IMG_EXT = (".png", ".gif")
KEY_RE = re.compile(r"[\w.\-/ ]{1,160}")
LOCK = threading.Lock()


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def slug(text, n=48):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:n] or "sprite"


def parse_manifest(text):
    """The Batch tab's row formats (parseBatch in index.html), plus [Group] lines:

        path | style | size | prompt          path | prompt          a prompt on its own
        folder | name | style | size | prompt  path | style-or-size | prompt
    Canonical manifests list every medium ("ansi-art/…", "pixel-art/…"): only pixel-art rows are kept.
    """
    rows, seen, group = [], set(), ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            group = m.group(1).strip()
            continue
        c = [x.strip() for x in line.split("|")]
        style = size = ""
        if len(c) >= 5:
            path, style, size, prompt = f"{c[0]}/{c[1]}", c[2], c[3], " | ".join(c[4:])
        elif len(c) == 4:
            path, style, size, prompt = c
        elif len(c) == 3:
            path, prompt = c[0], c[2]
            if re.fullmatch(r"\d+(x\d+)?", c[1], re.I):
                size = c[1]
            else:
                style = c[1]
        elif len(c) == 2:
            path, prompt = c
        else:
            prompt = c[0]
            path = slug(prompt)
        mm = re.fullmatch(r"([\w-]+-art)/(.*)", path)
        if mm and mm.group(1) != "pixel-art":
            continue
        path = re.sub(r"\.(png|gif|ans)$", "", mm.group(2) if mm else path, flags=re.I).strip("/")
        if not path or not KEY_RE.fullmatch(path) or ".." in path.split("/"):
            raise ValueError(f"{path!r} can't be a file path — letters, digits, spaces, . - _ /")
        if path in seen:
            raise ValueError(f"{path!r} appears twice")
        if not prompt:
            raise ValueError(f"{path}: no prompt")
        seen.add(path)
        g = group or (path.split("/")[0] if "/" in path else "")
        rows.append({"key": path, "style": re.sub(r"\s+", "", style), "size": size, "prompt": prompt, "group": g})
    if not rows:
        raise ValueError("no sprites in that list — one per line: `path | style | size | prompt`, `path | prompt`, or a prompt")
    return rows


class Sets:
    def __init__(self, root, gallery, home_path):
        self.root, self.gallery, self.home_path = root, gallery, home_path
        os.makedirs(root, exist_ok=True)

    # ---------------- storage ----------------
    def path(self, sid):
        sid = re.sub(r"[^\w-]", "", str(sid or ""))[:64]
        if not sid:
            raise ValueError("no set given")
        return os.path.join(self.root, sid, "set.json")

    def load(self, sid):
        s = _read(self.path(sid), None)
        if not s:
            raise ValueError("that set doesn't exist")
        return s

    def save(self, s):
        s["updated"] = time.time()
        _write(self.path(s["id"]), s)
        return s

    def list(self):
        out = []
        for f in glob.glob(os.path.join(self.root, "*", "set.json")):
            s = _read(f, None)
            if s:
                out.append({"id": s["id"], "name": s["name"], "keys": len(s["rows"]), "updated": s.get("updated")})
        return sorted(out, key=lambda x: -(x["updated"] or 0))

    def _theme(self, t):
        base = t.get("base")
        if not isinstance(base, dict) or not isinstance(base.get("form"), dict):
            raise ValueError("the set needs a theme: press “Use the form's current settings”")
        if len(json.dumps(base)) > 400000:
            raise ValueError("that theme is too big")
        base = {k: v for k, v in base.items() if k not in ("set", "group", "label", "export", "fit", "parent", "gen")}
        takes = int(t.get("takes") or 6)
        if not 1 <= takes <= 32:
            raise ValueError("takes per sprite must be 1..32")
        folder = str(t.get("game_folder") or "").strip()
        if folder:
            self.home_path(folder, must_exist=False)
        fit = t.get("fit") or {}
        fdir = str(fit.get("dir") or "").strip()
        if fdir:
            self.home_path(fdir, must_exist=False)
        if fit.get("on") and not (fdir or folder):
            raise ValueError("fitting into the old sprites needs a game folder (or an old sprites folder)")
        sm, zm = t.get("style_mode") or "both", t.get("size_mode") or "row"
        if sm not in ("row", "both", "form") or zm not in ("row", "form"):
            raise ValueError("bad style / size mode")
        return {"base": base, "takes": takes, "game_folder": folder, "style_mode": sm, "size_mode": zm,
                "fit": {"on": bool(fit.get("on")), "dir": fdir, "scale": int(fit.get("scale") or 0)}}

    def create(self, body):
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            raise ValueError("give the set a name")
        rows = parse_manifest(body.get("manifest"))
        theme = self._theme(body.get("theme") or {})
        sid = time.strftime("%Y%m%d-") + slug(name, 40)
        with LOCK:
            if os.path.exists(self.path(sid)):
                sid += time.strftime("-%H%M%S")
            return self.save({"id": sid, "name": name, "created": time.time(), "theme": theme, "rows": rows,
                              "picks": {}, "flags": {}, "notes": {}, "exported": {}})

    def update(self, body):
        with LOCK:
            s = self.load(body.get("id"))
            if "name" in body:
                s["name"] = str(body["name"] or s["name"]).strip()[:80] or s["name"]
            if "theme" in body:
                s["theme"] = self._theme(body["theme"])
            if "manifest" in body:                         # re-import: keep marks for keys that remain
                rows = parse_manifest(body["manifest"])
                keys = {r["key"] for r in rows}
                for d in ("picks", "flags", "notes"):
                    s[d] = {k: v for k, v in s[d].items() if k in keys}
                s["rows"] = rows
            if isinstance(body.get("row"), dict):
                r = body["row"]
                row = next((x for x in s["rows"] if x["key"] == r.get("key")), None)
                if not row:
                    raise ValueError("no such sprite in this set")
                if "prompt" in r:
                    p = str(r["prompt"] or "").strip()
                    if not p or "\n" in p or len(p) > 2000:
                        raise ValueError("a prompt is one line of up to 2000 characters")
                    row["prompt"] = p
            return self.save(s)

    def mark(self, body):
        with LOCK:
            s = self.load(body.get("id"))
            key = body.get("key")
            if key not in {r["key"] for r in s["rows"]}:
                raise ValueError("no such sprite in this set")
            take, act = body.get("take"), body.get("action")
            if take not in (None, "now") and not any(t["take"] == take for t in self.takes(s["id"]).get(key, [])):
                raise ValueError("that take isn't one of this sprite's")
            if act == "pick":
                if take is None:
                    s["picks"].pop(key, None)
                else:
                    s["picks"][key] = take
            elif act in ("flag", "unflag"):
                fl = set(s["flags"].get(key) or [])
                fl.add(take) if act == "flag" else fl.discard(take)
                s["flags"][key] = sorted(fl)
                if not s["flags"][key]:
                    del s["flags"][key]
            elif act == "note":
                s["notes"][key] = str(body.get("note") or "")[:500]
                if not s["notes"][key]:
                    del s["notes"][key]
            else:
                raise ValueError(f"unknown action {act!r}")
            return self.save(s)

    # ---------------- takes (from the gallery) ----------------
    def takes(self, sid):
        """{key: [{"take": "<job>/<file>", "job", "file", "seed", "created", "command"}]} oldest first."""
        out = {}
        for jf in sorted(glob.glob(os.path.join(self.gallery, "*", "job.json"))):
            job = _read(jf, None)
            ref = ((job or {}).get("params") or {}).get("set") or {}
            if ref.get("id") != sid or not job.get("outputs"):
                continue
            for o in job["outputs"]:
                if not o.get("file", "").lower().endswith(IMG_EXT):
                    continue
                out.setdefault(ref.get("key"), []).append({
                    "take": f"{job['dir']}/{o['file']}", "job": job["dir"], "file": o["file"], "seed": o.get("seed"),
                    "created": job.get("created"), "command": job.get("command")})
        return out

    def pending(self, sid, jobs):
        out = {}
        for j in jobs:
            ref = (j.get("params") or {}).get("set") or {}
            if ref.get("id") == sid and j["status"] in ("queued", "running"):
                out[ref.get("key")] = out.get(ref.get("key"), 0) + max(1, int(j.get("total") or 1) - len(j.get("outputs") or []))
        return out

    def take_path(self, take):
        full = os.path.realpath(os.path.join(self.gallery, str(take or "")))
        if not full.startswith(os.path.realpath(self.gallery) + os.sep) or not os.path.isfile(full):
            raise ValueError("that take's file is gone")
        return full

    # ---------------- what the game has today ----------------
    def game_dir(self, s):
        f = s["theme"].get("game_folder")
        return self.home_path(f, must_exist=False) if f else None

    def now_files(self, s):
        """{key: [files in the game folder with that key's stem]} — .png, .gif"""
        d = self.game_dir(s)
        if not d or not os.path.isdir(d):
            return {}
        out = {}
        for r in s["rows"]:
            hits = [r["key"] + e for e in IMG_EXT if os.path.isfile(os.path.join(d, r["key"] + e))]
            if hits:
                out[r["key"]] = hits
        return out

    def now_path(self, s, key):
        files = self.now_files(s).get(key)
        if not files:
            raise ValueError("the game has no picture for that sprite yet")
        d = os.path.realpath(self.game_dir(s))
        full = os.path.realpath(os.path.join(d, files[0]))
        if not full.startswith(d + os.sep):
            raise ValueError("outside the game folder")
        return full

    # ---------------- export ----------------
    def export_plan(self, s):
        d = self.game_dir(s)
        if not d:
            raise ValueError("this set has no game folder — set one with ✎ Edit set")
        now, plan = self.now_files(s), []
        for r in s["rows"]:
            k, pick = r["key"], s["picks"].get(r["key"])
            if not pick or pick == "now":
                continue
            src = self.take_path(pick)
            ext = os.path.splitext(src)[1].lower()
            rel = k + ext
            dest = os.path.realpath(os.path.join(d, rel))
            if not dest.startswith(os.path.realpath(d) + os.sep):
                raise ValueError(f"{k!r} points outside the game folder")
            done = s["exported"].get(k)
            plan.append({"key": k, "from": pick, "to": rel, "replaces": os.path.exists(dest),
                         "other_formats": [f for f in now.get(k, []) if f != rel],
                         "unchanged": bool(done and done.get("from") == pick and done.get("path") == rel and os.path.exists(dest))})
        return plan

    def export(self, s, only_changed=True):
        d = self.game_dir(s)
        plan = self.export_plan(s)
        backup = os.path.join(d, ".pixelmon-backup", time.strftime("%Y-%m-%d_%H%M%S"))
        done, errors = [], []
        for item in plan:
            if only_changed and item["unchanged"]:
                continue
            src, dest = self.take_path(item["from"]), os.path.join(d, item["to"])
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.exists(dest):                     # nothing is replaced without a copy kept
                    b = os.path.join(backup, item["to"])
                    os.makedirs(os.path.dirname(b), exist_ok=True)
                    shutil.copy2(dest, b)
                shutil.copy2(src, dest)
                s["exported"][item["key"]] = {"at": time.time(), "path": item["to"], "from": item["from"]}
                done.append(item)
            except OSError as e:
                errors.append({"key": item["key"], "error": str(e)[:300]})
        with LOCK:
            fresh = self.load(s["id"])
            fresh["exported"].update(s["exported"])
            self.save(fresh)
        return {"exported": done, "errors": errors, "backup": backup if os.path.isdir(backup) else None,
                "skipped": sum(1 for i in plan if i["unchanged"]) if only_changed else 0}
