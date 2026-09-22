#!/usr/bin/env python3
"""apply_diagrams.py — swap ASCII diagram blocks for draw.io PNG exports
in Project_Documentation.md, add the DFD section, and fix the figure lists."""

from pathlib import Path

MD = Path(__file__).resolve().parent.parent / "Project_Documentation.md"
t = MD.read_text(encoding="utf-8")

IMG = '<div align="center">\n\n![{alt}]({path})\n\n</div>\n'

FIGS = [
    # (figure no, image, alt)
    ("1-1", "diagrams/01_methodology.png", "مخطط منهجية تطوير المشروع"),
    ("3-1", "diagrams/02_use_case.png", "مخطط حالات الاستخدام"),
    ("3-2", "diagrams/03_erd.png", "مخطط الكيانات والعلاقات ERD"),
    ("3-3", "diagrams/04_class.png", "مخطط الفئات مع الأنماط التصميمية"),
    ("3-4", "diagrams/05_sequence.png", "مخطط التتابع — معالجة أمر لغوي عربي"),
    ("3-5", "diagrams/06_activity.png", "مخطط النشاط — تخطيط مسار بقيود استثناء"),
    ("4-1", "diagrams/09_architecture.png", "معمارية النظام متعدّدة الطبقات"),
]


def swap_block_before(anchor: str, replacement: str) -> None:
    """Replace the fenced code block that immediately precedes `anchor`."""
    global t
    i = t.index(anchor)
    close = t.rindex("```", 0, i)  # closing fence of the block
    open_ = t.rindex("```", 0, close)  # opening fence of the block
    t = t[:open_] + replacement.rstrip() + t[close + 3 :]


for fig, path, alt in FIGS:
    swap_block_before(f"> **شكل {fig}", IMG.format(alt=alt, path=path))
    old_cap_start = t.index(f"> **شكل {fig}")
    line_end = t.index("\n", old_cap_start)
    line = t[old_cap_start:line_end]
    if "draw.io" not in line:
        t = (
            t[:line_end]
            + " — أُنتِج بأداة draw.io (ملفات المخططات في مجلد diagrams)"
            + t[line_end:]
        )
    print(f"swapped figure {fig}")

# --- state diagram (3.5.6): swap block, add caption (had none) --------------
state_hdr = t.index("### 3.5.6")
nxt = t.index("## 3.6")  # section ends before 3.6
seg = t[state_hdr:nxt]
close = seg.rindex("```")
open_ = seg.rindex("```", 0, close)
new_seg = (
    seg[:open_]
    + IMG.format(
        alt="مخطط الحالات — دورة حياة سجلّ البحث", path="diagrams/07_state.png"
    ).rstrip()
    + seg[close + 3 :]
)
if "شكل 3-6" not in new_seg:
    new_seg = new_seg.rstrip() + (
        "\n\n> **شكل 3-6: مخطط الحالات (State Diagram) لسجلّ البحث — "
        "أُنتِج بأداة draw.io.**\n\n"
    )
t = t[:state_hdr] + new_seg + t[nxt:]
print("swapped state diagram + added caption 3-6")

# --- new DFD section 3.5.7 before 3.6 ---------------------------------------
dfd = """### 3.5.7 مخطط تدفّق البيانات (DFD)

يُوضّح مخطط تدفّق البيانات بال مستوى الأول حركة البيانات بين الكيانات الخارجية (المستخدم والمشرف) والعمليات الأربع داخل النظام (المصادقة، تخطيط المسار، تحليل الأمر العربي، إدارة الخريطة) ومستودعات البيانات الثلاثة (المستخدمون، سجلّ البحث، الخريطة):

<div align="center">

![شكل 3-7: مخطط تدفّق البيانات DFD](diagrams/08_dfd.png)

</div>

> **شكل 3-7: مخطط تدفّق البيانات (DFD) — المستوى الأول — أُنتِج بأداة draw.io.**

"""
t = t.replace("## 3.6 توصيف المخططات", dfd + "## 3.6 توصيف المخططات", 1)
print("added DFD section 3.5.7 + figure 3-7")

# --- figure list: add 3-6 and 3-7 rows ---------------------------------------
t = t.replace(
    "| شكل 3-5 | مخطط النشاط — تخطيط مسار بقيود استثناء | 20 |",
    "| شكل 3-5 | مخطط النشاط — تخطيط مسار بقيود استثناء | 20 |\n"
    "| شكل 3-6 | مخطط الحالات (State Diagram) لسجلّ البحث | 21 |\n"
    "| شكل 3-7 | مخطط تدفّق البيانات (DFD) — المستوى الأول | 22 |",
    1,
)
print("updated فهرس الأشكال")

MD.write_text(t, encoding="utf-8")
print(f"\nOK — {MD}")
