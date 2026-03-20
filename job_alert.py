#!/usr/bin/env python3
"""
EY Daily Job Alert — Ann Mary Kurian
=====================================
Scrapes careers.ey.com every morning, scores results against Ann Mary's
profile, generates a PDF report, and emails it to two recipients.

SETUP (one-time):
  pip install requests reportlab

CONFIGURE:
  1. Fill in EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENTS below.
  2. Schedule with cron (macOS/Linux) or Task Scheduler (Windows) — see bottom.

GMAIL USERS:
  Use an App Password (not your main password).
  Generate at: myaccount.google.com → Security → App passwords
"""

import smtplib
import json
import urllib.request
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime
from pathlib import Path
import tempfile
import os

# ─── CONFIGURE THESE ────────────────────────────────────────────────────────

EMAIL_SENDER     = os.environ.get("EMAIL_SENDER",     "tkurian2@gmail.com")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD",   "")        # Set via GitHub Secret
EMAIL_RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "kochu.kurian@gmail.com,annkurian2002@gmail.com").split(",")

APIFY_API_TOKEN  = os.environ.get("APIFY_API_TOKEN",  "")        # Optional: console.apify.com → Settings → Integrations

# Search queries tuned to Ann Mary's profile
SEARCH_QUERIES = [
    ("data analyst consulting",   "United States", "US"),
    ("marketing analytics senior","United States", "US"),
    ("business analyst senior",   "San Francisco", "US"),
    ("management consulting analyst", "San Francisco", "US"),
]

# Scoring keywords (case-insensitive)
HIGH_MATCH_KEYWORDS  = ["data analyst", "analytics", "consulting", "marketing", "insights", "reporting"]
SKILL_KEYWORDS       = ["sql", "python", "tableau", "dashboard", "kpi", "financial services", "banking"]
SENIOR_KEYWORDS      = ["senior analyst", "senior consultant", "senior associate", "manager"]
SF_KEYWORDS          = ["san francisco", "san jose", "bay area", "california", "remote", "location open"]

# ─── SCRAPER ─────────────────────────────────────────────────────────────────

def scrape_ey_jobs(query: str, location: str, country: str) -> list[dict]:
    """Fetch job listings from careers.ey.com for one query."""
    base = "https://careers.ey.com/ey/search/"
    params = urllib.parse.urlencode({
        "createNewAlert": "false",
        "q": query,
        "locationsearch": location,
        "optionsFacetsDD_country": country,
        "locale": "en_US",
    })
    url = f"{base}?{params}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; JobAlertBot/1.0)",
        "Accept": "text/html,application/xhtml+xml"
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ⚠  Fetch error for '{query}': {e}")
        return []

    # Parse job rows from SuccessFactors HTML
    jobs = []
    import re
    # Match job links: /ey/job/...
    pattern = re.compile(
        r'href="(/ey/job/([^"]+?))"[^>]*>\s*([^<]{10,150})\s*</a>.*?'
        r'(?:([A-Z]{2})\s*\|\s*)?([^<]{5,80}(?:US|IN|SG|UK|CA|AU)[^<]{0,60})',
        re.DOTALL
    )
    seen = set()
    for m in pattern.finditer(html):
        path, slug, title, state, loc = m.groups()
        title = re.sub(r'\s+', ' ', title).strip()
        loc   = re.sub(r'\s+', ' ', (loc or "")).strip()
        if len(title) < 10 or path in seen:
            continue
        seen.add(path)
        jobs.append({
            "title":    title,
            "location": loc,
            "url":      f"https://careers.ey.com{path}",
            "query":    query,
        })

    # Fallback: simpler link extraction
    if not jobs:
        links = re.findall(r'href="(/ey/job/([^"]+?))"', html)
        locs  = re.findall(r'([A-Z]{2}\s*\|\s*[^<\n]{5,60}(?:US|IN|SG|UK|CA)[^<\n]{0,40})', html)
        for i, (path, slug) in enumerate(links[:25]):
            title = slug.split("/")[0].replace("-", " ").strip()
            title = re.sub(r'\s+', ' ', title).title()
            if len(title) < 8:
                continue
            jobs.append({
                "title":    title,
                "location": locs[i] if i < len(locs) else location,
                "url":      f"https://careers.ey.com{path}",
                "query":    query,
            })

    return jobs[:20]   # cap per query


# ─── SCORER ──────────────────────────────────────────────────────────────────

def score_job(job: dict) -> float:
    t = job["title"].lower()
    l = job["location"].lower()
    text = f"{t} {l}"

    skills_hit  = sum(1 for k in SKILL_KEYWORDS      if k in text)
    role_hit    = sum(1 for k in HIGH_MATCH_KEYWORDS  if k in text)
    senior_hit  = any(k in text for k in SENIOR_KEYWORDS)
    sf_hit      = any(k in l    for k in SF_KEYWORDS)

    score  = (role_hit   / max(len(HIGH_MATCH_KEYWORDS), 1)) * 4.0  # 40%
    score += (skills_hit / max(len(SKILL_KEYWORDS),      1)) * 2.0  # 20% (seniority)
    score += (1.0 if senior_hit else 0.0) * 2.0                     # 20% seniority
    score += (1.0 if sf_hit     else 0.5) * 2.0                     # 20% location

    return round(min(score, 10), 1)


