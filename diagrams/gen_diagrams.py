#!/usr/bin/env python3
"""
gen_diagrams.py — Masari SE submission diagrams (draw.io + SVG + PNG).

v2 — ROUTED EDITION:
Every arrow between boxes is computed by an obstacle-avoiding grid router
(turn-penalized A* + simplification), then verified geometrically: a build
FAILS if any arrow segment penetrates any box. No hand-placed waypoints.

Outputs per diagram: NN_name.drawio (editable in diagrams.net),
NN_name.svg, NN_name.png (Chrome screenshot).
"""

from __future__ import annotations

import heapq
import html
import subprocess
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).resolve().parent


# ================================================================ model ====
class Box:
    def __init__(
        self,
        x,
        y,
        w,
        h,
        label="",
        *,
        kind="rect",
        dashed=False,
        fill="#dae8fc",
        stroke="#6c8ebf",
        font="#1a1a1a",
        rows=None,
        header=None,
        rounded=True,
        fs=13,
        frame=False,
    ):
        if rows is not None:
            # real height from content — the box body must be VISIBLE and the
            # router must treat it as a solid obstacle (h=0 was the root bug)
            h = max(h, 30 + len(rows) * 22 + 12)
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label, self.kind = label, kind
        self.dashed, self.fill, self.stroke, self.font = dashed, fill, stroke, font
        self.rows, self.header, self.rounded, self.fs = rows, header, rounded, fs
        self.frame = frame  # frames: drawn, but never obstacle/port


class Edge:
    """Explicit polyline (sequence-diagram lifelines/messages, stubs)."""

    def __init__(
        self,
        *points,
        label="",
        dashed=False,
        end="block",
        start="none",
        color="#595959",
        fs=12,
        lofs=(0, -8),
        lside="auto",
    ):
        pts = list(points)
        if pts and isinstance(pts[0], (int, float)):
            pts = [(pts[i], pts[i + 1]) for i in range(0, len(pts) - 1, 2)]
        self.points = pts
        self.label, self.dashed = label, dashed
        self.end, self.start, self.color, self.fs = end, start, color, fs
        self.lofs = lofs
        self.lside = lside  # auto | above | below | side


class Link:
    """Routed arrow between two Boxes (auto port-picking + spreading)."""

    def __init__(
        self,
        src,
        dst,
        label="",
        *,
        dashed=False,
        end="block",
        start="none",
        color="#595959",
        fs=12,
        src_side=None,
        dst_side=None,
        src_t=None,
        dst_t=None,
        lside="auto",
    ):
        self.src, self.dst = src, dst
        self.label, self.dashed = label, dashed
        self.end, self.start, self.color, self.fs = end, start, color, fs
        self.src_side, self.dst_side = src_side, dst_side
        self.src_t, self.dst_t = src_t, dst_t
        self.lside = lside


class Text:
    def __init__(self, x, y, s, *, fs=12, bold=False, color="#333", anchor="middle"):
        self.x, self.y, self.s, self.fs, self.bold = x, y, s, fs, bold
        self.color, self.anchor = color, anchor


BLUE = dict(fill="#dae8fc", stroke="#6c8ebf")
GREEN = dict(fill="#d5e8d4", stroke="#82b366")
ORANGE = dict(fill="#ffe6cc", stroke="#d79b00")
YELLOW = dict(fill="#fff2cc", stroke="#d6b656")
PURPLE = dict(fill="#e1d5e7", stroke="#9673a6")
GRAY = dict(fill="#f5f5f5", stroke="#666666")
RED = dict(fill="#f8cecc", stroke="#b85450")


def xml_escape(s):
    return html.escape(s, quote=True)


_ARABIC_RANGE = ((0x0590, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF), (0x0600, 0x06FF))


def _first_strong_is_rtl(s: str) -> bool:
    for ch in s:
        cp = ord(ch)
        for lo, hi in _ARABIC_RANGE:
            if lo <= cp <= hi:
                return True
        if ch.isalpha():
            return False
    return False


def bidi_attrs(s: str) -> str:
    """SVG text direction attr so Arabic/Latin mixed strings order correctly."""
    return (
        ' direction="rtl" unicode-bidi="plaintext"' if _first_strong_is_rtl(s) else ""
    )


# ============================================================ geometry =====
MARGIN = 8  # obstacle inflation around boxes
CELL = 10  # router grid cell


def seg_rect_hit(p1, p2, r, pad=0.0):
    """True if segment p1-p2 intersects rect r (optionally inflated)."""
    rx, ry, rw, rh = r
    rx -= pad
    ry -= pad
    rw += 2 * pad
    rh += 2 * pad
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x1 - rx),
        (dx, rx + rw - x1),
        (-dy, y1 - ry),
        (dy, ry + rh - y1),
    ):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def port_point(b, side, t):
    if side == "right":
        return (b.x + b.w, b.y + t * b.h)
    if side == "left":
        return (b.x, b.y + t * b.h)
    if side == "top":
        return (b.x + t * b.w, b.y)
    return (b.x + t * b.w, b.y + b.h)


def outward(side):
    return {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]


def facing_sides(sb, db):
    if db.x + db.w < sb.x + 60:
        return "left", "right"
    if sb.x + sb.w + 60 < db.x:
        return "right", "left"
    if db.y + db.h < sb.y + 40:
        return "top", "bottom"
    if sb.y + sb.h + 40 < db.y:
        return "bottom", "top"
    return "right", "left"


# --------------------------------------------------------------- router ----
DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
TURN_PENALTY = 4


