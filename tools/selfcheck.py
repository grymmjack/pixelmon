#!/usr/bin/env python3
"""Assert the things that broke silently. Run after touching pixelmon.py, animate.py or bin/pixelmon.

WHY THIS EXISTS

The costly bugs in pixelmon's history didn't crash; they quietly did the wrong thing:

    an identical re-run answered from ComfyUI's cache with "success" and no images
    a failed node waited out the full 10-minute timeout (animate.py did, until it used pixelmon's wait)
    a farm box that fails every prompt (a POISONED server) freeing up first and eating the queue
    a requeued farm job that no idle box ever picked up, and a run that still exited 0
    Ctrl-C leaving the rest of a 128-job batch rendering on the GPU for nobody
    the wrapper starting a local ComfyUI for --server http://127.0.0.1:8189, --server=rtx or --version
    PIL re-saves dropping the recipe ComfyUI embeds in every PNG
    an outdated PixelArtPalette on a farm box silently IGNORING inputs it doesn't know

An assertion re-checks each of these every time, offline (no ComfyUI, no GPU): graphs are built with
--print-graph, servers are tiny in-process stubs, the farm runs against fake boxes.

    tools/selfcheck.py            # all checks
    tools/selfcheck.py -v         # show each result
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.pop("PIXELMON_SERVER", None)
os.environ["NO_COLOR"] = "1"
import pixelmon as P      # noqa: E402
import manifest           # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    return ok


def graph(*args):
    """The API graph pixelmon would send for these flags (built offline with --print-graph)."""
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ["pixelmon", *args, "--print-graph"]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            P.main()
    finally:
        sys.argv = old
    return json.loads(out.getvalue())


def by_class(g, cls):
    return [k for k, v in g.items() if v["class_type"] == cls]


class Stub:
    """A tiny HTTP server answering from a dict of path -> JSON (or a callable(path, body) -> (code, JSON))."""
    def __init__(self, routes):
        self.routes, self.posts = routes, []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, body=None):
                path = self.path.split("?")[0]
                r = stub.routes.get(path)
                if r is None:
                    r = next((v for k, v in stub.routes.items() if k.endswith("*") and path.startswith(k[:-1])), None)
                code, obj = (r(path, body) if callable(r) else (200, r)) if r is not None else (404, {})
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.posts.append((self.path, json.loads(body or b"{}")))
                self._answer(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def node_inputs():
    """Every input name PixelArtPalette declares, read from the node's source (it needs torch to import)."""
    src = open(os.path.join(HERE, "custom_nodes", "pixelart_palette", "nodes.py"), encoding="utf-8").read()
    body = src[src.index("class PixelArtPalette"):]
    body = body[body.index("def INPUT_TYPES"):body.index("RETURN_TYPES")]
    return set(re.findall(r'"(\w+)":\s*\(', body))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="pixelmon-selfcheck-")
    try:
        from PIL import Image
    except ImportError:
        Image = None

    # --- the wait / error classifier ------------------------------------------------------------------
    err = {"status": {"status_str": "error", "messages": [["execution_error", {
        "node_type": "LoraLoader", "node_id": "15", "exception_type": "FileNotFoundError",
        "exception_message": "lora missing"}]]}, "outputs": {}}
    stopped = {"status": {"status_str": "error", "messages": [["execution_interrupted", {}]]}, "outputs": {}}
    cached = {"status": {"status_str": "success", "completed": True}, "outputs": {}}
    running = {"status": {"status_str": "success", "completed": False}, "outputs": {}}
    oom = "KSampler (node 3): torch.OutOfMemoryError: CUDA out of memory"
    check("a failed node is an error, naming the node; an interrupt says so",
          "LoraLoader" in (P._render_error(err) or "") and "interrupted" in (P._render_error(stopped) or "")
          and P._render_error(cached) is None and P._render_error(running) is None, P._render_error(err))
    check("an identical cached re-run (success, no images) is cached-empty, not a failure",
          P._cached_empty(cached) and not P._cached_empty(running) and not P._cached_empty(err))
    check("OOM and a loader IndexError are transient; a missing model isn't",
          P._transient(oom) and P._transient("CheckpointLoaderSimple (node 4): IndexError: list index out of range")
          and not P._transient(P._render_error(err)))

    # wait() against a stub server: fails fast, re-saves a cached re-run, forgets finished prompts
    hist = {"e1": err, "c1": cached, "ok": {"status": {"status_str": "success", "completed": True},
                                          "outputs": {"11": {"images": [{"filename": "x.png"}]}}}}
    st = Stub({"/history/*": lambda path, body: (200, {path.rsplit("/", 1)[1]: hist[path.rsplit("/", 1)[1]]}
                                                    if path.rsplit("/", 1)[1] in hist else {}),
               "/queue": {"queue_running": [], "queue_pending": []}})
    t0 = time.time()
    try:
        P.wait("e1", st.url)
        failed_fast = False
    except P.RenderFailed as e:
        failed_fast = "LoraLoader" in str(e) and time.time() - t0 < 5
    check("wait() raises RenderFailed at once on a node error (not after the 600 s timeout)", failed_fast,
          f"{time.time() - t0:.1f}s")
    asked = []
    P.SENT[st.url] = {"c1"}
    with contextlib.redirect_stdout(io.StringIO()):
        outs = P.wait("c1", st.url, resubmit=lambda fresh: asked.append(fresh) or "ok")
    check("wait() re-saves a cached re-run once (fresh=True) and returns the new prompt's images",
          asked == [True] and "11" in outs and not P.SENT[st.url], f"resubmits {asked}, SENT {P.SENT[st.url]}")
    try:
        P.wait("c1", st.url)
        nores = False
    except P.RenderFailed:
        nores = True
    check("a cached re-run with no way to resend it is a failed job, not a hang", nores)
    g0 = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pixelmon/x_s5"}}}
    fc = P.fresh_copy(g0)
    check("fresh_copy saves into a new subfolder, same file name, original untouched",
          re.fullmatch(r"pixelmon/rerun-[0-9a-f]{8}/x_s5", fc["9"]["inputs"]["filename_prefix"])
          and g0["9"]["inputs"]["filename_prefix"] == "pixelmon/x_s5", fc["9"]["inputs"]["filename_prefix"])
    st.close()

    # --- Ctrl-C / Cancel: our queued prompts go, the running one is interrupted only if it's ours -----------
    for running_pid, want_interrupt in (("ours1", True), ("theirs", False)):
        q = {"queue_running": [[0, running_pid]], "queue_pending": [[1, "ours2"]]}
        st = Stub({"/queue": lambda path, body, q=q: (200, {} if body is not None else q), "/interrupt": {}})
        P.SENT.clear()
        P.SENT[st.url] = {"ours1", "ours2"}
        with contextlib.redirect_stdout(io.StringIO()):
            P.cancel_sent("test")
        deleted = [b.get("delete") for pth, b in st.posts if pth == "/queue"]
        interrupted = any(pth == "/interrupt" for pth, _ in st.posts)
        check(f"cancel deletes our prompts and {'interrupts ours' if want_interrupt else 'leaves another client running'}",
              deleted == [["ours1", "ours2"]] and interrupted == want_interrupt and not P.SENT[st.url],
              f"deleted {deleted}, interrupt {interrupted}")
        st.close()
    P.SENT.clear()

    # --- readable /prompt rejections ------------------------------------------------------------------
    txt = P.rejection_text(json.dumps({
        "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
        "node_errors": {"10": {"class_type": "PixelArtPalette", "errors": [
            {"type": "value_not_in_list", "message": "Value not in list", "details": "palette: 'FOO' not in [...]"}]}}}))
    check("a /prompt rejection reads 'node 10 (PixelArtPalette): …', not raw JSON",
          "node 10 (PixelArtPalette): Value not in list — palette: 'FOO'" in txt and "{" not in txt.split("\n", 1)[1], txt)

    # --- capability check -------------------------------------------------------------------------------
    info = {
        "/object_info/CheckpointLoaderSimple": {"CheckpointLoaderSimple": {"input": {"required": {
            "ckpt_name": [["sd_xl_base_1.0.safetensors"], {}]}}}},
        "/object_info/ControlNetLoader": {"ControlNetLoader": {"input": {"required": {
            "control_net_name": ["COMBO", {"options": ["other.safetensors"]}]}}}},
        "/object_info/PixelArtPalette": {"PixelArtPalette": {"input": {"required": {"image": ["IMAGE"]},
                                                                       "optional": {"palette": [["none"], {}]}}}},
        "/object_info/*": {},
    }
    st = Stub(info)
    g = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
         "53": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "controlnet-union-sdxl-promax.safetensors"}},
         "10": {"class_type": "PixelArtPalette", "inputs": {"image": ["8", 0], "palette": "none", "thin_lines": 2}},
         "70": {"class_type": "SeamlessTile", "inputs": {}}}
    probs = P.graph_problems(g, st.url)
    hint = P.provision_hint(probs)
    check("the capability check finds a missing model (new-style combo), an unknown node input, a missing node",
          len(probs) == 3 and any("controlnet-union" in x for x in probs) and any("thin_lines" in x for x in probs)
          and any("SeamlessTile" in x for x in probs) and "--lab" in hint, " | ".join(probs) + " || " + hint)
    st.close()
    groups = P._model_groups()
    defaults = ["sd_xl_base_1.0.safetensors", "pixel-art-xl.safetensors", "lcm-lora-sdxl.safetensors",
                "controlnet-union-sdxl-promax.safetensors", "controlnet-depth-sdxl-1.0.safetensors",
                "ip-adapter-plus_sdxl_vit-h.safetensors", "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
                "birefnet.safetensors", "hunyuan3d-dit-v2_fp16.safetensors", "sdxl-inpainting-0.1.fp16.safetensors"]
    check("download-models.sh fetches every default model (the provisioning hint can name its group)",
          all(m in groups for m in defaults), f"missing: {[m for m in defaults if m not in groups]}")

    # --- graph wiring ----------------------------------------------------------------------------------
    pal_inputs = node_inputs()
    g = graph("a goblin", "--seed", "5", "--thin-lines", "2", "--pixel-size", "2x1")
    sent = set(g["10"]["inputs"])
    check("every PixelArtPalette input pixelmon sends is one the node declares (ComfyUI drops unknown ones)",
          sent <= pal_inputs, f"undeclared: {sorted(sent - pal_inputs)}")
    check("plain render: SDXL -> pixel LoRA -> KSampler from an empty latent; seed in the file name",
          g["3"]["inputs"]["model"] == ["15", 0] and g["3"]["inputs"]["latent_image"] == ["5", 0]
          and g["3"]["inputs"]["denoise"] == 1.0 and "_s5_sprite" in g["11"]["inputs"]["filename_prefix"],
          g["11"]["inputs"]["filename_prefix"])
    img = os.path.join(tmp, "ref.png")
    mask = os.path.join(tmp, "mask.png")
    if Image:
        Image.new("RGB", (64, 64), (200, 30, 30)).save(img)
        m = Image.new("L", (64, 64), 0)
        m.paste(255, (0, 0, 64, 40))
        m.save(mask)
    else:
        open(img, "wb").close()
    if Image:
        wide = os.path.join(tmp, "wide.png")
        Image.new("RGB", (320, 200)).save(wide)
        g = graph("a goblin", "--init", wide)
        g2 = graph("a goblin", "--init", wide, "--size", "64")
        check("--init without --size keeps the picture's shape (not squashed into a square)",
              (g["10"]["inputs"]["out_width"], g["10"]["inputs"]["out_height"]) == (128, 80)
              and (g2["10"]["inputs"]["out_width"], g2["10"]["inputs"]["out_height"]) == (64, 64))
    g = graph("a goblin", "--init", img, "--denoise", "0.45")
    check("--init: LoadImage -> ImageScale -> VAEEncode is the latent, at --denoise",
          g["3"]["inputs"]["latent_image"] == ["42", 0] and g["42"]["inputs"]["pixels"] == ["41", 0]
          and g["41"]["inputs"]["image"] == ["40", 0] and g["3"]["inputs"]["denoise"] == 0.45
          and g["40"]["inputs"]["image"].startswith("<upload:"))
    if Image:
        for mode, node in (("fill", "VAEEncodeForInpaint"), ("blend", "SetLatentNoiseMask")):
            g = graph("a hat", "--init", img, "--mask", mask, "--mask-mode", mode, "--no-inpaint-crop")
            check(f"--mask {mode}: the latent comes from {node} over the --init picture",
                  g["3"]["inputs"]["latent_image"] == ["48", 0] and g["48"]["class_type"] == node)
    steer = os.path.join(tmp, "steer")
    os.makedirs(steer)
    for i in range(3):
        shutil.copy(img, os.path.join(steer, f"r{i}.png"))
    g = graph("a goblin", "--steer", steer, "--fast")
    check("--steer: IPAdapter sits between the LoRAs and the sampler, all refs batched; --fast chains LCM after",
          g["3"]["inputs"]["model"] == ["22", 0] and g["22"]["inputs"]["model"] == ["16", 0]
          and g["16"]["inputs"]["model"] == ["15", 0] and len(by_class(g, "ImageBatch")) == 2
          and g["3"]["inputs"]["steps"] == 8)
    g = graph("a mech", "--depth", img, "--depth-raw", "--control", img)
    check("--depth after --control: both ControlNets condition the sampler, depth last",
          g["3"]["inputs"]["positive"] == ["d3", 0] and g["d3"]["inputs"]["positive"] == ["55", 0]
          and g["55"]["inputs"]["positive"] == ["6", 0] and g["d1"]["class_type"] == "LoadImage")
    g = graph("a castle", "--art")
    check("--art: no pixel LoRA, no palette node, saved as _art",
          not by_class(g, "PixelArtPalette") and "15" not in g and g["11"]["inputs"]["filename_prefix"].endswith("_art"))
    g = graph("a castle", "--raw")
    check("--raw: the VAE's picture is saved as is", g["11"]["inputs"]["images"] == ["8", 0]
          and g["11"]["inputs"]["filename_prefix"].endswith("_raw"))
    g = graph("stone wall", "--tile")
    check("--tile: circular VAE decode, SeamlessTile wraps the model last, a 3x3 preview is saved",
          g["8"]["class_type"] == "CircularVAEDecode" and g["3"]["inputs"]["model"] == ["70", 0]
          and g["72"]["inputs"]["images"] == ["71", 0])
    g = graph("a gem", "--post-sweep", "angle_grid=pixel,hex;dither=none,bayer4")
    pals = by_class(g, "PixelArtPalette")
    check("--post-sweep: one render, one palette step + save per combination",
          len(pals) == 4 and len(by_class(g, "KSampler")) == 1
          and {g[k]["inputs"]["dithering"] for k in pals} == {"none", "bayer4"})
    g = graph("exactly this", "--exact-prompt")
    check("--exact-prompt sends the prompt word for word", g["6"]["inputs"]["text"] == "exactly this"
          and g["7"]["inputs"]["text"] == "")

    # --- the render farm: breaker, requeue to another box, idle refeed, exit status -------------------------
    def farm(boxes, n, poll_fn, grace=None):
        jobs = [P.Job(i, "x", 100 + i, "none", None, argparse.Namespace(batch=None, batch_file=None)) for i in range(n)]
        calls, saved = [], {}
        for name, fn in (("server_up", lambda s: True), ("check_boxes", lambda boxes, jobs: list(boxes)),
                         ("submit", lambda g, s: (calls.append((s, g)), f"{s}|{g}|{len(calls)}")[1]),
                         ("poll", poll_fn), ("collect", lambda outs, d, s: ["f"]),
                         ("finish", lambda job, files, s: f"{s}:{job.seed}")):
            saved[name] = getattr(P, name)
            setattr(P, name, fn)
        saved_job_graph = P.Job.graph
        P.Job.graph = lambda self, server: self.seed
        old_pool, old_grace = P.POOL, P.POLL_GRACE
        P.POOL = boxes
        if grace is not None:
            P.POLL_GRACE = grace
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                made, lost = P.run_farm(argparse.Namespace(batch=None, batch_file=None), jobs)
        finally:
            for name, fn in saved.items():
                setattr(P, name, fn)
            P.Job.graph = saved_job_graph
            P.POOL, P.POLL_GRACE = old_pool, old_grace
        return made, lost, calls, out.getvalue()

    def poisoned(pid, srv):
        if srv == "bad":
            raise RuntimeError("KSampler (node 3): AttributeError: 'NoneType' object has no attribute 'model_size'")
        return {"11": {}}
    made, lost, calls, log = farm(["bad", "good"], 8, poisoned)
    check("farm: a box failing every job is dropped after 2; its jobs are made on the other box",
          len(made) == 8 and lost == 0 and all(m.startswith("good:") for m in made)
          and sum(1 for s, _ in calls if s == "bad") == 2 and "dropping bad" in log,
          f"made {len(made)}, lost {lost}, sent to bad {sum(1 for s, _ in calls if s == 'bad')}")

    gone = {"t": None}

    def vanishes(pid, srv):                  # 'flaky' answers once, then never again
        if srv == "flaky":
            if gone["t"] is None:
                gone["t"] = 1
                return None
            raise OSError("connection refused")
        return {"11": {}}
    made, lost, calls, log = farm(["flaky", "good"], 3, vanishes, grace=0)
    check("farm: a box that stops answering has its job requeued, and an idle box picks it up",
          len(made) == 3 and lost == 0 and "requeueing" in log, f"made {made}, lost {lost}")

    def always_fails(pid, srv):
        if pid.split("|")[1] == "100":
            raise RuntimeError("KSampler (node 3): ValueError: bad")
        return {"11": {}}
    made, lost, calls, log = farm(["a", "b"], 4, always_fails)
    check("farm: a job that fails on two boxes is given up and counted (the run exits non-zero)",
          len(made) == 3 and lost == 1 and len({s for s, g in calls if g == 100}) == 2,
          f"made {len(made)}, lost {lost}, seed-100 boxes {[s for s, g in calls if g == 100]}")

    # --- targets, wrapper ------------------------------------------------------------------------------
    check("server targets: alias, bare host gets :8188, port and URL kept; loopback is local",
          P.resolve_server("local") == P.SERVERS["local"].rstrip("/")
          and P.resolve_server("10.0.0.5") == "http://10.0.0.5:8188"
          and P.resolve_server("10.0.0.5:8190") == "http://10.0.0.5:8190"
          and P.is_local("http://127.0.0.1:8189") and not P.is_local("http://192.168.1.5:8188")
          and P.on_this_disk("http://127.0.0.1:8188") and not P.on_this_disk("http://127.0.0.1:8189"))
    py = sys.executable

    def local_urls(*args, env=None):
        r = subprocess.run([py, os.path.join(HERE, "pixelmon.py"), "--wrapper-local-url", *args],
                           capture_output=True, text=True, env=dict(os.environ, **(env or {})))
        return [x for x in r.stdout.split() if x.startswith("http") or x == "farm"]
    answers = {
        "x --server http://127.0.0.1:8189": local_urls("x", "--server", "http://127.0.0.1:8189"),
        "x --server=10.9.9.9": local_urls("x", "--server=10.9.9.9"),
        "x --server 10.9.9.9,127.0.0.1": local_urls("x", "--server", "10.9.9.9,127.0.0.1"),
        "--version": local_urls("--version"),
        "x --dry-run": local_urls("x", "--dry-run", "--server", "127.0.0.1"),
        "x --list-servers": local_urls("--list-servers", "--server", "127.0.0.1"),
        "-h": local_urls("-h", "--server", "127.0.0.1"),
    }
    want = {"x --server http://127.0.0.1:8189": ["http://127.0.0.1:8189"], "x --server=10.9.9.9": [],
            "x --server 10.9.9.9,127.0.0.1": ["http://127.0.0.1:8188", "farm"], "--version": [],
            "x --dry-run": [], "x --list-servers": [], "-h": []}
    check("the wrapper is told exactly which local ComfyUI a command needs (none for remote/help/--version/dry runs)",
          answers == want, f"{ {k: v for k, v in answers.items() if v != want[k]} }")
    if shutil.which("bash") and shutil.which("curl"):
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "ComfyUI", ".venv", "bin"))
        os.symlink(py, os.path.join(home, "ComfyUI", ".venv", "bin", "python"))
        os.symlink(os.path.join(HERE, "pixelmon.py"), os.path.join(home, "ComfyUI", "pixelmon.py"))
        stub = os.path.join(home, "launch-comfyui.sh")
        with open(stub, "w") as f:
            f.write('#!/usr/bin/env bash\necho "$*" >> "$HOME/launched"\nexit 3\n')
        os.chmod(stub, 0o755)
        launched = {}
        for args in (["x", "--server", "http://127.0.0.1:1"], ["x", "--server=10.255.255.1", "--dry-run"], ["--version"]):
            subprocess.run(["bash", os.path.join(HERE, "bin", "pixelmon"), *args], capture_output=True,
                           env=dict(os.environ, HOME=home, COMFYUI_DIR=os.path.join(home, "ComfyUI")), timeout=60)
            launched[" ".join(args)] = os.path.exists(os.path.join(home, "launched"))
        check("bin/pixelmon starts no ComfyUI for another local port, --server=HOST or --version",
              not any(launched.values()), str(launched))

    # --- provenance, sizes, lists ---------------------------------------------------------------------------
    if Image:
        from PIL import PngImagePlugin
        f = os.path.join(tmp, "s_sprite_00001_.png")
        meta = PngImagePlugin.PngInfo()
        meta.add_text("prompt", '{"3": {}}')
        im = Image.new("RGB", (5, 3))
        im.putpixel((1, 1), (255, 0, 0))
        im.save(f, pnginfo=meta)
        P.enlarge_output(argparse.Namespace(enlarge=3, raw=False), f)
        P.stamp_recipe(f, argparse.Namespace(), 7, "a goblin", "http://127.0.0.1:8188")
        out = Image.open(f)
        px = out.load()
        square = out.size == (15, 9) and all(px[x, y] == (255, 0, 0) for x in range(3, 6) for y in range(3, 6)) \
            and px[2, 3] != (255, 0, 0)
        rec = json.loads(out.text.get("pixelmon", "{}"))
        check("enlarging past 1024 keeps whole square pixels and the PNG's recipe (ComfyUI's + pixelmon's)",
              square and out.text.get("prompt") == '{"3": {}}' and rec.get("seed") == 7, f"{out.size} {list(out.text)}")
        pal16 = os.path.join(tmp, "pal.png")
        p16 = Image.new("RGB", (4, 4))
        for i in range(16):
            p16.putpixel((i % 4, i // 4), (i * 16, 255 - i * 16, 80))
        p16.save(pal16)
        from colors import picture_palette
        cols = picture_palette(pal16, cap=32, exact=32)
        check("--palette-from takes a pixel-art picture's exact colours", len(cols) == 16 and "#00ff50" in cols, cols[:3])

    rows = manifest.parse_manifest("# list\n[Foes]\nenemies/bat | a bat\nrat | geometric | 32x48 | a rat, red\n"
                                   "door | 64 | a door\na glowing potion\nansi-art/x | skip\n")
    check("--batch-file rows: key | prompt, style + size, size alone, bare prompts; other media skipped",
          [r["key"] for r in rows] == ["enemies/bat", "rat", "door", "a-glowing-potion"]
          and rows[1]["style"] == "geometric" and rows[1]["size"] == "32x48" and rows[1]["prompt"] == "a rat, red"
          and rows[2]["size"] == "64" and rows[0]["group"] == "Foes", str(rows[:2]))
    try:
        manifest.parse_manifest("a | x\na | y\n")
        dup = False
    except ValueError:
        dup = True
    check("a duplicate key in a sprite list is an error (two rows would write one file)", dup)
    sys.path.insert(0, os.path.join(HERE, "gui"))
    try:
        import sets as gui_sets
        sample = ("[A]\nenemies/bat | a bat\nrat | geometric | 32x48 | a rat\ndoor | 64 | a door\nplain words\n"
                  "pixel-art/x.png | y\nansi-art/z | q\ntown | hut | dark | 16x16 | a hut, small\n")
        bad_lists = ("/etc/x | y", "a/../b | y", "a | x\na | y", "# nothing\n")

        def both(t):
            out = []
            for parse in (manifest.parse_manifest, gui_sets.parse_manifest):
                try:
                    out.append(parse(t))
                except ValueError:
                    out.append("error")
            return out
        same = all(x == y for x, y in (both(t) for t in (sample,) + bad_lists))
        check("--batch-file and the GUI's Sets read a list the same way (manifest.py vs gui/sets.py)", same)
    except ImportError as e:
        check("--batch-file and the GUI's Sets read a list the same way (manifest.py vs gui/sets.py)", False, str(e))
    d = os.path.join(tmp, "made")
    os.makedirs(d)
    for s_ in (11, 12):
        open(os.path.join(d, f"bat_32x32_none_s{s_}_sprite_00001_.png"), "w").close()
    open(os.path.join(d, "bat_wing_32x32_none_s13_sprite_00001_.png"), "w").close()
    ns = argparse.Namespace(batch="bat", seed=-1, name=None)
    jobs = [P.Job(i, "bat", 200 + i, "none", d, ns) for i in range(4)]
    with contextlib.redirect_stdout(io.StringIO()):
        left = P.skip_made(ns, jobs)
        ns2 = argparse.Namespace(batch="bat", seed=11, name=None)
        left2 = P.skip_made(ns2, [P.Job(0, "bat", 11, "none", d, ns2), P.Job(1, "bat", 12, "none", d, ns2),
                                  P.Job(2, "bat", 14, "none", d, ns2)])
    check("--skip-existing: 2 of 4 takes made -> 2 to go (bat_wing isn't bat); a fixed seed with a file is skipped",
          len(left) == 2 and [j.seed for j in left2] == [14], f"{len(left)} left, seeds {[j.seed for j in left2]}")

    combos = P.parse_post_sweep("angle_grid=pixel,hex;dither=none,bayer4;dither_amount=0.5,1",
                                argparse.ArgumentParser())
    check("--post-sweep: dither 'none' is made once, not once per amount", len(combos) == 6,
          f"{len(combos)} combinations")

    # every flag is in the help (a hidden flag is a flag nobody finds)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.print_help()
    cap = {}

    def grab(self, *a_, **k):
        cap["p"] = self
        raise SystemExit
    orig = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = grab
    try:
        sys.argv = ["pixelmon"]
        P.main()
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig
    hidden = [o for act in cap["p"]._actions if act.help != argparse.SUPPRESS for o in act.option_strings
              if not re.search(re.escape(o) + r"(?![\w-])", buf.getvalue())]
    check("--help mentions every flag", not hidden, f"missing: {hidden}")

    shutil.rmtree(tmp, ignore_errors=True)
    bad = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        if a.verbose or not ok:
            print(f"{'✓' if ok else '✗'} {name}" + (f"\n    {detail}" if detail and (a.verbose or not ok) else ""))
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