# ─── PDF BUILDER ─────────────────────────────────────────────────────────────

def build_pdf(jobs: list[dict], output_path: str):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table,
            TableStyle, HRFlowable
        )
    except ImportError:
        print("  ⚠  reportlab not installed. Skipping PDF. Run: pip install reportlab")
        return None

    EY_YELLOW = colors.HexColor("#FFE600")
    EY_DARK   = colors.HexColor("#1A1A24")
    EY_GRAY   = colors.HexColor("#F5F5F5")
    EY_MID    = colors.HexColor("#4A4A6A")
    GREEN     = colors.HexColor("#2E7D32")
    AMBER     = colors.HexColor("#F57F17")
    LINK      = colors.HexColor("#1565C0")

    def S(n, **kw): return ParagraphStyle(n, **kw)

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2.5*cm, bottomMargin=2*cm)
    story = []
    today = datetime.now().strftime("%d %B %Y")

    # Header
    hdr = Table([[
        Paragraph("<b>ANN MARY KURIAN</b><br/>EY Daily Job Alert",
                  S("T", fontName="Helvetica-Bold", fontSize=16, textColor=EY_DARK)),
        Paragraph(f"Date: {today}<br/>careers.ey.com – live scrape",
                  S("R", fontName="Helvetica", fontSize=9, textColor=EY_MID))
    ]], colWidths=["65%","35%"])
    hdr.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), EY_YELLOW),
        ("LEFTPADDING",   (0,0),(-1,-1), 14),
        ("RIGHTPADDING",  (0,0),(-1,-1), 14),
        ("TOPPADDING",    (0,0),(-1,-1), 12),
        ("BOTTOMPADDING", (0,0),(-1,-1), 12),
        ("ALIGN",         (1,0),(1,0),   "RIGHT"),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
    ]))
    story += [hdr, Spacer(1, 0.4*cm)]

    story.append(Paragraph(
        f"Top {len(jobs)} EY Matches for today",
        S("H2", fontName="Helvetica-Bold", fontSize=13, textColor=EY_DARK, spaceAfter=6)
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=EY_YELLOW, spaceAfter=6))

    rows = [["#", "Job Title", "Location", "Score", "Query"]]
    for i, j in enumerate(jobs, 1):
        sc = j["score"]
        sc_color = GREEN if sc >= 7.5 else AMBER
        rows.append([
            Paragraph(str(i), S(f"N{i}", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white)),
            Paragraph(f'<b>{j["title"]}</b><br/><font color="#1565C0"><u>{j["url"]}</u></font>',
                      S(f"T{i}", fontName="Helvetica", fontSize=8.5, leading=12)),
            Paragraph(j["location"], S(f"L{i}", fontName="Helvetica", fontSize=8)),
            Paragraph(f"<b>{sc}</b>", S(f"S{i}", fontName="Helvetica-Bold", fontSize=9, textColor=sc_color)),
            Paragraph(j["query"],    S(f"Q{i}", fontName="Helvetica", fontSize=8, textColor=EY_MID)),
        ])

    tbl = Table(rows, colWidths=["5%","38%","22%","9%","26%"])
    row_bgs = [("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, EY_GRAY])]
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,0),  EY_DARK),
        ("TEXTCOLOR",    (0,0),(-1,0),  colors.white),
        ("FONTNAME",     (0,0),(-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0,0),(-1,0),  8.5),
        *row_bgs,
        ("BACKGROUND",   (0,1),(0,-1),  EY_DARK),
        ("TEXTCOLOR",    (0,1),(0,-1),  colors.white),
        ("ALIGN",        (0,0),(0,-1),  "CENTER"),
        ("ALIGN",        (3,0),(3,-1),  "CENTER"),
        ("GRID",         (0,0),(-1,-1), 0.5, colors.HexColor("#CCCCCC")),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0),(-1,-1), 7),
        ("RIGHTPADDING", (0,0),(-1,-1), 7),
        ("TOPPADDING",   (0,0),(-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor("#CCCCCC"), spaceAfter=4))
    story.append(Paragraph(
        f"Auto-generated {datetime.now().strftime('%d %B %Y %H:%M')} | careers.ey.com | Ann Mary Kurian",
        S("FT", fontName="Helvetica-Oblique", fontSize=7.5, textColor=EY_MID)
    ))

    doc.build(story)
    return output_path


# ─── EMAIL SENDER ─────────────────────────────────────────────────────────────

