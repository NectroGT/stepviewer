"""Random parametric parts for VLM training: each part is a short CadQuery build() in the style vlm.py asks for,
plus the STEP it makes. The code that made a part is the correct answer for its drawing, so nothing needs labelling.

    python training/gen_parts.py OUT_DIR N [--seed S] [--workers K]

Writes OUT_DIR/parts/<id>/answer.py (feature lines + build()), model.step, meta.json and OUT_DIR/parts.jsonl.
Families (share): shaft 16%, bushing 12%, flange 12% (turned: 40%), housing 22% (several features), plate 14%,
bracket 12%, stepped block 12%. make_dataset.py then drops most parts the rules converter already handles.
Every part is built in the model frame vlm.py uses: x along the part, y from the front face backwards, z up,
lowest corner at the origin, millimetres."""
import argparse, json, math, os, random, subprocess, sys, tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed

FAMILIES = [("shaft", 16), ("bushing", 12), ("flange", 12), ("housing", 22), ("plate", 14), ("bracket", 12),
            ("block", 12)]
DRILLS = [3.4, 4.5, 5.5, 6.6, 9.0, 11.0, 13.5]                    # clearance holes M3 .. M12
CBORE = {3.4: (6.5, 3.4), 4.5: (8.0, 4.4), 5.5: (10.0, 5.4), 6.6: (11.0, 6.5), 9.0: (15.0, 8.6),
         11.0: (18.0, 10.8), 13.5: (20.0, 13.0)}                  # drill: (counterbore diameter, depth)
CSK = {3.4: 6.5, 4.5: 9.0, 5.5: 11.0, 6.6: 13.0, 9.0: 17.5, 11.0: 21.0, 13.5: 25.0}


def nice(v, step=1.0):
    return round(round(v / step) * step, 3)


def f(v):
    """a number as plain code: 12 not 12.0"""
    v = round(float(v), 3)
    return str(int(v)) if v == int(v) else repr(v)


def pts(ps):
    return "[" + ", ".join(f"({f(a)}, {f(b)})" for a, b in ps) + "]"


def code_of(lines, body):
    """the answer: feature lines, then the code block"""
    return ("Features:\n" + "\n".join("- " + l for l in lines) + "\n\n```python\nimport cadquery as cq\nimport math\n\n\n"
            "def build():\n" + "\n".join("    " + l if l else "" for l in body) + "\n```\n")


# ------------------------------------------------------------------ prismatic parts

