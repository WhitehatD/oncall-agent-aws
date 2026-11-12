"""Draws docs/architecture.svg. Run: python docs/diagram.py docs/architecture.svg"""
import sys
from xml.sax.saxutils import escape as e

W, H = 1280, 1010
BG, CARD, LINE, TEXT, MUTED = "#0d1117", "#161b22", "#30363d", "#e6edf3", "#8b949e"
C = dict(compute="#ED7100", db="#C925D1", net="#8C4FFF", integ="#E7157B", mgmt="#E7157B",
         sec="#DD344C", ext="#6e7681", human="#58a6ff")
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

out = []
add = out.append


def text(x, y, s, size=13, fill=TEXT, weight=400, anchor="start", mono=False, spacing=0):
    fam = MONO if mono else SANS
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{ls}>{e(s)}</text>')


def zone(x, y, w, h, label, note=""):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="#0f141b" '
        f'stroke="{LINE}" stroke-dasharray="5 5"/>')
    text(x + 20, y + 26, label.upper(), 11, MUTED, 600, mono=True, spacing=1.2)
    if note:
        text(x + w - 20, y + 26, note, 11, MUTED, 400, "end", mono=True)


def node(x, y, w, svc, color, title, sub="", h=58, dashed=False):
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{CARD}" stroke="{LINE}"{dash}/>')
    add(f'<rect x="{x}" y="{y + 10}" width="3" height="{h - 20}" rx="1.5" fill="{color}"/>')
    text(x + 16, y + 18, svc.upper(), 9.5, color, 600, mono=True, spacing=0.8)
    text(x + 16, y + 36, title, 14, TEXT, 600)
    if sub:
        text(x + 16, y + 51, sub, 11, MUTED)


def step(x, y, w, n, title, who, color, h=46):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{CARD}" stroke="{LINE}"/>')
    add(f'<circle cx="{x + 24}" cy="{y + h / 2}" r="12" fill="none" stroke="{color}" stroke-width="1.5"/>')
    text(x + 24, y + h / 2 + 4.5, str(n), 12, color, 700, "middle", mono=True)
    text(x + 46, y + h / 2 + 5, title, 14, TEXT, 600)
    text(x + w - 14, y + h / 2 + 4, who, 10.5, MUTED, 400, "end", mono=True)


def path(d, color=MUTED, dashed=False, marker="a", width=1.5):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    m = f' marker-end="url(#{marker})"' if marker else ""
    # a background-coloured halo keeps crossings readable
    add(f'<path d="{d}" fill="none" stroke="{BG}" stroke-width="{width + 4}"/>')
    add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash}{m}/>')


def label(x, y, s, color=MUTED, anchor="middle", bg=BG):
    w = len(s) * 6.1 + 10
    x0 = x - w / 2 if anchor == "middle" else (x if anchor == "start" else x - w)
    add(f'<rect x="{x0}" y="{y - 12}" width="{w}" height="17" rx="4" fill="{bg}"/>')
    text(x0 + w / 2, y + 1, s, 10.5, color, 400, "middle", mono=True)


add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
add(f'''<defs>
  <marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>
  <marker id="h" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{C['human']}"/></marker>
  <marker id="o" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{C['compute']}"/></marker>
  <marker id="r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{C['sec']}"/></marker>
  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#161b22" stroke-width="1"/></pattern>
</defs>''')
add(f'<rect width="{W}" height="{H}" rx="18" fill="{BG}"/>')
add(f'<rect width="{W}" height="{H}" rx="18" fill="url(#grid)"/>')

# title
text(40, 52, "oncall-agent-aws", 24, TEXT, 700)
text(40, 78, "An alarm fires, an agent finds the cause, a person approves the fix. IAM, not the prompt, decides what it can touch.", 13.5, MUTED)

# ---------------- zones ----------------
ZT, ZB = 110, 770
zone(40, ZT, 360, ZB - ZT, "Demo service", "oncall=managed")
zone(440, 220, 380, ZB - 220, "Step Functions", "one incident")
zone(860, 220, 380, ZB - 220, "Agent and approval")

# ---------------- demo service ----------------
node(70, 150, 300, "CloudWatch", C["mgmt"], "6 alarms", "5xx, errors, throttles, queue age, no traffic")
node(70, 258, 300, "EventBridge + Lambda", C["integ"], "demo-traffic", "places orders every minute")
node(70, 352, 300, "API Gateway", C["net"], "demo-orders", "HTTP API")
node(70, 446, 300, "Lambda", C["compute"], "demo-orders-api", "alias live → version N")
node(70, 540, 140, "DynamoDB", C["db"], "orders")
node(230, 540, 140, "SQS", C["integ"], "orders", "+ dead-letter queue")
node(230, 634, 140, "Lambda", C["compute"], "worker", "consumes the queue")
path("M220 316 V352")
path("M220 410 V446")
path("M140 504 V540")
path("M300 504 V540")
path("M300 598 V634")
path("M230 663 H140 V598")

