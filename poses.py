"""Poses for sprite animation: a 3D stick figure moved through a move (walk, run, idle, attack, hurt, death,
jump), turned to face any way, seen from the side or from above, and drawn as an OpenPose skeleton - the
picture the pose ControlNet follows.

Units: the figure is 1 tall, feet on y = 0. Its own frame: r (its right), u (up), f (forward). A turn (yaw,
degrees) of 0 faces the viewer, 90 faces the picture's right, 180 away, 270 left. pitch tilts the camera down
(0 = side view, 30 = a top-down RPG's view).
"""
import math

from PIL import Image, ImageDraw

# OpenPose COCO-18 keypoints and the limb colours its ControlNets were trained on
NOSE, NECK, RSHO, RELB, RWRI, LSHO, LELB, LWRI, RHIP, RKNE, RANK, LHIP, LKNE, LANK, REYE, LEYE, REAR, LEAR = range(18)
LIMBS = [(NECK, RSHO), (NECK, LSHO), (RSHO, RELB), (RELB, RWRI), (LSHO, LELB), (LELB, LWRI), (NECK, RHIP),
         (RHIP, RKNE), (RKNE, RANK), (NECK, LHIP), (LHIP, LKNE), (LKNE, LANK), (NECK, NOSE), (NOSE, REYE),
         (REYE, REAR), (NOSE, LEYE), (LEYE, LEAR)]
COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0),
          (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255),
          (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]

# body: (r, u, f) at rest; limb lengths
NECK_P, HEAD_C = (0.0, 0.82, 0.0), (0.0, 0.91, 0.0)
SHO_R, HIP_R, HIP_U = 0.11, 0.065, 0.50
UPPER_ARM, FOREARM, THIGH, SHIN = 0.17, 0.15, 0.24, 0.235


def _rot_r(p, ang, about):
    """p (r, u, f) turned by ang degrees round the r axis through about: + leans forward (top towards +f)."""
    a = math.radians(ang)
    r, u, f = p
    u0, f0 = about[1], about[2]
    du, df = u - u0, f - f0
    return (r, u0 + du * math.cos(a) - df * math.sin(a), f0 + du * math.sin(a) + df * math.cos(a))


def _limb(start, side, swing, flex, abduct, l1, l2, flex_back):
    """A two-bone limb from start: swing (degrees from straight down, + forward), flex at the middle joint
    (+ = the knee bending back / the elbow forward), abduct (degrees out to the side). -> (mid, end)."""
    def seg(p, ang, abd, ln):
        a, b = math.radians(ang), math.radians(abd)
        return (p[0] + side * ln * math.sin(b), p[1] - ln * math.cos(a) * math.cos(b), p[2] + ln * math.sin(a) * math.cos(b))
    mid = seg(start, swing, abduct, l1)
    end = seg(mid, swing - flex if flex_back else swing + flex, abduct * 0.6, l2)
    return mid, end