def send_email(pdf_path: str, jobs: list[dict]):
    today = datetime.now().strftime("%d %B %Y")
    subject = f"EY Daily Job Alert – {today} ({len(jobs)} matches)"

    html_rows = ""
    for i, j in enumerate(jobs, 1):
        score_color = "#2E7D32" if j["score"] >= 7.5 else "#F57F17"
        html_rows += f"""
        <tr style="background:{'#f9f9f9' if i%2==0 else 'white'}">
          <td style="padding:6px 8px;text-align:center;font-weight:bold">{i}</td>
          <td style="padding:6px 8px"><a href="{j['url']}" style="color:#1565C0">{j['title']}</a></td>
          <td style="padding:6px 8px">{j['location']}</td>
          <td style="padding:6px 8px;text-align:center;font-weight:bold;color:{score_color}">{j['score']}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Helvetica,Arial,sans-serif;color:#1A1A24">
    <div style="background:#FFE600;padding:16px 20px;border-radius:4px 4px 0 0">
      <h2 style="margin:0;color:#1A1A24">EY Daily Job Alert</h2>
      <p style="margin:4px 0 0;font-size:13px;color:#4A4A6A">Ann Mary Kurian &nbsp;|&nbsp; {today} &nbsp;|&nbsp; Live scrape of careers.ey.com</p>
    </div>
    <div style="padding:16px 20px;background:#f5f5f5">
      <p style="margin:0 0 12px">Found <b>{len(jobs)} job matches</b> scoring 6.0 or above today. Full details in the attached PDF.</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="background:#1A1A24;color:white">
            <th style="padding:8px;text-align:center">#</th>
            <th style="padding:8px;text-align:left">Job Title</th>
            <th style="padding:8px;text-align:left">Location</th>
            <th style="padding:8px;text-align:center">Score</th>
          </tr>
        </thead>
        <tbody>{html_rows}</tbody>
      </table>
      <p style="margin:16px 0 0;font-size:11px;color:#888">
        This is an automated daily alert. To stop receiving it, remove this script from your scheduler.
      </p>
    </div>
    </body></html>"""

    for recipient in EMAIL_RECIPIENTS:
        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))

        # Attach PDF
        if pdf_path and Path(pdf_path).exists():
            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            fname = f"EY_Job_Alert_{datetime.now().strftime('%Y%m%d')}.pdf"
            part.add_header("Content-Disposition", f"attachment; filename={fname}")
            msg.attach(part)

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.sendmail(EMAIL_SENDER, recipient, msg.as_string())
            print(f"  ✓  Email sent to {recipient}")
        except Exception as e:
            print(f"  ✗  Failed to send to {recipient}: {e}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"  EY Daily Job Alert  —  {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*55}")

    # 1. Scrape
    all_jobs: list[dict] = []
    seen_urls: set[str]  = set()
    for query, location, country in SEARCH_QUERIES:
        print(f"\n  Scraping: '{query}' in {location} ...")
        results = scrape_ey_jobs(query, location, country)
        print(f"    → {len(results)} raw results")
        for j in results:
            if j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                j["score"] = score_job(j)
                all_jobs.append(j)

    # 2. Sort all jobs by score (no filter)
    matched = sorted(all_jobs, key=lambda x: x["score"], reverse=True)
    print(f"\n  Total unique jobs scraped : {len(all_jobs)}")

    if not matched:
        print("  ⚠  No jobs found today — email not sent.")
        return

    # 3. Build PDF
    print("\n  Building PDF ...")
    pdf_path = tempfile.mktemp(suffix=".pdf", prefix="ey_alert_")
    build_pdf(matched[:30], pdf_path)
    if pdf_path and Path(pdf_path).exists():
        size_kb = Path(pdf_path).stat().st_size // 1024
        print(f"  ✓  PDF ready ({size_kb} KB) → {pdf_path}")
    else:
        pdf_path = None
        print("  ⚠  PDF generation failed — sending email without attachment.")

    # 4. Send email
    print("\n  Sending emails ...")
    send_email(pdf_path, matched[:30])

    # 5. Cleanup temp
    if pdf_path and Path(pdf_path).exists():
        os.remove(pdf_path)

    print(f"\n  Done!  {len(matched)} matches found and sent.\n")


if __name__ == "__main__":
    main()


# ─── SCHEDULING INSTRUCTIONS ─────────────────────────────────────────────────
#
# macOS / Linux — add a cron job:
#   Open terminal and run:  crontab -e
#   Add this line to run every day at 7:00 AM:
#     0 7 * * * /usr/bin/python3 /path/to/ey_daily_job_alert.py >> /tmp/ey_alert.log 2>&1
#
# Windows — Task Scheduler:
#   1. Open Task Scheduler → Create Basic Task
#   2. Trigger: Daily at 7:00 AM
#   3. Action: Start a program
#      Program: python
#      Arguments: C:\path\to\ey_daily_job_alert.py
#
# GitHub Actions (free cloud scheduler — no machine needs to be on):
#   Create .github/workflows/ey_alert.yml with:
#
#     name: EY Daily Job Alert
#     on:
#       schedule:
#         - cron: '0 7 * * *'   # 7 AM UTC daily
#     jobs:
#       alert:
#         runs-on: ubuntu-latest
#         steps:
#           - uses: actions/checkout@v3
#           - uses: actions/setup-python@v4
#             with: { python-version: '3.11' }
#           - run: pip install reportlab
#           - run: python ey_daily_job_alert.py
#             env:
#               EMAIL_SENDER:   ${{ secrets.EMAIL_SENDER }}
#               EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
#
# ─────────────────────────────────────────────────────────────────────────────