# ---------------- trigger + model (top row) ----------------
node(470, 150, 320, "EventBridge rule", C["integ"], "Alarm went to ALARM", "starts one execution per alarm")
node(890, 150, 320, "External · OpenRouter", C["ext"], "Claude Haiku 5.5", "key in SSM, ~1¢ per incident", dashed=True)
path("M370 179 H470")
path("M630 208 V258")

# ---------------- workflow steps ----------------
SX, SW = 470, 320
ys = [258, 324, 390, 456, 522, 588]
steps = [("Open incident", "control"), ("Investigate", "agent"), ("Validate the call", "executor"),
         ("Wait for approval", "task token"), ("Execute", "executor"), ("Verify alarms", "control")]
for i, ((t, who), y) in enumerate(zip(steps, ys), 1):
    step(SX, y, SW, i, t, who, C["integ"] if who in ("task token",) else C["compute"])
for a, b in zip(ys, ys[1:]):
    path(f"M630 {a + 46} V{b}")
node(470, 668, 150, "Outcome", "#3fb950", "Resolved", "email the summary")
node(640, 668, 150, "Executor", C["compute"], "Undo, retry", "up to 3 attempts")
path("M600 634 V668")
path("M660 634 V668")
# retry loop: undo -> investigate, on the left of the column
path("M715 726 V748 H455 V347 H470")
label(560, 752, "failed fix fed back")

# executor writes into the demo service
add(f'<path d="M470 545 H404" fill="none" stroke="{BG}" stroke-width="6"/>')
add(f'<path d="M470 545 H404" fill="none" stroke="{C["compute"]}" stroke-width="2" marker-end="url(#o)"/>')
label(421, 531, "approved call", C["compute"])

# ---------------- agent column ----------------
node(890, 318, 320, "Lambda · LangGraph", C["compute"], "Agent", "cites evidence, proposes one call + its undo")
path("M790 347 H890")
path("M1050 208 V318", dashed=True, marker="")
add(f'<path d="M1050 314 l-4 -8 h8 z" fill="{MUTED}"/>')
add(f'<path d="M1050 212 l-4 8 h8 z" fill="{MUTED}"/>')
node(890, 410, 320, "IAM role", C["sec"], "oncall-investigator", "read-only, no data, no secrets")
path("M1050 376 V410")
label(1050, 396, "assumes")

# approval
node(890, 502, 320, "SNS", C["integ"], "Email", "diagnosis, evidence, exact call, undo")
node(890, 594, 150, "You", C["human"], "Approve", "or reject + why", dashed=True)
node(1060, 594, 150, "API GW + Lambda", C["net"], "Approval page", "")
path("M790 472 H840 V531 H890", C["human"], marker="h")
path("M1050 560 V594", C["human"], marker="h")
path("M1040 623 H1060", C["human"], marker="h")
path("M1135 652 V694 H826 V490 H794", C["human"], marker="h")
label(985, 698, "SendTaskSuccess", C["human"])

# investigator reads: around the right edge and under everything into the demo service
path("M1210 439 H1252 V792 H220 V774", C["sec"], dashed=True, marker="r")
label(700, 796, "Describe / List / Get, Logs Insights, metrics, CloudTrail", C["sec"])

# incidents table, under the agent column
node(890, 704, 320, "DynamoDB", C["db"], "oncall-incidents", "every step, call, cost and decision")

# ---------------- three checks ----------------
text(40, 838, "THREE CHECKS BETWEEN THE MODEL AND THE ACCOUNT", 11, MUTED, 600, mono=True, spacing=1.2)
cards = [
    (C["sec"], "1 · IAM role the tools run as", "oncall-investigator",
     ["AWS ReadOnlyAccess, minus object reads, table",
      "items, queue messages, secrets and KMS decrypt.",
      "It cannot see its own model key."]),
    (C["compute"], "2 · Before any write", "src/shared/policy.py",
     ["A real API call with valid parameters, an undo",
      "call, no deletes, IAM, KMS or access settings,",
      "nothing named oncall-*, at most 3 writes."]),
    (C["sec"], "3 · IAM role the executor runs as", "write-managed + boundary",
     ["Writes only to resources tagged oncall=managed.",
      "A permission boundary blocks destructive actions",
      "even if the role policy is widened by mistake."]),
]
cw, gap = 386, 21
for i, (col, head, sub, lines) in enumerate(cards):
    x = 40 + i * (cw + gap)
    add(f'<rect x="{x}" y="852" width="{cw}" height="126" rx="12" fill="{CARD}" stroke="{LINE}"/>')
    add(f'<rect x="{x}" y="852" width="{cw}" height="3" rx="1.5" fill="{col}"/>')
    text(x + 18, 880, head, 14, TEXT, 600)
    text(x + 18, 898, sub, 10.5, col, 400, mono=True)
    for j, ln in enumerate(lines):
        text(x + 18, 922 + j * 17, ln, 12, MUTED)

add("</svg>")
open(sys.argv[1], "w", encoding="utf-8").write("\n".join(out))
print("ok")