def body(q):
    """Keypoints (r, u, f) of the figure for pose q (a dict; anything left out is the rest pose)."""
    g = q.get
    lean, hip_u = g("lean", 0.0), HIP_U + g("hip_dy", 0.0)
    hipc = (0.0, hip_u, g("hip_df", 0.0))
    up = lambda p: _rot_r((p[0], p[1] - HIP_U + hip_u, p[2] + hipc[2]), lean, hipc)
    sh_up = g("shrug", 0.0)
    k = [None] * 18
    k[NECK] = up(NECK_P)
    head = lambda p: _rot_r(up(p), g("head", 0.0), k[NECK])
    k[NOSE] = head((0.0, 0.90, 0.075))
    k[REYE], k[LEYE] = head((0.033, 0.925, 0.06)), head((-0.033, 0.925, 0.06))
    k[REAR], k[LEAR] = head((0.065, 0.915, -0.01)), head((-0.065, 0.915, -0.01))
    k[RSHO], k[LSHO] = up((SHO_R, 0.81 + sh_up, 0.0)), up((-SHO_R, 0.81 + sh_up, 0.0))
    for side, sho, elb, wri, pre in ((1, RSHO, RELB, RWRI, "r"), (-1, LSHO, LELB, LWRI, "l")):
        k[elb], k[wri] = _limb(k[sho], side, g(f"arm_{pre}", 0.0) + lean, g(f"elbow_{pre}", 12.0),
                               g(f"abd_{pre}", 6.0), UPPER_ARM, FOREARM, flex_back=False)
    for side, hip, kne, ank, pre in ((1, RHIP, RKNE, RANK, "r"), (-1, LHIP, LKNE, LANK, "l")):
        k[hip] = (side * HIP_R, hip_u, hipc[2])
        k[kne], k[ank] = _limb(k[hip], side, g(f"thigh_{pre}", 0.0), g(f"knee_{pre}", 4.0),
                               g(f"spread_{pre}", 2.0), THIGH, SHIN, flex_back=True)
    roll = g("roll", 0.0)                         # the whole body tipping (falling): + forward, - backward
    if roll:
        piv = (0.0, 0.03, k[RANK][2] if roll < 0 else 0.0)
        k = [_rot_r(p, roll, piv) for p in k]
    if g("ground", True):                         # the lower foot (or lowest point) on the floor
        low = min(p[1] for p in k)
        k = [(p[0], p[1] - low + 0.03, p[2]) for p in k]
    lift = g("lift", 0.0)
    return [(p[0], p[1] + lift, p[2] + g("move_f", 0.0)) for p in k]


def project(k, yaw=90.0, pitch=0.0):
    """Keypoints -> (screen x right, screen y up, depth towards the viewer), and which face points show."""
    y, p = math.radians(yaw), math.radians(pitch)
    F = (math.sin(y), 0.0, math.cos(y))              # its forward, in the world (camera on +z)
    R = (-math.cos(y), 0.0, math.sin(y))             # its right
    out = []
    for r, u, f in k:
        X = r * R[0] + f * F[0]
        Z = r * R[2] + f * F[2]
        out.append((X, u * math.cos(p) - Z * math.sin(p), Z * math.cos(p) + u * math.sin(p)))
    cam = (0.0, math.sin(p), math.cos(p))
    dot = lambda v: v[0] * cam[0] + v[1] * cam[1] + v[2] * cam[2]
    face = dot(F)
    side = dot(R)
    show = [True] * 18
    show[NOSE] = face > -0.15
    show[REYE] = face + 0.75 * side > 0.05
    show[LEYE] = face - 0.75 * side > 0.05
    show[REAR] = side - 0.35 * face > -0.35
    show[LEAR] = -side - 0.35 * face > -0.35
    return out, show


def draw(img, k2, show, box, height_px, foot_px):
    """Draw one skeleton (projected points) into box (x0, y0, x1, y1) of img: height_px tall, feet foot_px
    above the box's bottom, centred."""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    to = lambda p: (cx + p[0] * height_px, y1 - foot_px - p[1] * height_px)
    sw = max(2.0, height_px / 85)                  # OpenPose's stick 4 px wide at ~340 px tall
    over = Image.new("RGB", img.size)
    od = ImageDraw.Draw(over)
    for i, (a, b) in enumerate(LIMBS):
        if not (show[a] and show[b]):
            continue
        (ax, ay), (bx, by) = to(k2[a]), to(k2[b])
        ln = math.hypot(bx - ax, by - ay)
        if ln < 1:
            continue
        nx, ny = -(by - ay) / ln * sw, (bx - ax) / ln * sw
        c = tuple(int(v * 0.6) for v in COLORS[i])
        # a long thin diamond, as OpenPose's ellipse: widest in the middle
        mx, my = (ax + bx) / 2, (ay + by) / 2
        od.polygon([(ax, ay), (mx + nx, my + ny), (bx, by), (mx - nx, my - ny)], fill=c)
    img.paste(over, (0, 0), over.convert("L").point(lambda v: 255 if v else 0))
    for i in range(18):
        if show[i]:
            x, y = to(k2[i])
            d.ellipse((x - sw, y - sw, x + sw, y + sw), fill=COLORS[i])


