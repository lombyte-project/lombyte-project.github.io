#!/usr/bin/env python3
"""Build the GitHub Pages site into build/site.

Run from a checkout of the game repository's main branch: the logo comes from
assets/, the commits bar from its git log, the numbers from report.json on its
`progress` branch. The page sources live next to this script.

usage: build_site.py PROGRESS_DIR [SITE_URL]
"""
import datetime
import html
import json
import pathlib
import shutil
import subprocess
import sys

from PIL import Image

progress = pathlib.Path(sys.argv[1])
site = sys.argv[2] if len(sys.argv) > 2 else "https://lombyte-project.github.io/"
out = pathlib.Path("build/site")

m = json.loads((progress / "report.json").read_text())["measures"]
REPO = "https://github.com/lombyte-project/lombyte"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def commits_bar(count=5):
    """Announcement bar with the latest commits on the checked-out branch."""
    log = git("log", f"-{count}", "--format=%H%x09%cs%x09%s").splitlines()
    items = "".join(
        f'<li><a href="{REPO}/commit/{sha}"><time datetime="{day}">{day}</time>'
        f"{html.escape(subject)}</a></li>"
        for sha, day, subject in (line.split("\t", 2) for line in log)
    )
    return (f'<aside class="bar" aria-label="Latest commits"><strong>Latest on main</strong>'
            f"<ol>{items}</ol></aside>")


values = {
    "@COMMITS@": commits_bar(),
    "@COMMITS_N@": f"{int(git('rev-list', '--count', 'HEAD')):,}",
    "@SITE@": site,
    "@DATE@": datetime.date.today().isoformat(),
    "@C_EXACT@": f"{float(m['matched_code_percent']):.2f}",
    "@FUNCS_DONE@": f"{int(m['matched_functions']):,}",
    "@FUNCS@": f"{int(m['total_functions']):,}",
    "@UNITS_DONE@": f"{int(m['complete_units']):,}",
    "@UNITS@": f"{int(m['total_units']):,}",
}

shutil.rmtree(out, ignore_errors=True)
shutil.copytree(pathlib.Path(__file__).resolve().parent / "site", out)
# Full logo for link previews, a small copy for the page.
logo = Image.open("assets/lombyte-logo.png")
shutil.copy("assets/lombyte-logo.png", out)
logo.resize((256, 259), Image.LANCZOS).save(out / "logo-256.png", optimize=True)
# Favicons must be square (Google wants a multiple of 48 px): pad, then scale.
side = max(logo.size)
square = Image.new("RGBA", (side, side))
square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))
apple = Image.new("RGB", (180, 180), "#000")
apple.paste(square.resize((150, 150), Image.LANCZOS), (15, 15), square.resize((150, 150), Image.LANCZOS))
apple.save(out / "apple-touch-icon.png", optimize=True)
for name in ("index.html", "robots.txt", "sitemap.xml"):
    text = (out / name).read_text()
    for key, value in values.items():
        text = text.replace(key, value)
    (out / name).write_text(text)
(out / ".nojekyll").touch()
print(f"site built in {out}")