def route_link(link, obstacles, bounds):
    """Compute an obstacle-avoiding polyline for one Link. Returns points."""
    sb, db = link.src, link.dst
    ss = link.src_side or facing_sides(sb, db)[0]
    ds = link.dst_side or facing_sides(sb, db)[1]
    sp = port_point(sb, ss, link.src_t if link.src_t is not None else 0.5)
    dp = port_point(db, ds, link.dst_t if link.dst_t is not None else 0.5)

    W, H = bounds
    so, do_ = outward(ss), outward(ds)
    push = MARGIN + CELL
    start = (sp[0] + so[0] * push, sp[1] + so[1] * push)
    goal = (dp[0] + do_[0] * push, dp[1] + do_[1] * push)

    def blocked(x, y):
        if x < 2 or y < 2 or x > W - 2 or y > H - 2:
            return True
        for r in obstacles:
            if (
                r[0] - MARGIN <= x <= r[0] + r[2] + MARGIN
                and r[1] - MARGIN <= y <= r[1] + r[3] + MARGIN
            ):
                return True
        return False

    def cell_center(cx, cy):
        return (cx * CELL + CELL / 2, cy * CELL + CELL / 2)

    sc = (int(start[0] // CELL), int(start[1] // CELL))
    gc = (int(goal[0] // CELL), int(goal[1] // CELL))
    for c in (sc, gc):  # never block the endpoints
        pass

    # A* with turn penalty over 4-dir grid
    startstate: tuple[int, int, int] = (sc[0], sc[1], -1)
    openq: list[tuple[float, int, tuple[int, int, int]]] = [(0.0, 0, startstate)]
    came: dict[tuple[int, int, int], tuple[int, int, int] | None] = {startstate: None}
    cost: dict[tuple[int, int, int], float] = {startstate: 0.0}
    counter = 1
    goalstate: tuple[int, int, int] | None = None
    while openq:
        f, _, cur = heapq.heappop(openq)
        cx, cy, d = cur
        if (cx, cy) == gc:
            goalstate = cur
            break
        px, py = cell_center(cx, cy)
        for nd, (dx, dy) in enumerate(DIRS):
            ncx, ncy = cx + dx, cy + dy
            npx, npy = cell_center(ncx, ncy)
            if blocked(npx, npy) and (ncx, ncy) != gc:
                continue
            step = TURN_PENALTY if (d != -1 and d != nd) else 1
            ncost = cost[cur] + step
            ns = (ncx, ncy, nd)
            if ncost < cost.get(ns, 1e18):
                cost[ns] = ncost
                came[ns] = cur
                h = abs(ncx - gc[0]) + abs(ncy - gc[1])
                heapq.heappush(openq, (ncost + h, counter, ns))
                counter += 1

    if goalstate is None:
        print(f"    !! no route {sp}->{dp} — straight fallback")
        return [sp, dp]

    # rebuild
    pts = []
    cur: tuple[int, int, int] | None = goalstate
    while cur is not None:
        pts.append(cell_center(cur[0], cur[1]))
        cur = came[cur]
    pts.reverse()
    pts[0] = start
    pts[-1] = goal
    pts = [sp] + pts + [dp]

    # merge collinear
    simp = [pts[0]]
    for p in pts[1:-1]:
        a, b = simp[-1], p
        nxtsame = False
        # keep logic simple: add, collinear-merge after
        simp.append(p)
    simp.append(pts[-1])
    merged = [simp[0]]
    for p in simp[1:]:
        a, b = merged[-1], p
        if len(merged) >= 2:
            a0 = merged[-2]
            if (a0[0] == a[0] == p[0]) or (a0[1] == a[1] == p[1]):
                merged[-1] = p
                continue
        merged.append(p)

    # shortcut removal (never touch first/last two points)
    pts0, ptsN = merged[0], merged[-1]

    def clear(p1, p2):
        length = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
        for r in obstacles:
            # allow tiny port-stub segments hugging their own box;
            # long shortcuts must respect EVERY obstacle
            if length <= 3 * push and (
                _pt_in_rect(p1, r, MARGIN) or _pt_in_rect(p2, r, MARGIN)
            ):
                continue
            if seg_rect_hit(p1, p2, r, MARGIN):
                return False
        return True

    i = 1
    while i < len(merged) - 2:
        if clear(merged[i - 1], merged[i + 1]):
            del merged[i]
        else:
            i += 1
    return merged


def _pt_in_rect(p, r, pad):
    return (
        r[0] - pad <= p[0] <= r[0] + r[2] + pad
        and r[1] - pad <= p[1] <= r[1] + r[3] + pad
    )


# ------------------------------------------------------------ spreading ----
def spread_ports(links):
    """Distribute implicit t values per (box, side)."""
    per = {}
    for l in links:
        for box, side, tkey, skey in (
            (l.src, l.src_side, "src_t", "src_side"),
            (l.dst, l.dst_side, "dst_t", "dst_side"),
        ):
            if side and getattr(l, tkey) is None:
                per.setdefault((id(box), side), []).append((l, tkey))
    for group in per.values():
        n = len(group)
        for i, (l, tkey) in enumerate(group):
            setattr(l, tkey, round((i + 1) / (n + 1), 3))


# ========================================================= SVG rendering ===
def _svg_multiline(x, y, s, fs, bold=False, color="#1a1a1a"):
    out = []
    fw = 'font-weight="bold" ' if bold else ""
    bidi = bidi_attrs(s)
    for i, line in enumerate(s.split("\n")):
        out.append(
            f'<text x="{x}" y="{y + i * (fs + 6)}" font-size="{fs}" '
            f'fill="{color}" text-anchor="middle" '
            f"{fw}{bidi}>{xml_escape(line)}</text>"
        )
    return out


def render_box_svg(b):
    out, cx, cy = [], b.x + b.w / 2, b.y + b.h / 2
    dash = 'stroke-dasharray="7 5" ' if b.dashed else ""
    common = f'fill="{b.fill}" stroke="{b.stroke}" stroke-width="1.6" {dash}'
    if b.kind == "rect":
        r = 10 if b.rounded else 0
        out.append(
            f'<rect x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" rx="{r}" {common}/>'
        )
    elif b.kind == "ellipse":
        out.append(
            f'<ellipse cx="{cx}" cy="{cy}" rx="{b.w / 2}" ry="{b.h / 2}" {common}/>'
        )
    elif b.kind == "cylinder":
        ry = 12
        out.append(
            f'<path d="M {b.x} {b.y + ry} A {b.w / 2} {ry} 0 0 1 {b.x + b.w} {b.y + ry} '
            f'L {b.x + b.w} {b.y + b.h - ry} A {b.w / 2} {ry} 0 0 0 {b.x} {b.y + b.h - ry} Z" {common}/>'
        )
        out.append(
            f'<ellipse cx="{cx}" cy="{b.y + ry}" rx="{b.w / 2}" ry="{ry}" {common}/>'
        )
    elif b.kind == "diamond":
        pts = f"{cx},{b.y} {b.x + b.w},{cy} {cx},{b.y + b.h} {b.x},{cy}"
        out.append(f'<polygon points="{pts}" {common}/>')
    elif b.kind == "actor":
        s = b.stroke
        out.append(
            f'<circle cx="{cx}" cy="{b.y + 14}" r="11" fill="none" stroke="{s}" stroke-width="2.2"/>'
        )
        out.append(
            f'<line x1="{cx}" y1="{b.y + 25}" x2="{cx}" y2="{b.y + 52}" stroke="{s}" stroke-width="2.2"/>'
        )
        out.append(
            f'<line x1="{cx - 20}" y1="{b.y + 36}" x2="{cx + 20}" y2="{b.y + 36}" stroke="{s}" stroke-width="2.2"/>'
        )
        out.append(
            f'<line x1="{cx}" y1="{b.y + 52}" x2="{cx - 16}" y2="{b.y + 74}" stroke="{s}" stroke-width="2.2"/>'
        )
        out.append(
            f'<line x1="{cx}" y1="{b.y + 52}" x2="{cx + 16}" y2="{b.y + 74}" stroke="{s}" stroke-width="2.2"/>'
        )
        if b.label:
            out += _svg_multiline(cx, b.y + 92, b.label, 13, True)
        return out
    if b.rows is not None:
        out.append(
            f'<rect x="{b.x}" y="{b.y}" width="{b.w}" height="30" rx="6" fill="{b.stroke}" opacity="0.28"/>'
        )
        out.append(
            f'<line x1="{b.x}" y1="{b.y + 30}" x2="{b.x + b.w}" y2="{b.y + 30}" stroke="{b.stroke}" stroke-width="1.2"/>'
        )
        out += _svg_multiline(cx, b.y + 20, b.header or "", b.fs + 1, True)
        for i, row in enumerate(b.rows):
            out += _svg_multiline(cx, b.y + 48 + i * 22, row, b.fs - 1)
    elif b.label:
        out += _svg_multiline(cx, cy + b.fs * 0.36, b.label, b.fs, b.kind != "ellipse")
    return out


def render_edge_svg(e, label_pos=None):
    out = []
    dash = 'stroke-dasharray="7 5" ' if e.dashed else ""
    pts = " ".join(f"{x},{y}" for x, y in e.points)
    ae = (
        'marker-end="url(#ar-end)" '
        if e.end == "block"
        else ('marker-end="url(#ar-open)" ' if e.end == "open" else "")
    )
    as_ = 'marker-start="url(#ar-start)" ' if e.start == "block" else ""
    out.append(
        f'<polyline points="{pts}" fill="none" stroke="{e.color}" '
        f'stroke-width="1.7" {dash}{ae}{as_}/>'
    )
    if e.label:
        lx, ly = label_pos or _default_label(e)
        # float the pill clear of its own line (never cover the line)
        pl = e.points
        best, bd = None, 1e18
        for i in range(len(pl) - 1):
            mx = (pl[i][0] + pl[i + 1][0]) / 2
            my = (pl[i][1] + pl[i + 1][1]) / 2
            d = abs(mx - lx) + abs(my - ly)
            if d < bd:
                bd, best = d, (pl[i], pl[i + 1])
        (ax, ay), (bx, by) = best
        horiz = abs(bx - ax) >= abs(by - ay)
        tw = max(len(e.label) * e.fs * 0.62, 34)
        th = e.fs + 8
        if e.lside == "below":
            ly += th - 1
        elif e.lside == "above":
            ly -= 13
        elif e.lside == "side":
            lx += tw / 2 + 10
        elif horiz:  # auto: horizontal -> lift up
            ly -= 13
        else:  # auto: vertical -> side
            lx += tw / 2 + 10
        out.append(
            f'<rect x="{lx - tw / 2}" y="{ly - th + 5}" width="{tw}" height="{th}" '
            f'fill="#ffffff" fill-opacity="0.94" rx="4" stroke="#d8d8d8" '
            f'stroke-width="0.7"/>'
        )
        out.append(
            f'<text x="{lx}" y="{ly}" font-size="{e.fs}" fill="#333" '
            f'text-anchor="middle"{bidi_attrs(e.label)} '
            f'font-style="italic">{xml_escape(e.label)}</text>'
        )
    return out


def _default_label(e):
    pl = e.points
    segs = [(pl[i], pl[i + 1]) for i in range(len(pl) - 1)]
    total = sum(max(abs(b[0] - a[0]), abs(b[1] - a[1])) for a, b in segs) or 1
    target, acc = total / 2, 0
    lx, ly = pl[0]
    for a, b in segs:
        seglen = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        if acc + seglen >= target:
            t = (target - acc) / (seglen or 1)
            lx, ly = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            break
        acc += seglen
    return lx + e.lofs[0], ly + e.lofs[1]


def label_pos_clear(e, rects):
    """Pick a clear spot along the longest segment for the label."""
    pl = e.points
    segs = [(pl[i], pl[i + 1]) for i in range(len(pl) - 1)]
    segs.sort(
        key=lambda s: max(abs(s[1][0] - s[0][0]), abs(s[1][1] - s[0][1])), reverse=True
    )
    w = max(len(e.label) * 6.5, 40)
    h = 16
    for a, b in segs[:4]:
        for t in (0.5, 0.35, 0.65, 0.25, 0.75):
            lx = a[0] + (b[0] - a[0]) * t
            ly = a[1] + (b[1] - a[1]) * t
            r = (lx - w / 2, ly - h / 2, w, h)
            if not any(
                seg_rect_hit((r[0], r[1]), (r[0] + r[2], r[1] + r[3]), rr, 2)
                for rr in rects
            ):
                return (lx, ly + 4)
    d = _default_label(e)
    return (d[0], d[1] + 4)


def render_svg(name, w, h, boxes, edges, texts, label_rects_map):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Cairo, Segoe UI, Amiri, sans-serif">',
        "<defs>"
        '<marker id="ar-end" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#595959"/></marker>'
        '<marker id="ar-open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M1,1 L9,5 L1,9" fill="none" stroke="#595959" stroke-width="1.6"/></marker>'
        '<marker id="ar-start" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M10,0 L0,5 L10,10 z" fill="#595959"/></marker>'
        "</defs>",
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
    ]
    for b in boxes:
        parts += render_box_svg(b)
    for e in edges:
        lp = label_rects_map.get(id(e))
        parts += render_edge_svg(e, lp)
    for t in texts:
        fw = 'font-weight="bold" ' if t.bold else ""
        parts.append(
            f'<text x="{t.x}" y="{t.y}" font-size="{t.fs}" fill="{t.color}" '
            f'text-anchor="{t.anchor}" {fw}{bidi_attrs(t.s)}>'
            f"{xml_escape(t.s)}</text>"
        )
    parts.append("</svg>")
    (OUT / f"{name}.svg").write_text("\n".join(parts), encoding="utf-8")


# ===================================================== draw.io export =====
DIO_STYLE = {
    "rect": "rounded={r};whiteSpace=wrap;html=1;",
    "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    "cylinder": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=14;",
    "diamond": "rhombus;whiteSpace=wrap;html=1;",
    "actor": "shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;outlineConnect=0;",
}


def render_dio(name, page_w, page_h, boxes, edges, texts):
    cells = []
    cid = [1]

    def nid():
        cid[0] += 1
        return f"n{cid[0]}"

    for b in boxes:
        i = nid()
        if b.kind == "actor":
            style = DIO_STYLE["actor"] + f"strokeColor={b.stroke};"
            label = b.label
        else:
            style = DIO_STYLE.get(b.kind, DIO_STYLE["rect"]).replace(
                "{r}", "1" if (b.rounded and b.kind == "rect") else "0"
            )
            style += f"fillColor={b.fill};strokeColor={b.stroke};fontFamily=Cairo;fontSize={b.fs};"
            if b.dashed:
                style += "dashed=1;"
            if b.rows is not None:
                body = "<br>".join(xml_escape(r) for r in b.rows)
                label = (
                    f"<b>{xml_escape(b.header or '')}</b><hr size='1'/>"
                    f"<div style='line-height:1.35'>{body}</div>"
                )
                style += "verticalAlign=top;align=center;"
            else:
                label = b.label.replace("\n", "<br>")
        cells.append(
            f'<mxCell id="{i}" value="{label}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" as="geometry"/></mxCell>'
        )

    for e in edges:
        i = nid()
        style = (
            "edgeStyle=none;html=1;rounded=0;fontFamily=Cairo;"
            f"fontSize={e.fs};fontStyle=2;strokeColor={e.color};"
            + (
                ""
                if e.end == "none"
                else f"endArrow={e.end};endFill={'1' if e.end == 'block' else '0'};"
            )
            + ("" if e.start == "none" else f"startArrow={e.start};startFill=1;")
            + ("dashed=1;" if e.dashed else "")
        )
        src, tgt = e.points[0], e.points[-1]
        wps = "".join(
            f'<mxPoint x="{int(x)}" y="{int(y)}"/>' for x, y in e.points[1:-1]
        )
        cells.append(
            f'<mxCell id="{i}" value="{xml_escape(e.label)}" style="{style}" edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{int(src[0])}" y="{int(src[1])}" as="sourcePoint"/>'
            f'<mxPoint x="{int(tgt[0])}" y="{int(tgt[1])}" as="targetPoint"/>'
            + (f'<Array as="points">{wps}</Array>' if wps else "")
            + "</mxGeometry></mxCell>"
        )

    for t in texts:
        i = nid()
        cells.append(
            f'<mxCell id="{i}" value="{xml_escape(t.s)}" style="text;html=1;align=center;'
            f"fontFamily=Cairo;fontSize={t.fs};{'fontStyle=1;' if t.bold else ''}"
            f'fillColor=none;strokeColor=none;" vertex="1" parent="1">'
            f'<mxGeometry x="{t.x - 160}" y="{t.y - 20}" width="320" height="30" as="geometry"/></mxCell>'
        )

    xml = (
        '<mxfile host="app.diagrams.net" type="device">'
        f'<diagram id="{name}" name="{name}">'
        f'<mxGraphModel dx="1000" dy="700" grid="1" gridSize="10" guides="1" tooltips="1" '
        f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{page_w}" '
        f'pageHeight="{page_h}" math="0" shadow="0">'
        '<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        + "".join(cells)
        + "</root></mxGraphModel></diagram></mxfile>"
    )
    (OUT / f"{name}.drawio").write_text(xml, encoding="utf-8")


# ================================================== pipeline + verify =====
def emit(name, w, h, boxes, links=(), raw_edges=(), texts=()):
    spread_ports(links)
    obstacles = [(b.x, b.y, b.w, b.h) for b in boxes if not b.frame]
    # ---- box-overlap check: two solid shapes may never intersect ----
    overlap_errors = []
    solids = [b for b in boxes if not b.frame]
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            a, c = solids[i], solids[j]
            if (
                a.x < c.x + c.w - 2
                and c.x < a.x + a.w - 2
                and a.y < c.y + c.h - 2
                and c.y < a.y + a.h - 2
            ):
                overlap_errors.append(
                    (a.label or a.header or "?", c.label or c.header or "?")
                )
    edges = list(raw_edges)
    for l in links:
        pts = route_link(l, obstacles, (w, h))
        edges.append(
            Edge(
                *pts,
                label=l.label,
                dashed=l.dashed,
                end=l.end,
                start=l.start,
                color=l.color,
                fs=l.fs,
                lside=l.lside,
            )
        )
    # ---- geometric verification: no arrow may penetrate a box ----
    violations = []
    for e in edges:
        for i in range(len(e.points) - 1):
            p1, p2 = e.points[i], e.points[i + 1]
            for b in boxes:
                if b.frame:
                    continue
                if seg_rect_hit(p1, p2, (b.x, b.y, b.w, b.h), pad=-2.5):
                    violations.append((name, e.label or "?", b.label or b.header, i))
    # label positions
    lr = {}
    for e in edges:
        if e.label:
            lr[id(e)] = label_pos_clear(e, obstacles)
    render_svg(name, w, h, boxes, edges, texts, lr)
    render_dio(name, w, h, boxes, edges, texts)
    ok = True
    if overlap_errors:
        ok = False
        print(f"  {name}: BOX OVERLAPS:")
        for a, c in overlap_errors:
            print(f"      '{a}' overlaps '{c}'")
    if violations:
        ok = False
        print(f"  {name}: arrow violations x{len(violations)}:")
        for v in violations:
            print(f"      edge '{v[1]}' seg{v[3]} penetrates box '{v[2]}'")
    if ok:
        print(f"  {name}: {len(boxes)} boxes, {len(edges)} edges -> OK")
    return ok


def to_png(name):
    svg = (OUT / f"{name}.svg").resolve()
    png = (OUT / f"{name}.png").resolve()
    import re as _re

    m = _re.search(r'width="(\d+)" height="(\d+)"', svg.read_text(encoding="utf-8"))
    w, h = int(m.group(1)), int(m.group(2))
    subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={w},{h}",
            f"--screenshot={png}",
            svg.as_uri(),
        ],
        capture_output=True,
        timeout=90,
        check=False,
    )
    ok = png.exists() and png.stat().st_size > 8000
    print(f"  {name}.png {'OK' if ok else 'FAIL'} ({png.stat().st_size // 1024}KB)")
    return ok


# =============================================================== figures ===
def d01_methodology():
    boxes, links = [], []
    P = {
        "plan": (810, 100),
        "design": (465, 100),
        "code": (120, 100),
        "test": (120, 380),
        "review": (465, 380),
        "feedback": (810, 380),
    }
    L = {
        "plan": "① التخطيط والتحليل",
        "design": "② التصميم",
        "code": "③ التنفيذ",
        "test": "④ الاختبار",
        "review": "⑤ المراجعة والدمج",
        "feedback": "⑥ التغذية الراجعة",
    }
    F = {
        "plan": BLUE,
        "design": GREEN,
        "code": ORANGE,
        "test": YELLOW,
        "review": PURPLE,
        "feedback": GRAY,
    }
    W, H = 230, 78
    for k, (x, y) in P.items():
        boxes.append(Box(x, y, W, H, L[k], **F[k]))
    links += [
        Link(boxes[0], boxes[1], src_side="left", dst_side="right"),
        Link(boxes[1], boxes[2], src_side="left", dst_side="right"),
        Link(boxes[2], boxes[3], src_side="bottom", dst_side="top"),
        Link(boxes[3], boxes[4], src_side="right", dst_side="left"),
        Link(boxes[4], boxes[5], src_side="right", dst_side="left"),
        Link(
            boxes[5],
            boxes[0],
            "تكرار Sprint جديد",
            color="#7a4f00",
            src_side="top",
            dst_side="bottom",
        ),
    ]
    texts = [
        Text(
            625,
            540,
            "التكرارات الأربعة المُنفَّذة: ① الخريطة + الخوارزميات ② الخادم و REST "
            "③ العميل التفاعلي ④ NLU + الأمان + UX",
            fs=13,
            bold=True,
        )
    ]
    return emit("01_methodology", 1250, 580, boxes, links, texts=texts)


def d02_usecase():
    boxes, links = [], []
    boxes.append(
        Box(
            190,
            60,
            1000,
            790,
            "",
            rounded=False,
            fill="#fcfcfc",
            stroke="#999",
            dashed=True,
            frame=True,
        )
    )
    texts = [
        Text(
            690, 92, "نظام مساري — Smart Navigation", fs=16, bold=True, color="#1f4e79"
        )
    ]
    user = Box(1230, 320, 90, 120, "مستخدم\nعادي", kind="actor", stroke="#1f4e79")
    admin = Box(60, 640, 90, 120, "مشرف\nAdmin", kind="actor", stroke="#7a4f00")
    boxes += [user, admin]
    texts.append(Text(1275, 470, "الضيف = مستخدم قبل الدخول", fs=11, color="#666"))
    UC = dict(kind="ellipse", fs=12.5)
    uc = {
        "login": Box(880, 120, 240, 74, "تسجيل الدخول / حساب جديد", **GREEN, **UC),
        "map": Box(880, 240, 240, 74, "عرض الخريطة التفاعلية", **BLUE, **UC),
        "route": Box(880, 380, 240, 74, "تخطيط المسار A*/UCS", **ORANGE, **UC),
        "nlpcmd": Box(880, 510, 240, 74, "إدخال أمر لغوي عربي", **PURPLE, **UC),
        "places": Box(880, 640, 240, 74, "تصفّح الأماكن", **BLUE, **UC),
        "hist": Box(880, 760, 240, 74, "عرض سجلّ الرحلات", **BLUE, **UC),
        "token": Box(520, 120, 230, 66, "إصدار توكن موقّع", **GREEN, **UC),
        "avoid": Box(520, 380, 230, 66, "استثناء شارع / مكان", **RED, **UC),
        "nlu": Box(520, 510, 230, 66, "تحليل NLU للجملة", **PURPLE, **UC),
        "detail": Box(520, 640, 230, 66, "تفاصيل المكان وجيرانه", **BLUE, **UC),
        "algo": Box(210, 380, 230, 66, "تنفيذ خوارزمية البحث", **ORANGE, **UC),
        "save": Box(520, 760, 230, 66, "حفظ في سجلّ الرحلات", **GREEN, **UC),
        "admin": Box(210, 760, 230, 74, "إدارة الخريطة CRUD", **YELLOW, **UC),
    }
    boxes += uc.values()
    for k in ("login", "map", "route", "nlpcmd", "places", "hist"):
        links.append(Link(user, uc[k], src_side="left", dst_side="right"))
    links.append(Link(admin, uc["admin"], src_side="right", dst_side="left"))
    inc = dict(dashed=True, end="open", color="#7a7a7a", fs=11)
    links += [
        Link(uc["login"], uc["token"], "«include»", **inc),
        Link(uc["route"], uc["algo"], "«include»", **inc),
        Link(uc["avoid"], uc["route"], "«extend»", **inc),
        Link(uc["nlpcmd"], uc["nlu"], "«include»", **inc),
        Link(
            uc["nlu"],
            uc["route"],
            "«include»",
            **inc,
            src_side="top",
            dst_side="bottom",
        ),
        Link(uc["places"], uc["detail"], "«include»", **inc),
        Link(uc["hist"], uc["save"], "«include»", **inc),
        Link(
            uc["route"],
            uc["save"],
            "«include»",
            **inc,
            src_side="bottom",
            dst_side="left",
            src_t=0.2,
        ),
    ]
    return emit("02_use_case", 1400, 880, boxes, links, texts=texts)


def d03_erd():
    boxes, links = [], []
    E = dict(kind="rect", rounded=False, fs=12)
    users = Box(
        830,
        90,
        300,
        0,
        "",
        header="users",
        rows=[
            "PK  id : INTEGER",
            "UQ  username : TEXT",
            "password_hash : TEXT",
            "created_at : TEXT",
        ],
        **GREEN,
        **E,
    )
    hist = Box(
        410,
        60,
        320,
        0,
        "",
        header="search_history",
        rows=[
            "PK  id : INTEGER",
            "FK  user_id → users.id",
            "command : TEXT",
            "destination : TEXT",
            "algorithm : TEXT",
            "path : TEXT",
            "cost : REAL",
            "created_at : TEXT",
        ],
        **BLUE,
        **E,
    )
    nodes = Box(
        830,
        480,
        300,
        0,
        "",
        header="map_nodes",
        rows=["PK  id : TEXT", "name : TEXT", "x : REAL", "y : REAL"],
        **ORANGE,
        **E,
    )
    edges_t = Box(
        200,
        480,
        320,
        0,
        "",
        header="map_edges",
        rows=[
            "PK  id : INTEGER",
            "FK  u → map_nodes.id",
            "FK  v → map_nodes.id",
            "street : TEXT",
        ],
        **ORANGE,
        **E,
    )
    boxes += [users, hist, nodes, edges_t]
    links += [
        Link(
            users,
            hist,
            "1 — N",
            color="#2d6a2d",
            fs=12,
            src_side="left",
            dst_side="right",
        ),
        Link(
            nodes,
            edges_t,
            "N — N",
            color="#8a5a00",
            fs=12,
            src_side="left",
            dst_side="right",
        ),
    ]
    texts = [
        Text(
            700,
            45,
            "مستخدم واحد له عدة عمليات بحث — والوصلة تربط عقدتين (u, v)",
            fs=12,
            color="#555",
        )
    ]
    return emit("03_erd", 1250, 720, boxes, links, texts=texts)


def d04_class():
    boxes, links = [], []
    C = dict(kind="rect", rounded=False, fs=11.5)
    citymap = Box(
        880,
        70,
        290,
        0,
        "",
        header="CityMap",
        rows=[
            "- nodes : dict",
            "- edges : list",
            "- _adj : dict",
            "+ neighbors(node, avoid)",
            "+ heuristic(a, b)",
            "+ find_node_by_name(txt)",
            "+ find_streets_by_name(txt)",
            "+ reload()",
        ],
        **GREEN,
        **C,
    )
    router = Box(
        490,
        70,
        290,
        0,
        "",
        header="Router",
        rows=[
            "- graph : CityMap",
            "+ find_path(start, goal,",
            "    algorithm, avoid)",
            "+ compare(start, goal, avoid)",
        ],
        **ORANGE,
        **C,
    )
    iface = Box(
        90,
        70,
        300,
        0,
        "",
        header="«interface» SearchStrategy",
        rows=["name : str", "+ search(graph, start,", "    goal, avoid) : PathResult"],
        **PURPLE,
        **C,
    )
    ucs = Box(
        60,
        380,
        190,
        0,
        "",
        header="UCS",
        rows=["+ search(...)", 'name = "UCS"'],
        **BLUE,
        **C,
    )
    astar = Box(
        280,
        380,
        190,
        0,
        "",
        header="AStar",
        rows=["+ search(...)", 'name = "A*"'],
        **BLUE,
        **C,
    )
    nlu = Box(
        560,
        380,
        290,
        0,
        "",
        header="nlu «module»",
        rows=[
            "+ parse_command(text,",
            "    citymap, api_key)",
            "+ parse_with_groq()",
            "+ parse_with_rules()",
        ],
        **PURPLE,
        **C,
    )
    pathres = Box(
        880,
        540,
        290,
        0,
        "",
        header="«DTO» PathResult",
        rows=[
            "algorithm, start, goal",
            "path[], streets[]",
            "cost, expanded, found",
            "+ to_dict()",
        ],
        **YELLOW,
        **C,
    )
    userrepo = Box(
        560,
        780,
        270,
        0,
        "",
        header="UserRepository",
        rows=["- conn : sqlite3", "+ create()", "+ get_by_username()", "+ get_by_id()"],
        **GRAY,
        **C,
    )
    histrepo = Box(
        880,
        780,
        290,
        0,
        "",
        header="HistoryRepository",
        rows=["- conn : sqlite3", "+ add(...)", "+ list(user_id, limit)"],
        **GRAY,
        **C,
    )
    api = Box(
        120,
        780,
        300,
        0,
        "",
        header="FastAPI app «main»",
        rows=[
            "/api/auth/*  /api/places*",
            "/api/route  /api/command",
            "/api/admin/*  /api/map(.xml)",
        ],
        **ORANGE,
        **C,
    )
    boxes += [citymap, router, iface, ucs, astar, nlu, pathres, userrepo, histrepo, api]
    dep = dict(dashed=True, end="open", color="#7a7a7a", fs=11)
    links += [
        Link(router, citymap, "يستخدم", src_side="right", dst_side="left"),
        Link(router, iface, "«يختار»", **dep, src_side="left", dst_side="right"),
        Link(
            ucs,
            iface,
            "realizes",
            **dep,
            src_side="top",
            dst_side="bottom",
            src_t=0.3,
            dst_t=0.3,
        ),
        Link(
            astar,
            iface,
            "realizes",
            **dep,
            src_side="top",
            dst_side="bottom",
            src_t=0.7,
            dst_t=0.7,
        ),
        Link(
            router,
            pathres,
            "يُنشئ",
            **dep,
            src_side="bottom",
            dst_side="top",
            src_t=0.7,
            dst_t=0.3,
        ),
        Link(
            nlu,
            citymap,
            "يستعلم الخريطة",
            fs=11,
            src_side="top",
            dst_side="bottom",
            src_t=0.7,
            dst_t=0.3,
        ),
        Link(
            api,
            router,
            "يستدعي",
            src_side="top",
            dst_side="bottom",
            src_t=0.3,
            dst_t=0.3,
        ),
        Link(
            api,
            nlu,
            "يستدعي NLU",
            src_side="top",
            dst_side="bottom",
            src_t=0.7,
            dst_t=0.7,
        ),
        Link(
            api, userrepo, "", src_side="right", dst_side="left", src_t=0.3, dst_t=0.5
        ),
        Link(
            api,
            histrepo,
            "",
            src_side="bottom",
            dst_side="bottom",
            src_t=0.7,
            dst_t=0.5,
        ),
    ]
    texts = [
        Text(
            625,
            45,
            "الأنماط: Strategy ‖ Repository ‖ Singleton (CITY/ROUTER) ‖ DTO",
            fs=12.5,
            bold=True,
            color="#1f4e79",
        )
    ]
    return emit("04_class", 1250, 1080, boxes, links, texts=texts)


def d05_sequence():
    boxes, raw, texts = [], [], []
    W, H = 1250, 1000
    lifex = {
        "user": 105,
        "client": 275,
        "api": 445,
        "nlu": 615,
        "map": 785,
        "router": 955,
        "db": 1125,
    }
    heads = {
        "user": "المستخدم",
        "client": "العميل JS",
        "api": "FastAPI",
        "nlu": "nlu",
        "map": "CityMap",
        "router": "Router",
        "db": "SQLite",
    }
    for k, x in lifex.items():
        boxes.append(
            Box(
                x - 70,
                40,
                140,
                46,
                heads[k],
                rounded=False,
                fill="#e8f0fb",
                stroke="#4a6f9e",
                fs=12.5,
            )
        )
        raw.append(Edge((x, 86), (x, 880), end="none", dashed=True, color="#9aa5b1"))
    M = [
        ("user", "client", "«اكتب جملة عربية»", False),
        ("client", "api", "POST /api/command {text}", False),
        ("api", "nlu", "parse_command(text)", False),
        ("nlu", "map", "find_node_by_name(dest)", False),
        ("map", "nlu", "node_id", True),
        ("nlu", "map", "find_streets_by_name(avoid)", False),
        ("map", "nlu", "[streets]", True),
        ("nlu", "api", "{destination_node, avoid, source}", True),
        ("api", "router", "find_path(start, goal, astar, avoid)", False),
        ("router", "api", "PathResult {path, cost, expanded}", True),
        ("api", "db", "INSERT search_history", False),
        ("api", "client", "JSON {parsed, result}", True),
        ("client", "user", "عرض المسار على الخريطة", True),
    ]
    y = 150
    for a, b, lbl, back in M:
        xa, xb = lifex[a], lifex[b]
        color = "#8a5a00" if back else "#1f4e79"
        raw.append(
            Edge(
                (xa, y),
                (xb, y),
                label=lbl,
                dashed=back,
                end="open" if back else "block",
                color=color,
                fs=11.5,
            )
        )
        y += 60
    boxes.append(
        Box(
            120,
            900,
            300,
            54,
            "يُنفَّذ فقط للمستخدم المسجّل",
            rounded=True,
            dashed=True,
            fill="#fffbe6",
            stroke="#b8a13a",
            fs=12,
        )
    )
    texts = [
        Text(
            625,
            30,
            "مخطط التسلسل — معالجة أمر لغوي عربي «ودّيني حدة، إيّاك تمر من شارع الزبيري»",
            fs=14,
            bold=True,
            color="#1f4e79",
        )
    ]
    return emit("05_sequence", W, H, boxes, raw_edges=raw, texts=texts)


def d06_activity():
    boxes, links, raw, texts = [], [], [], []
    cx = 625
    start = Box(
        cx - 12,
        50,
        24,
        24,
        "",
        kind="ellipse",
        fill="#000",
        stroke="#000",
        rounded=False,
    )
    req = Box(
        cx - 130,
        100,
        260,
        58,
        "استلام طلب المسار\n(start, goal, algorithm, avoid)",
        **BLUE,
    )
    chk1 = Box(
        cx - 150,
        195,
        300,
        96,
        "هل البداية والوجهة\nموجودتان في الخريطة؟",
        kind="diamond",
        **YELLOW,
    )
    http = Box(cx + 220, 210, 200, 62, "HTTP 400\nUnknown place", **RED, fs=12)
    exempt = Box(
        cx - 190,
        340,
        380,
        58,
        "إعفاء البداية والوجهة من قائمة الاستثناء",
        **GREEN,
        fs=12.5,
    )
    strat = Box(
        cx - 150,
        435,
        300,
        96,
        "اختيار الاستراتيجية\n(astar أم ucs)",
        kind="diamond",
        **YELLOW,
    )
    abox = Box(cx - 330, 570, 210, 56, "A*  — f = g + h", **BLUE, fs=12)
    ubox = Box(cx + 120, 570, 210, 56, "UCS — g فقط", **BLUE, fs=12)
    loop = Box(
        cx - 240,
        680,
        480,
        140,
        "",
        rounded=True,
        dashed=True,
        fill="#fbfbfb",
        stroke="#888",
    )
    chk2 = Box(cx - 150, 875, 300, 92, "هل وُجد مسار؟", kind="diamond", **YELLOW)
    nof = Box(cx + 220, 888, 230, 64, "PathResult\nfound = false", **RED, fs=12)
    rebuild = Box(
        cx - 190, 1020, 380, 56, "إعادة بناء المسار + الأسماء العربية", **GREEN, fs=12.5
    )
    savej = Box(
        cx - 190, 1110, 380, 56, "حفظ السجل «إن مسجّل» + إعادة JSON", **GREEN, fs=12.5
    )
    endmain = Box(
        cx - 12,
        1200,
        24,
        24,
        "",
        kind="ellipse",
        fill="#000",
        stroke="#000",
        rounded=False,
    )
    endabort = Box(
        1150 - 12,
        1200,
        24,
        24,
        "",
        kind="ellipse",
        fill="#000",
        stroke="#000",
        rounded=False,
    )
    boxes += [
        start,
        req,
        chk1,
        http,
        exempt,
        strat,
        abox,
        ubox,
        loop,
        chk2,
        nof,
        rebuild,
        savej,
        endmain,
        endabort,
    ]
    texts += [
        Text(cx, 706, "«حلقة البحث»", fs=13, bold=True, color="#555"),
        Text(cx, 738, "① افتح العقدة ذات الأولوى الأقل من الطابور", fs=12.5),
        Text(cx, 764, "② تجاهل الوصلات التي شارعها أو جارها ∈ avoid", fs=12.5),
        Text(cx, 790, "③ حدّث cost_so_far و came_from", fs=12.5),
        Text(cx, 816, "④ توقّف عند بلوغ الهدف", fs=12.5),
        Text(cx + 1150, 1258, "abort", fs=11, color="#888"),
    ]
    links += [
        Link(start, req, src_side="bottom", dst_side="top"),
        Link(req, chk1, src_side="bottom", dst_side="top"),
        Link(chk1, http, "لا", color="#b83b3b", src_side="right", dst_side="left"),
        Link(chk1, exempt, "نعم", color="#2d6a2d", src_side="bottom", dst_side="top"),
        Link(exempt, strat, src_side="bottom", dst_side="top"),
        Link(strat, abox, "astar", src_side="left", dst_side="right"),
        Link(strat, ubox, "ucs", src_side="right", dst_side="left"),
        Link(chk2, nof, "لا", color="#b83b3b", src_side="right", dst_side="left"),
        Link(chk2, rebuild, "نعم", color="#2d6a2d", src_side="bottom", dst_side="top"),
        Link(rebuild, savej, src_side="bottom", dst_side="top"),
    ]
    raw += [
        Edge(abox.x + abox.w // 2, abox.y + abox.h, cx - 70, 680),
        Edge(ubox.x + ubox.w // 2, ubox.y + ubox.h, cx + 70, 680),
        Edge(cx, 820, cx, 875),
        Edge(savej.x + savej.w // 2, savej.y + savej.h, cx, 1200),
    ]
    links.append(
        Link(http, endabort, "", color="#b83b3b", src_side="bottom", dst_side="top")
    )
    links.append(
        Link(
            nof,
            endabort,
            "",
            color="#b83b3b",
            src_side="right",
            dst_side="top",
            src_t=0.8,
        )
    )
    return emit("06_activity", 1250, 1290, boxes, links, raw_edges=raw, texts=texts)


def d07_state():
    boxes, links, texts = [], [], []
    start = Box(
        160, 130, 26, 26, "", kind="ellipse", fill="#000", stroke="#000", rounded=False
    )
    new = Box(
        260, 110, 260, 70, "جديد New\n«إنشاء السجلّ عند نجاح البحث»", **BLUE, fs=12
    )
    saved = Box(
        620, 110, 260, 70, "محفوظ Saved\n«INSERT في قاعدة البيانات»", **GREEN, fs=12
    )
    listed = Box(980, 110, 240, 70, "معروض Listed\n«GET /api/history»", **PURPLE, fs=12)
    trans = Box(
        420,
        330,
        300,
        74,
        "مؤقّت Transient\n«نتيجة ضيف — لا تُحفَظ»",
        **RED,
        fs=12,
        dashed=True,
    )
    end = Box(
        990, 330, 26, 26, "", kind="ellipse", fill="#000", stroke="#000", rounded=False
    )
    boxes += [start, new, saved, listed, trans, end]
    links += [
        Link(start, new, "بحث ناجح", src_side="right", dst_side="left"),
        Link(new, saved, "مستخدم مسجّل", src_side="right", dst_side="left"),
        Link(saved, listed, "طلب السجلّ", src_side="right", dst_side="left"),
        Link(listed, end, src_side="bottom", dst_side="top"),
        Link(
            new,
            trans,
            "مستخدم ضيف",
            dashed=True,
            color="#b83b3b",
            src_side="bottom",
            dst_side="top",
        ),
        Link(
            trans,
            end,
            "انتهاء العرض",
            dashed=True,
            color="#b83b3b",
            src_side="bottom",
            dst_side="bottom",
        ),
    ]
    return emit("07_state", 1300, 560, boxes, links, texts=texts)


def d08_dfd():
    boxes, links, texts = [], [], []
    P = dict(kind="ellipse", fs=12)
    user = Box(
        960, 120, 210, 80, "مستخدم", rounded=False, fill="#f5f5f5", stroke="#333", fs=13
    )
    admin = Box(
        80,
        700,
        210,
        80,
        "مشرف Admin",
        rounded=False,
        fill="#f5f5f5",
        stroke="#333",
        fs=13,
    )
    auth = Box(650, 90, 210, 84, "1.0\nالمصادقة", **ORANGE, **P)
    route = Box(610, 400, 230, 90, "2.0\nتخطيط المسار", **ORANGE, **P)
    nlu = Box(880, 590, 220, 90, "3.0\nتحليل الأمر NLU", **PURPLE, **P)
    manage = Box(170, 400, 230, 90, "4.0\nإدارة الخريطة", **YELLOW, **P)
    d1 = Box(960, 250, 170, 80, "D1 users", kind="cylinder", **GREEN, fs=12)
    d2 = Box(330, 590, 180, 80, "D2 search_history", kind="cylinder", **GREEN, fs=11.5)
    d3 = Box(
        170, 130, 190, 84, "D3 map_nodes\n+ map_edges", kind="cylinder", **GREEN, fs=12
    )
    boxes += [user, admin, auth, route, nlu, manage, d1, d2, d3]
    links += [
        Link(
            user,
            auth,
            "بيانات الدخول",
            fs=11,
            src_side="left",
            dst_side="right",
            src_t=0.3,
            dst_t=0.5,
        ),
        Link(
            auth,
            user,
            "توكن موقّع",
            fs=11,
            src_side="right",
            dst_side="left",
            src_t=0.7,
            dst_t=0.7,
            lside="below",
        ),
        Link(auth, d1, "تحقّق", fs=11, src_side="bottom", dst_side="top"),
        Link(
            user,
            nlu,
            "جملة عربية",
            fs=11,
            src_side="bottom",
            dst_side="top",
            src_t=0.5,
            dst_t=0.6,
        ),
        Link(
            user,
            route,
            "طلب مسار (start, goal, algo, avoid)",
            fs=11,
            src_side="left",
            dst_side="right",
            src_t=0.8,
            dst_t=0.55,
        ),
        Link(
            nlu,
            route,
            "وجهة + استثناءات",
            fs=11,
            src_side="left",
            dst_side="right",
            src_t=0.5,
            dst_t=0.8,
        ),
        Link(
            d3,
            route,
            "قراءة الخريطة",
            fs=11,
            start="block",
            src_side="bottom",
            src_t=0.85,
            dst_side="top",
            dst_t=0.15,
        ),
        Link(
            route,
            d2,
            "كتابة السجلّ",
            fs=11,
            src_side="bottom",
            dst_side="top",
            src_t=0.6,
            dst_t=0.5,
        ),
        Link(
            route,
            user,
            "مسار + تكلفة",
            fs=11,
            src_side="top",
            dst_side="top",
            src_t=0.7,
            dst_t=0.2,
        ),
        Link(admin, manage, "تعديلات CRUD", fs=11, src_side="top", dst_side="bottom"),
        Link(
            manage,
            d3,
            "تحديث العقد والوصلات",
            fs=11,
            src_side="top",
            src_t=0.3,
            dst_side="bottom",
            dst_t=0.3,
        ),
    ]
    texts = [
        Text(
            625,
            45,
            "مخطط تدفّق البيانات DFD — المستوى الأول",
            fs=15,
            bold=True,
            color="#1f4e79",
        )
    ]
    return emit("08_dfd", 1300, 850, boxes, links, texts=texts)


def d09_architecture():
    boxes, links, texts = [], [], []
    boxes.append(
        Box(
            60,
            90,
            1130,
            260,
            "",
            rounded=False,
            fill="#eef6ff",
            stroke="#4a6f9e",
            dashed=True,
            frame=True,
        )
    )
    texts.append(
        Text(
            625,
            122,
            "المنصّة 2 — العميل الأمامي Web Client «HTML/CSS/JS SPA»",
            fs=14,
            bold=True,
            color="#1f4e79",
        )
    )
    screens = Box(120, 150, 310, 120, "الشاشات الـ 12\nHash Router", **BLUE, fs=12.5)
    svgmap = Box(
        470, 150, 310, 120, "خريطة SVG تفاعلية\nعقد + وصلات + مسار", **GREEN, fs=12.5
    )
    fetch = Box(820, 150, 310, 120, "HTTP fetch\nتوكن + JSON", **PURPLE, fs=12.5)
    rest = Box(430, 420, 390, 70, "RESTful API  —  JSON / XML", **YELLOW, fs=13)
    boxes += [screens, svgmap, fetch, rest]
    boxes.append(
        Box(
            60,
            560,
            1130,
            500,
            "",
            rounded=False,
            fill="#f2fbf2",
            stroke="#4f8a4f",
            dashed=True,
            frame=True,
        )
    )
    texts.append(
        Text(
            625,
            592,
            "المنصّة 1 — الخادم الخلفي Backend «Python + FastAPI + uvicorn»",
            fs=14,
            bold=True,
            color="#2d6a2d",
        )
    )
    apilayer = Box(
        120,
        620,
        1030,
        110,
        "طبقة الواجهة API Layer\n"
        "/api/auth/*  /api/places*  /api/route  /api/command  "
        "/api/admin/*  /api/map(.xml)",
        rounded=False,
        fill="#ffffff",
        stroke="#4f8a4f",
        fs=12,
    )
    logic = Box(
        120,
        780,
        500,
        120,
        "طبقة المنطق\nnlu «Groq + rules»\nRouter ← A* / UCS «Strategy»",
        **ORANGE,
        fs=12,
    )
    citymap = Box(
        680,
        780,
        470,
        120,
        "CityMap «graph»\nالعقد: 17 — الوصلات: 21\nSingleton",
        **GREEN,
        fs=12,
    )
    repos = Box(
        120,
        940,
        1030,
        90,
        "طبقة الوصول للبيانات — Repository Pattern\n"
        "UserRepository  ·  HistoryRepository",
        **GRAY,
        fs=12,
    )
    groq = Box(
        900,
        1080,
        180,
        74,
        "Groq API\n«اختياري»",
        rounded=True,
        dashed=True,
        fill="#fff",
        stroke="#888",
        fs=11.5,
    )
    db = Box(
        430,
        1080,
        390,
        90,
        "SQLite — masari.db\nusers · search_history · map_nodes · map_edges",
        kind="cylinder",
        **BLUE,
        fs=12,
    )
    boxes += [apilayer, logic, citymap, repos, groq, db]
    links += [
        Link(
            rest,
            fetch,
            "طلبات / استجابات REST",
            start="block",
            color="#8a5a00",
            src_side="top",
            dst_side="bottom",
            src_t=0.4,
            dst_t=0.5,
        ),
        Link(
            rest, apilayer, "", src_side="bottom", dst_side="top", src_t=0.4, dst_t=0.4
        ),
        Link(
            apilayer,
            logic,
            "",
            src_side="bottom",
            dst_side="top",
            src_t=0.25,
            dst_t=0.5,
        ),
        Link(
            apilayer,
            citymap,
            "",
            src_side="bottom",
            dst_side="top",
            src_t=0.75,
            dst_t=0.5,
        ),
        Link(logic, citymap, "يستخدم", src_side="right", dst_side="left"),
        Link(repos, db, "", src_side="bottom", dst_side="top", src_t=0.3, dst_t=0.3),
        Link(
            groq,
            logic,
            "",
            dashed=True,
            color="#888",
            src_side="top",
            dst_side="bottom",
            src_t=0.5,
            dst_t=0.8,
        ),
        Link(
            citymap, repos, "", src_side="bottom", dst_side="top", src_t=0.6, dst_t=0.8
        ),
    ]
    return emit("09_architecture", 1250, 1220, boxes, links, texts=texts)


if __name__ == "__main__":
    print("Generating ROUTED diagrams ...")
    results = [
        d01_methodology(),
        d02_usecase(),
        d03_erd(),
        d04_class(),
        d05_sequence(),
        d06_activity(),
        d07_state(),
        d08_dfd(),
        d09_architecture(),
    ]
    print("\nExporting PNGs ...")
    pngs = all(
        to_png(n)
        for n in (
            "01_methodology",
            "02_use_case",
            "03_erd",
            "04_class",
            "05_sequence",
            "06_activity",
            "07_state",
            "08_dfd",
            "09_architecture",
        )
    )
    print(
        "\n"
        + (
            "ALL GEOMETRY OK + PNG OK"
            if (all(results) and pngs)
            else ">>> FAILURES — fix before shipping <<<"
        )
    )