# ---------------------------------------------------------------- moves

def _walk(t, run=False):
    ph = 2 * math.pi * t
    A, K, ARM, EL, LEAN = (38, 95, 50, 85, 14) if run else (24, 46, 22, 18, 4)
    q = {"lean": LEAN, "head": -LEAN * 0.6}
    for pre, o in (("r", 0.0), ("l", math.pi)):
        s, c = math.sin(ph + o), math.cos(ph + o)
        q[f"thigh_{pre}"] = A * s
        q[f"knee_{pre}"] = 5 + K * max(0.0, c) ** (1.2 if run else 2.0) + (12 if run else 0) * max(0.0, -s)
        q[f"arm_{pre}"] = -ARM * s
        q[f"elbow_{pre}"] = EL + 10 * max(0.0, -s)
    if run:                                         # in the air between steps
        q["lift"] = 0.05 * max(0.0, math.cos(2 * ph))
    return q


def _idle(t):
    b = (1 - math.cos(2 * math.pi * t)) / 2         # breathing in and out
    return {"hip_dy": -0.008 * b, "shrug": 0.006 * b, "arm_r": 3 * b, "arm_l": 3 * b, "elbow_r": 14 + 4 * b,
            "elbow_l": 14 + 4 * b, "knee_r": 4 + 6 * b, "knee_l": 4 + 6 * b, "head": -2 * b}