def hole_pattern(r, L, W, d, margin):
    """hole centres on an L x W face: corners, a row, or a grid"""
    m = max(margin, d * 1.5)
    kind = r.choice(["corners", "corners", "row", "grid"])
    if kind == "corners" or L < 4 * m or W < 4 * m:
        return [(m, m), (L - m, m), (m, W - m), (L - m, W - m)], "4X at the corners"
    if kind == "row":
        n = r.randint(2, max(2, min(5, int((L - 2 * m) // (3 * d)) + 1)))
        y = nice(W / 2, 0.5)
        xs = [nice(m + i * (L - 2 * m) / (n - 1), 0.5) for i in range(n)]
        return [(x, y) for x in xs], f"{n}X in a row along x at y = {f(y)}"
    nx, ny = r.randint(2, 3), 2
    xs = [nice(m + i * (L - 2 * m) / (nx - 1), 0.5) for i in range(nx)]
    ys = [m, W - m]
    return [(x, y) for x in xs for y in ys], f"{nx * ny}X in a {nx} x {ny} grid"


def holes_code(r, face_z, ps, d, through, depth_limit):
    """hole calls on the top face at z = face_z; returns (body lines, feature text)"""
    kind = r.choices(["thru", "cbore", "csk", "blind"], [5, 2, 2, 1])[0]
    head = f"part = part.faces(\">Z\").workplane(origin=(0, 0, {f(face_z)})).pushPoints({pts(ps)})"
    n = len(ps)
    if kind == "cbore" and CBORE[d][1] < depth_limit - 1:
        cd, cdep = CBORE[d]
        return [head + f".cboreHole({f(d)}, {f(cd)}, {f(cdep)})"], f"{n}X Ø{f(d)} thru, counterbore Ø{f(cd)} x {f(cdep)} deep"
    if kind == "csk":
        cd = CSK[d]
        return [head + f".cskHole({f(d)}, {f(cd)}, 90)"], f"{n}X Ø{f(d)} thru, countersink Ø{f(cd)} x 90°"
    if kind == "blind" and depth_limit > 6:
        dep = nice(depth_limit * r.uniform(0.4, 0.7), 0.5)
        return [head + f".hole({f(d)}, {f(dep)})"], f"{n}X Ø{f(d)} x {f(dep)} deep (blind)"
    return [head + f".hole({f(d)})"], f"{n}X Ø{f(d)} thru"


def plate(r):
    L = nice(r.uniform(40, 220), 5); W = nice(r.uniform(25, min(L, 150)), 5); T = nice(r.uniform(4, 25), 1)
    lines = [f"plate {f(L)} x {f(W)} x {f(T)} (x, y, z)"]
    body = [f"part = cq.Workplane(\"XY\").box({f(L)}, {f(W)}, {f(T)}, centered=False)"]
    if r.random() < 0.5:
        rad = nice(r.uniform(2, min(L, W) / 6), 1)
        body.append(f"part = part.edges(\"|Z\").fillet({f(rad)})")
        lines.append(f"corners rounded R{f(rad)}")
    d = r.choice([x for x in DRILLS if x < min(L, W) / 5] or [3.4])
    pocket = r.random() < 0.55 and T >= 6
    if pocket:                                                       # holes at the corners, clear of the pocket
        m = max(6, nice(min(L, W) * 0.12, 1), d * 1.5)
        ps, where = [(m, m), (L - m, m), (m, W - m), (L - m, W - m)], "4X at the corners"
    else:
        ps, where = hole_pattern(r, L, W, d, max(6, nice(min(L, W) * 0.12, 1)))
    hb, ht = holes_code(r, T, ps, d, True, T)
    body += hb; lines.append(f"holes {ht}, {where}")
    if pocket:                                                       # a pocket or a slot in the middle
        pw, ph = nice(L * r.uniform(0.25, 0.45), 1), nice(W * r.uniform(0.2, 0.4), 1)
        dep = nice(T * r.uniform(0.3, 0.6), 0.5)
        cx, cy = nice(L / 2, 0.5), nice(W / 2, 0.5)
        if r.random() < 0.5:
            body.append(f"part = part.faces(\">Z\").workplane(origin=(0, 0, {f(T)})).center({f(cx)}, {f(cy)})"
                        f".slot2D({f(pw)}, {f(ph)}).cutBlind(-{f(dep)})")
            lines.append(f"slot {f(pw)} long x {f(ph)} wide x {f(dep)} deep, centred at ({f(cx)}, {f(cy)})")
        else:
            body.append(f"part = part.faces(\">Z\").workplane(origin=(0, 0, {f(T)})).center({f(cx)}, {f(cy)})"
                        f".rect({f(pw)}, {f(ph)}).cutBlind(-{f(dep)})")
            lines.append(f"rectangular pocket {f(pw)} x {f(ph)} x {f(dep)} deep, centred at ({f(cx)}, {f(cy)})")
    body.append("return part")
    return lines, body


def block(r):
    """a block with a step (rebate) along one side and holes in the lower level"""
    L = nice(r.uniform(40, 160), 5); W = nice(r.uniform(30, 100), 5); H = nice(r.uniform(15, 60), 1)
    sw = nice(W * r.uniform(0.35, 0.6), 1); sh = nice(H * r.uniform(0.35, 0.65), 1)
    lines = [f"block {f(L)} x {f(W)} x {f(H)} (x, y, z)",
             f"step along x on the front: {f(sw)} deep in y, down to z = {f(H - sh)}"]
    body = [f"part = cq.Workplane(\"XY\").box({f(L)}, {f(W)}, {f(H)}, centered=False)",
            f"part = part.cut(cq.Workplane(\"XY\").box({f(L)}, {f(sw)}, {f(sh)}, centered=False)"
            f".translate((0, 0, {f(H - sh)})))"]
    d = r.choice([x for x in DRILLS if x < sw / 2.5] or [3.4])
    m = max(nice(sw / 2, 0.5), d)
    n = r.randint(2, 4)
    xs = [nice(L * (i + 1) / (n + 1), 0.5) for i in range(n)]
    ps = [(x, nice(sw / 2, 0.5)) for x in xs]
    hb, ht = holes_code(r, H - sh, ps, d, True, H - sh)
    body += hb
    lines.append(f"holes in the lower level: {ht} at x = {', '.join(f(x) for x in xs)}, y = {f(nice(sw / 2, 0.5))}")
    if r.random() < 0.5:                                             # holes in the upper level too
        d2 = r.choice([x for x in DRILLS if x < (W - sw) / 2.5] or [3.4])
        y2 = nice(sw + (W - sw) / 2, 0.5)
        ps2 = [(nice(L * 0.2, 0.5), y2), (nice(L * 0.8, 0.5), y2)]
        body.append(f"part = part.faces(\">Z\").workplane(origin=(0, 0, {f(H)})).pushPoints({pts(ps2)}).hole({f(d2)})")
        lines.append(f"2X Ø{f(d2)} thru in the upper level at y = {f(y2)}")
    body.append("return part")
    return lines, body


def bracket(r):
    """an L bracket: base along x-y, upright leg at the back"""
    L = nice(r.uniform(40, 140), 5); W = nice(r.uniform(30, 100), 5); H = nice(r.uniform(30, 110), 5)
    t = nice(r.uniform(4, 15), 1)
    prof = [(0, 0), (L, 0), (L, t), (t, t), (t, H), (0, H)]
    lines = [f"L bracket, profile in the x-z plane: base {f(L)} long x {f(t)} thick, upright {f(H)} high x {f(t)} "
             f"thick at x = 0; {f(W)} deep in y"]
    body = [f"part = cq.Workplane(\"XZ\").polyline({pts(prof)}).close().extrude(-{f(W)})"]
    if r.random() < 0.6:
        rad = nice(t * r.uniform(0.8, 1.5), 1)
        body.append(f"part = part.edges(\"|Y\").edges(cq.selectors.NearestToPointSelector(({f(t)}, 0, {f(t)})))"
                    f".fillet({f(rad)})")
        lines.append(f"inside corner rounded R{f(rad)}")
    d = r.choice([x for x in DRILLS if x < min(W, L - t) / 4] or [3.4])
    xb = nice(t + (L - t) * 0.6, 0.5); yb = [nice(W * 0.25, 0.5), nice(W * 0.75, 0.5)]
    body.append(f"part = part.cut(cq.Workplane(\"XY\").pushPoints({pts([(xb, y) for y in yb])})"
                f".circle({f(d / 2)}).extrude({f(t)}))")
    lines.append(f"base: 2X Ø{f(d)} thru at x = {f(xb)}, y = {', '.join(f(y) for y in yb)}")
    zu = nice(t + (H - t) * 0.6, 0.5)
    d2 = r.choice([x for x in DRILLS if x < min(W, H - t) / 4] or [3.4])
    body.append(f"part = part.cut(cq.Workplane(\"YZ\").pushPoints({pts([(y, zu) for y in yb])})"
                f".circle({f(d2 / 2)}).extrude({f(t)}))")
    lines.append(f"upright: 2X Ø{f(d2)} thru at y = {', '.join(f(y) for y in yb)}, z = {f(zu)}")
    body.append("return part")
    return lines, body


# ------------------------------------------------------------------ turned parts

def revolve_x(profile, R):
    """body lines: a profile of (x, radius) points spun around the x axis, then moved so it lies in y, z >= 0"""
    return [f"profile = {pts(profile)}  # (x, radius)",
            "part = cq.Workplane(\"XZ\").polyline(profile).close().revolve(360, (0, 0, 0), (1, 0, 0))",
            f"part = part.translate((0, {f(R)}, {f(R)}))"]


def shaft(r):
    n = r.randint(2, 4)
    segs = []                                                        # (length, diameter)
    big = nice(r.uniform(16, 60), 2)
    for i in range(n):
        dia = big if i == 0 else max(8, nice(segs[-1][1] - r.uniform(3, 12), 2))
        segs.append((nice(r.uniform(12, 70), 1), dia))
    if r.random() < 0.5:
        segs.reverse()                                               # the biggest step may be at either end
    R = max(d for _, d in segs) / 2
    c = nice(min(segs[0][1], segs[-1][1]) * r.choice([0.04, 0.06, 0.08]), 0.5)
    c = max(c, 0.5)
    prof, x = [(0, 0)], 0.0
    lines = ["shaft along x: " + ", ".join(f"Ø{f(d)} x {f(l)}" for l, d in segs)]
    for i, (l, d) in enumerate(segs):
        rr = d / 2
        if i == 0:
            prof += [(0, rr - c), (c, rr)]
        else:
            prof.append((x, rr))
        x += l
        if i == len(segs) - 1:
            prof += [(x - c, rr), (x, rr - c)]
        else:
            prof.append((x, rr))
    prof.append((x, 0))
    lines.append(f"both ends chamfered {f(c)} x 45°")
    body = revolve_x(prof, R)
    L = x
    i = max(range(n), key=lambda j: segs[j][0])                      # features go on the longest step
    x0 = sum(l for l, _ in segs[:i]); l, d = segs[i]
    if r.random() < 0.55 and l >= 25 and d >= 12:
        kw = {12: 4, 17: 5, 22: 6, 30: 8, 38: 10, 44: 12, 50: 14, 58: 16}
        w = next(v for k, v in sorted(kw.items(), reverse=True) if d >= k) if d >= 12 else 4
        kd = nice(w * 0.6, 0.5); kl = nice(l * r.uniform(0.5, 0.75), 1); kx = nice(x0 + (l - kl) / 2, 0.5)
        body.append(f"key = cq.Workplane(\"XY\").center({f(kx + kl / 2)}, {f(R)}).slot2D({f(kl)}, {f(w)}).extrude({f(kd)})")
        body.append(f"part = part.cut(key.translate((0, 0, {f(R + d / 2 - kd)})))")
        lines.append(f"keyway {f(w)} wide x {f(kd)} deep x {f(kl)} long on the Ø{f(d)} step, from x = {f(kx)} "
                     "(top of the step)")
    elif r.random() < 0.6 and l >= 15:
        gw = nice(r.choice([1.5, 2, 3]), 0.5); gd = nice(min(d * 0.08, 2.5), 0.1) or 1
        gx = nice(x0 + l * r.uniform(0.3, 0.7), 0.5)
        body.append(f"groove = cq.Workplane(\"YZ\").workplane(offset={f(gx)}).center({f(R)}, {f(R)})"
                    f".circle({f(d / 2 + 1)}).circle({f(d / 2 - gd)}).extrude({f(gw)})")
        body.append("part = part.cut(groove)")
        lines.append(f"groove {f(gw)} wide, to Ø{f(d - 2 * gd)}, at x = {f(gx)} on the Ø{f(d)} step")
    j = min((q for q in range(n) if q != i), key=lambda q: -segs[q][0], default=None)
    if j is not None:
        xj = sum(l_ for l_, _ in segs[:j]); lj, dj = segs[j]
        if r.random() < 0.4 and lj >= 10:                            # a cross hole through another step
            ch = nice(dj * r.uniform(0.2, 0.35), 0.5); cx = nice(xj + lj / 2, 0.5)
            body.append(f"part = part.cut(cq.Workplane(\"XY\").center({f(cx)}, {f(R)}).circle({f(ch / 2)}).extrude({f(2 * R)}))")
            lines.append(f"cross hole Ø{f(ch)} thru along z at x = {f(cx)} (on the Ø{f(dj)} step)")
        elif r.random() < 0.35 and lj >= 10:                         # a flat on another step
            fd = max(0.5, nice(dj * 0.1, 0.5))
            body.append(f"part = part.cut(cq.Workplane(\"XY\").box({f(lj)}, {f(2 * R)}, {f(fd)}, centered=False)"
                        f".translate(({f(xj)}, 0, {f(R + dj / 2 - fd)})))")
            lines.append(f"flat {f(fd)} deep on top of the Ø{f(dj)} step, its full length")
    if r.random() < 0.4:
        cd = nice(min(segs[0][1], segs[-1][1]) * 0.3, 0.5); cdep = nice(cd * 2, 1)
        body.append(f"part = part.cut(cq.Workplane(\"YZ\").center({f(R)}, {f(R)}).circle({f(cd / 2)}).extrude({f(cdep)}))")
        lines.append(f"blind bore Ø{f(cd)} x {f(cdep)} deep in the x = 0 end")
    body.append("return part")
    return lines, body


def bushing(r):
    D = nice(r.uniform(16, 80), 2); bore = nice(D * r.uniform(0.35, 0.65), 1); L = nice(r.uniform(12, 80), 1)
    flanged = r.random() < 0.6
    Fd = nice(D * r.uniform(1.3, 1.8), 2) if flanged else D
    Ft = nice(r.uniform(3, max(4, L * 0.3)), 1) if flanged else 0
    R = Fd / 2
    c = max(0.5, nice(D * 0.03, 0.5))
    if flanged:
        prof = [(0, bore / 2 + c), (c, bore / 2), (L, bore / 2), (L, D / 2 - c), (L - c, D / 2), (Ft, D / 2),
                (Ft, Fd / 2), (0, Fd / 2)]
        lines = [f"flanged bushing along x: body Ø{f(D)} x {f(L)} overall, flange Ø{f(Fd)} x {f(Ft)} at x = 0"]
    else:
        prof = [(0, bore / 2 + c), (c, bore / 2), (L - c, bore / 2), (L, bore / 2 + c), (L, D / 2 - c),
                (L - c, D / 2), (c, D / 2), (0, D / 2 - c)]
        lines = [f"plain bushing along x: Ø{f(D)} x {f(L)}"]
    lines.append(f"bore Ø{f(bore)} thru; chamfers {f(c)} x 45°")
    body = revolve_x(prof, R)
    if r.random() < 0.45 and L > 20 and D - bore > 8:                # an internal groove
        gw = nice(r.choice([2, 3, 4]), 1); gdia = nice(bore + min(4, (D - bore) / 3), 0.5)
        gx = nice(L * r.uniform(0.35, 0.65), 0.5)
        body.append(f"part = part.cut(cq.Workplane(\"YZ\").workplane(offset={f(gx)}).center({f(R)}, {f(R)})"
                    f".circle({f(gdia / 2)}).extrude({f(gw)}))")
        lines.append(f"internal groove Ø{f(gdia)} x {f(gw)} wide at x = {f(gx)}")
    elif r.random() < 0.4 and D - bore > 6:                          # a counterbore at the far end
        cbd = nice(bore + (D - bore) / 2, 0.5); cbl = nice(L * r.uniform(0.15, 0.35), 0.5)
        body.append(f"part = part.cut(cq.Workplane(\"YZ\").workplane(offset={f(L - cbl)}).center({f(R)}, {f(R)})"
                    f".circle({f(cbd / 2)}).extrude({f(cbl)}))")
        lines.append(f"counterbore Ø{f(cbd)} x {f(cbl)} deep at the x = {f(L)} end")
    if flanged and r.random() < 0.6 and Fd - D > 10:
        n = r.choice([3, 4, 6]); h = r.choice([x for x in DRILLS if x < (Fd - D) / 2.6] or [3.4])
        bc = nice((Fd + D) / 2, 1)
        body.append(f"holes = cq.Workplane(\"YZ\").center({f(R)}, {f(R)}).polarArray({f(bc / 2)}, 0, 360, {n})"
                    f".circle({f(h / 2)}).extrude({f(Ft)})")
        body.append("part = part.cut(holes)")
        lines.append(f"flange: {n}X Ø{f(h)} thru on a Ø{f(bc)} bolt circle")
    body.append("return part")
    return lines, body


def flange(r):
    D = nice(r.uniform(50, 220), 5); T = nice(r.uniform(6, 25), 1); bore = nice(D * r.uniform(0.2, 0.45), 1)
    R = D / 2
    lines = [f"flange: disc Ø{f(D)} x {f(T)} thick, axis along z"]
    body = [f"part = cq.Workplane(\"XY\").center({f(R)}, {f(R)}).circle({f(R)}).extrude({f(T)})"]
    hub = r.random() < 0.55
    if hub:
        hd = nice(bore + r.uniform(10, D * 0.3), 2); hh = nice(r.uniform(5, 30), 1)
        body.append(f"part = part.union(cq.Workplane(\"XY\").workplane(offset={f(T)}).center({f(R)}, {f(R)})"
                    f".circle({f(hd / 2)}).extrude({f(hh)}))")
        lines.append(f"hub Ø{f(hd)} x {f(hh)} high on the top face")
        top = T + hh
    else:
        hd, top = bore, T
    body.append(f"part = part.cut(cq.Workplane(\"XY\").center({f(R)}, {f(R)}).circle({f(bore / 2)}).extrude({f(top)}))")
    lines.append(f"bore Ø{f(bore)} thru")
    n = r.choice([3, 4, 4, 6, 6, 8, 12])
    inner = hd if hub else bore
    h = r.choice([x for x in DRILLS if x < (D - inner) / 5] or [3.4])
    bc = nice((D + inner) / 2, 1)
    body.append(f"holes = cq.Workplane(\"XY\").center({f(R)}, {f(R)}).polarArray({f(bc / 2)}, 0, 360, {n})"
                f".circle({f(h / 2)}).extrude({f(T)})")
    body.append("part = part.cut(holes)")
    lines.append(f"{n}X Ø{f(h)} thru, equally spaced on a Ø{f(bc)} bolt circle, the first on the +x side")
    if r.random() < 0.4 and CBORE[h][1] < T - 2 and CBORE[h][0] < (D - inner) / 2.5:
        cd, cdep = CBORE[h]
        body.append(f"part = part.cut(cq.Workplane(\"XY\").workplane(offset={f(T - cdep)}).center({f(R)}, {f(R)})"
                    f".polarArray({f(bc / 2)}, 0, 360, {n}).circle({f(cd / 2)}).extrude({f(cdep)}))")
        lines.append(f"each bolt hole counterbored Ø{f(cd)} x {f(cdep)} deep from the top face")
    if r.random() < 0.35 and T > 8:                                  # a recess in the bottom face
        rd = nice((bc - h) * r.uniform(0.6, 0.85), 1); rdep = nice(r.uniform(1, min(4, T * 0.25)), 0.5)
        if rd > inner + 4:
            body.append(f"part = part.cut(cq.Workplane(\"XY\").center({f(R)}, {f(R)}).circle({f(rd / 2)}).extrude({f(rdep)}))")
            lines.append(f"recess Ø{f(rd)} x {f(rdep)} deep in the bottom face")
    if r.random() < 0.4:
        c = nice(min(T * 0.15, 2), 0.5) or 0.5
        body.insert(1, f"part = part.edges(\"%CIRCLE\").edges(\">Z\").chamfer({f(c)})")
        lines.append(f"top outer edge chamfered {f(c)} x 45°")
    body.append("return part")
    return lines, body


def housing(r):
    """a block with several features: a pocket, corner holes, holes in the front face, a channel underneath"""
    L = nice(r.uniform(60, 200), 5); W = nice(r.uniform(40, 120), 5); H = nice(r.uniform(20, 70), 1)
    lines = [f"block {f(L)} x {f(W)} x {f(H)} (x, y, z)"]
    body = [f"part = cq.Workplane(\"XY\").box({f(L)}, {f(W)}, {f(H)}, centered=False)"]
    if r.random() < 0.5:
        rad = nice(r.uniform(3, min(L, W) / 8), 1)
        body.append(f"part = part.edges(\"|Z\").fillet({f(rad)})")
        lines.append(f"vertical corners rounded R{f(rad)}")
    if r.random() < 0.4:
        c = nice(r.uniform(1, 3), 0.5)
        body.append(f"part = part.faces(\">Z\").edges().chamfer({f(c)})")
        lines.append(f"top edges chamfered {f(c)} x 45°")
    m = max(8, nice(min(L, W) * 0.12, 1))
    pw, ph = nice(L - 4 * m, 1), nice(W - 4 * m, 1)
    dep = nice(H * r.uniform(0.3, 0.6), 0.5)
    if pw > 10 and ph > 10:
        pr = max(1, nice(min(pw, ph) * r.uniform(0.05, 0.2), 1))
        body.append(f"pocket = cq.Workplane(\"XY\").box({f(pw)}, {f(ph)}, {f(dep)}, centered=False)"
                    f".edges(\"|Z\").fillet({f(pr)}).translate(({f(2 * m)}, {f(2 * m)}, {f(H - dep)}))")
        body.append("part = part.cut(pocket)")
        lines.append(f"pocket {f(pw)} x {f(ph)} x {f(dep)} deep from the top, corners R{f(pr)}, from ({f(2 * m)}, {f(2 * m)})")
        if r.random() < 0.5:
            d0 = r.choice([x for x in DRILLS if x < min(pw, ph) / 4] or [3.4])
            cx, cy = nice(L / 2, 0.5), nice(W / 2, 0.5)
            body.append(f"part = part.cut(cq.Workplane(\"XY\").center({f(cx)}, {f(cy)}).circle({f(d0 / 2)}).extrude({f(H)}))")
            lines.append(f"Ø{f(d0)} thru in the pocket floor at ({f(cx)}, {f(cy)})")
    d = r.choice([x for x in DRILLS if x < m * 0.9] or [3.4])
    hm = nice(m, 0.5)
    ps = [(hm, hm), (L - hm, hm), (hm, W - hm), (L - hm, W - hm)]
    hb, ht = holes_code(r, H, ps, d, True, H)
    body += hb
    lines.append(f"holes {ht}, at the corners, {f(hm)} in from the edges")
    if r.random() < 0.7:                                             # holes in the front face
        n = r.choice([1, 2])
        ds = r.choice([x for x in DRILLS if x < H / 3] or [3.4])
        sdep = nice(min(W * 0.6, r.uniform(10, 40)), 1)
        zs = nice(H / 2 if r.random() < 0.5 else H * 0.4, 0.5)
        xs = [nice(L / 2, 0.5)] if n == 1 else [nice(L * 0.3, 0.5), nice(L * 0.7, 0.5)]
        body.append(f"part = part.cut(cq.Workplane(\"XZ\").pushPoints({pts([(x, zs) for x in xs])})"
                    f".circle({f(ds / 2)}).extrude(-{f(sdep)}))")
        lines.append(f"{n}X Ø{f(ds)} x {f(sdep)} deep into the front face (y = 0) at z = {f(zs)}, "
                     f"x = {', '.join(f(x) for x in xs)}")
    if r.random() < 0.5:                                             # a channel along x underneath
        cw = nice(W * r.uniform(0.2, 0.4), 1); ch = nice(min(H * 0.3, r.uniform(3, 12)), 0.5)
        cy0 = nice((W - cw) / 2, 0.5)
        body.append(f"part = part.cut(cq.Workplane(\"XY\").box({f(L)}, {f(cw)}, {f(ch)}, centered=False)"
                    f".translate((0, {f(cy0)}, 0)))")
        lines.append(f"channel {f(cw)} wide x {f(ch)} deep along x on the bottom, from y = {f(cy0)}")
    body.append("return part")
    return lines, body


MAKERS = dict(shaft=shaft, bushing=bushing, flange=flange, housing=housing, plate=plate, bracket=bracket, block=block)


# ------------------------------------------------------------------ build and check

BUILD = r'''
import json, sys, warnings
warnings.filterwarnings("ignore")
import cadquery as cq
src = open(sys.argv[1], encoding="utf-8").read()
code = src.split("```python", 1)[1].split("```", 1)[0]
ns = {}
exec(code, ns)
s = ns["build"]().val()
bb = s.BoundingBox()
cq.exporters.export(cq.Workplane().add(s), sys.argv[2])
print("RESULT " + json.dumps(dict(solids=len(s.Solids()), valid=s.isValid(), volume=s.Volume(),
                                  min=[bb.xmin, bb.ymin, bb.zmin], size=[bb.xlen, bb.ylen, bb.zlen])))
'''


def make_one(out, idx, seed):
    """one part: returns its record, or None when the random part did not build cleanly"""
    r = random.Random(seed)
    fam = r.choices([n for n, _ in FAMILIES], [w for _, w in FAMILIES])[0]
    lines, body = MAKERS[fam](r)
    answer = code_of(lines, body)
    pid = f"{fam}_{idx:05d}"
    d = os.path.join(out, "parts", pid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "answer.py"), "w", encoding="utf-8").write(answer)
    line, err = None, "timeout"
    with tempfile.TemporaryDirectory() as td:
        runner = os.path.join(td, "build.py")
        open(runner, "w").write(BUILD)
        try:
            p = subprocess.run([sys.executable, runner, os.path.join(d, "answer.py"), os.path.join(d, "model.step")],
                               capture_output=True, text=True, timeout=120)
            line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
            err = (p.stderr or p.stdout)[-600:]
        except subprocess.TimeoutExpired:
            pass
    res = json.loads(line[7:]) if line else dict(error=err)
    ok = (line is not None and res["solids"] == 1 and res["valid"] and res["volume"] > 0
          and all(abs(m) < 0.01 for m in res["min"]))               # one valid solid, lowest corner at the origin
    rec = dict(id=pid, family=fam, seed=seed, ok=ok, **({k: res[k] for k in ("size", "volume")} if line else res))
    if not ok and line:
        rec["why"] = "not one valid solid at the origin"
    json.dump(rec, open(os.path.join(d, "meta.json"), "w"), indent=1)
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out"); ap.add_argument("n", type=int)
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "parts"), exist_ok=True)
    recs = []
    with ProcessPoolExecutor(a.workers) as ex:
        futs = [ex.submit(make_one, a.out, i, a.seed * 1_000_003 + i) for i in range(a.n)]
        for k, fu in enumerate(as_completed(futs), 1):
            recs.append(fu.result())
            if k % 25 == 0 or k == a.n:
                print(f"{k}/{a.n} built, {sum(r['ok'] for r in recs)} ok", flush=True)
    recs.sort(key=lambda r: r["id"])
    with open(os.path.join(a.out, "parts.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    bad = [r for r in recs if not r["ok"]]
    print(f"{len(recs) - len(bad)} of {len(recs)} parts ok" + (f"; failed: {', '.join(r['id'] for r in bad[:10])}" if bad else ""))


if __name__ == "__main__":
    main()
