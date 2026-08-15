#!/usr/bin/env python3
"""import-cookies.py — Importe les cookies Netscape (.txt) et les convertit en sessions Playwright.

Usage:
  python3 scripts/import-cookies.py
  python3 scripts/import-cookies.py --dir ~/Downloads

Place les fichiers *cookies.txt dans le répertoire (défaut: .) et lance.
"""

import asyncio, os, sys, glob, json
from playwright.async_api import async_playwright

SESSION_DIR = os.path.expanduser("~/.auto-freelance/sessions")
COOKIE_DIR = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--dir" else "."
if "--dir" in sys.argv:
    idx = sys.argv.index("--dir")
    if idx + 1 < len(sys.argv):
        COOKIE_DIR = sys.argv[idx + 1]

def parse_netscape_cookie(filepath):
    cookies = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, _, name, value = parts[:7]
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "httpOnly": False,
                "secure": secure == "TRUE",
                "sameSite": "Lax"
            })
    return cookies

COOKIE_FILES = {
    "freework": "*free-work.com*cookies.txt",
    "freelance-informatique": "*freelance-informatique*cookies.txt",
    "freelancermap": "*freelancermap*cookies.txt",
}

async def main():
    os.makedirs(SESSION_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for source, pattern in COOKIE_FILES.items():
            files = glob.glob(os.path.join(COOKIE_DIR, pattern))
            if not files:
                print(f"  ⚠️  {source}: aucun fichier trouvé ({pattern})")
                continue
            filepath = files[0]
            context = await browser.new_context()
            cookies = parse_netscape_cookie(filepath)
            await context.add_cookies(cookies)
            state_path = os.path.join(SESSION_DIR, f"{source}.json")
            await context.storage_state(path=state_path)
            print(f"  ✅ {source}: {len(cookies)} cookies → {state_path}")
            await context.close()
        await browser.close()
    print(f"\n📁 Sessions dans {SESSION_DIR}/")

if __name__ == "__main__":
    asyncio.run(main())