ATTACK = [  # an overhead swing with the right (near) arm, stepping into it
    {"arm_r": 25, "elbow_r": 50, "arm_l": 10, "thigh_r": -8, "thigh_l": 12, "knee_l": 10},
    {"arm_r": 205, "elbow_r": 70, "abd_r": 12, "arm_l": -25, "lean": -6, "head": 4, "thigh_r": -10, "thigh_l": 14},
    {"arm_r": 165, "elbow_r": 25, "arm_l": -30, "lean": 2, "thigh_r": -14, "thigh_l": 22, "knee_l": 18},
    {"arm_r": 75, "elbow_r": 0, "arm_l": -35, "lean": 16, "thigh_r": -20, "thigh_l": 34, "knee_l": 40, "head": -6},
    {"arm_r": 15, "elbow_r": 10, "arm_l": -20, "lean": 20, "thigh_r": -22, "thigh_l": 36, "knee_l": 46, "head": -8},
    {"arm_r": 20, "elbow_r": 40, "arm_l": 5, "lean": 8, "thigh_r": -12, "thigh_l": 18, "knee_l": 18},
]
HURT = [
    {},
    {"lean": -16, "head": 16, "arm_r": 45, "elbow_r": 50, "abd_r": 30, "arm_l": 40, "elbow_l": 50, "abd_l": 30,
     "thigh_r": -14, "thigh_l": 10, "knee_r": 16, "hip_df": -0.03},
    {"lean": -24, "head": 22, "arm_r": 70, "elbow_r": 30, "abd_r": 40, "arm_l": 60, "elbow_l": 35, "abd_l": 40,
     "thigh_r": -20, "thigh_l": 14, "knee_r": 22, "knee_l": 10, "hip_df": -0.05},
    {"lean": -8, "head": 8, "arm_r": 20, "elbow_r": 30, "arm_l": 18, "elbow_l": 30, "thigh_r": -8, "knee_r": 8},
]
DEATH = [
    {"lean": -14, "head": 14, "arm_r": 50, "abd_r": 30, "arm_l": 45, "abd_l": 30, "thigh_r": -12, "knee_r": 14},
    {"lean": 10, "head": -10, "arm_r": 15, "elbow_r": 40, "arm_l": 10, "elbow_l": 40, "thigh_r": 10, "knee_r": 55,
     "thigh_l": -5, "knee_l": 40},
    {"lean": 22, "head": -16, "arm_r": 30, "elbow_r": 50, "arm_l": 25, "elbow_l": 45, "thigh_r": 70, "knee_r": 120,
     "thigh_l": 30, "knee_l": 110},
    {"lean": 30, "head": -20, "arm_r": 50, "elbow_r": 30, "arm_l": 40, "elbow_l": 30, "thigh_r": 75, "knee_r": 125,
     "thigh_l": 40, "knee_l": 115, "roll": -35},
    {"lean": 10, "head": -8, "arm_r": 100, "elbow_r": 20, "abd_r": 20, "arm_l": 90, "elbow_l": 20,
     "thigh_r": 20, "knee_r": 40, "thigh_l": 10, "knee_l": 20, "roll": -75},
    {"lean": 0, "head": 0, "arm_r": 160, "elbow_r": 10, "abd_r": 25, "arm_l": 150, "elbow_l": 10, "abd_l": 20,
     "thigh_r": 5, "knee_r": 10, "thigh_l": 0, "knee_l": 5, "roll": -88},
]
JUMP = [  # crouch, leap (off the floor, legs trailing), tucked at the top, coming down, landing
    {"lean": 22, "head": -12, "hip_dy": 0, "thigh_r": 50, "knee_r": 95, "thigh_l": 44, "knee_l": 90,
     "arm_r": -45, "elbow_r": 20, "arm_l": -40, "elbow_l": 20},
    {"lean": 10, "arm_r": 150, "elbow_r": 20, "arm_l": 135, "elbow_l": 25, "thigh_r": 35, "knee_r": 70,
     "thigh_l": -20, "knee_l": 45, "lift": 0.08},
    {"lean": 4, "arm_r": 100, "elbow_r": 50, "arm_l": 90, "elbow_l": 50, "thigh_r": 75, "knee_r": 120,
     "thigh_l": 65, "knee_l": 115, "lift": 0.14},
    {"lean": -4, "arm_r": 95, "elbow_r": 10, "abd_r": 50, "arm_l": 85, "abd_l": 50, "thigh_r": 25, "knee_r": 35,
     "thigh_l": 5, "knee_l": 30, "lift": 0.06},
    {"lean": 20, "head": -10, "thigh_r": 45, "knee_r": 85, "thigh_l": 40, "knee_l": 80, "arm_r": 30,
     "elbow_r": 30, "arm_l": 25, "elbow_l": 30},
]


def _keyed(keys, n, loop=False):
    """n poses spread over keyframes keys (blended between them)."""
    if n == len(keys) and not loop:
        return [dict(k) for k in keys]
    out = []
    m = len(keys) if loop else len(keys) - 1
    for i in range(n):
        s = (i / n if loop else i / max(1, n - 1)) * m
        a = int(math.floor(s)) % len(keys)
        b = (a + 1) % len(keys)
        w = s - math.floor(s)
        ka, kb = keys[a], keys[b]
        q = {}
        for key in set(ka) | set(kb):
            va = ka.get(key, _REST.get(key, 0.0))
            vb = kb.get(key, _REST.get(key, 0.0))
            q[key] = va + (vb - va) * w
        out.append(q)
    return out


_REST = {"elbow_r": 12.0, "elbow_l": 12.0, "knee_r": 4.0, "knee_l": 4.0, "abd_r": 6.0, "abd_l": 6.0,
         "spread_r": 2.0, "spread_l": 2.0}

# move -> (default frame count, loops?, words for the prompt)
MOVES = {
    "walk":   (6, True, "walking"),
    "run":    (6, True, "running fast"),
    "idle":   (4, True, "standing idle, relaxed"),
    "attack": (6, False, "swinging a weapon overhead, attacking"),
    "hurt":   (4, False, "recoiling in pain, hit"),
    "death":  (6, False, "falling down defeated, dying"),
    "jump":   (5, False, "jumping"),
    "stand":  (1, False, "standing"),
}


# what each keyframe is, for the prompt (the skeleton alone is read loosely: a jump came out standing)
KEY_WORDS = {
    "attack": ["raising a weapon to strike", "weapon raised high overhead", "swinging the weapon down hard",
               "striking forward, lunging", "follow-through of a strike, leaning forward", "recovering after a strike"],
    "hurt": ["standing", "flinching back, hit hard", "reeling backwards in pain", "recovering from a hit"],
    "death": ["staggering, mortally wounded", "knees buckling", "collapsing to the knees", "falling over sideways",
              "falling to the ground", "lying flat on the ground, defeated, eyes closed"],
    "jump": ["crouching low, about to jump", "leaping up into the air, arms raised", "in mid air, knees tucked up",
             "falling back down, arms out", "landing in a crouch"],
}


def frame_words(move, i, n):
    """The prompt's words for frame i of n of a move: its keyframe's own words, else the move's."""
    kw = KEY_WORDS.get(move)
    if not kw:
        return MOVES[move][2]
    return kw[min(len(kw) - 1, round(i * (len(kw) - 1) / max(1, n - 1)))]


def move_poses(move, n=None):
    """The n poses of a move (n None: its usual count)."""
    n = n or MOVES[move][0]
    if move in ("walk", "run"):
        return [_walk(i / n, move == "run") for i in range(n)]
    if move == "idle":
        return [_idle(i / n) for i in range(n)]
    if move == "stand":
        return [{} for _ in range(n)]
    keys = {"attack": ATTACK, "hurt": HURT, "death": DEATH, "jump": JUMP}[move]
    return _keyed(keys, n)


DIRECTIONS = {4: [0, 90, 180, 270], 8: [0, 45, 90, 135, 180, 225, 270, 315]}
DIR_WORDS = {0: "front view, facing the viewer", 45: "three-quarter view facing right", 90: "side view facing right",
             135: "three-quarter back view facing right, seen from behind",
             180: "back view, seen from behind, facing away from the viewer, the back of the head",
             225: "three-quarter back view facing left, seen from behind", 270: "side view facing left",
             315: "three-quarter view facing left"}
DIR_NAMES = {0: "s", 45: "se", 90: "e", 135: "ne", 180: "n", 225: "nw", 270: "w", 315: "sw"}


def sheet(frames, cols, cell_w, cell_h, fill=0.8, foot=0.06):
    """A pose sheet: frames [(keypoints3d, yaw, pitch)] drawn left to right, top to bottom in cells of
    cell_w x cell_h. One scale for every move (a standing figure fill of the cell tall), so a character is
    the same size in all its sheets; the same floor (foot of the cell up) in every cell, so the motion's bob
    stays. The whole sheet is shifted sideways once, so a fall or a lunge stays inside its cells."""
    rows = math.ceil(len(frames) / cols)
    img = Image.new("RGB", (cols * cell_w, rows * cell_h))
    proj = [project(k, yaw, pitch) for k, yaw, pitch in frames]
    scale = fill * cell_h
    xs = [p[0] for k2, show in proj for i, p in enumerate(k2) if show[i]]
    shift = -(min(xs) + max(xs)) / 2 if (max(xs) - min(xs)) * scale > 0.7 * cell_w else 0.0
    for i, (k2, show) in enumerate(proj):
        cx, cy = (i % cols) * cell_w, (i // cols) * cell_h
        k2 = [(p[0] + shift, p[1], p[2]) for p in k2]
        draw(img, k2, show, (cx, cy, cx + cell_w, cy + cell_h), scale, foot * cell_h)
    return img
