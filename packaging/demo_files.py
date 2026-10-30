"""Create a folder of made-up files for screenshots (no personal data).

Usage: python demo_files.py <target folder>
"""
import os
import random
import shutil
import sys
import time

from PIL import Image, ImageDraw, ImageFilter

root = sys.argv[1]
random.seed(7)
shutil.rmtree(root, ignore_errors=True)

def mk(rel, data=b'', days_ago=0):
    p = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    t = time.time() - days_ago * 86400 - random.randint(0, 80000)
    os.utime(p, (t, t))
    return p

def photo(seed, w=1600, h=1067):
    """A soft landscape: sky gradient, sun, layered hills."""
    r = random.Random(seed)
    sky = [(r.randint(60, 120), r.randint(120, 180), r.randint(200, 250)),
           (r.randint(230, 255), r.randint(170, 210), r.randint(120, 170))]
    im = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(sky[0][i] * (1 - t) + sky[1][i] * t) for i in range(3)))
    sx, sy = r.randint(200, w - 200), r.randint(150, h // 2)
    d.ellipse([sx - 90, sy - 90, sx + 90, sy + 90], fill=(255, 236, 190))
    for layer in range(4):
        base = h * (0.5 + layer * 0.12)
        col = (30 + layer * 12, 70 + layer * 20 - r.randint(0, 20), 60 + layer * 10)
        pts = [(0, h)] + [(x, base - 80 * abs(((x / w) * (2 + layer) + r.random()) % 2 - 1)) for x in range(0, w + 80, 80)] + [(w, h)]
        d.polygon(pts, fill=col)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    grain = Image.effect_noise((w, h), 28).convert('RGB')     # film grain: real-photo file sizes
    return Image.blend(im, grain, 0.08)

def jpg(im, q=86):
    import io
    b = io.BytesIO()
    im.save(b, 'JPEG', quality=q)
    return b.getvalue()

def pdf(title, lines):
    """Minimal one-page PDF with text."""
    text = 'BT /F1 22 Tf 60 760 Td (%s) Tj ET ' % title
    y = 720
    for l in lines:
        text += 'BT /F1 12 Tf 60 %d Td (%s) Tj ET ' % (y, l.replace('(', '[').replace(')', ']'))
        y -= 20
    objs = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
            '<< /Length %d >>\nstream\n%s\nendstream' % (len(text), text),
            '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offs = '%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += '%d 0 obj\n%s\nendobj\n' % (i, o)
    x = len(out)
    out += 'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + ''.join('%010d 00000 n \n' % o for o in offs)
    out += 'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, x)
    return out.encode('latin-1')

# Photos, with some duplicates under other names
shots = []
for i, name in enumerate(['Goa beach sunset', 'Rishikesh morning', 'Coorg hills', 'Ladakh road',
                          'Udaipur lake', 'Munnar tea gardens', 'Hampi ruins', 'Shillong mist']):
    b = jpg(photo(i))
    shots.append(b)
    mk(f'Pictures/Trips 2026/{name}.jpg', b, days_ago=30 + i * 9)
mk('Downloads/IMG_20260412_183015.jpg', shots[0], 160)
mk('Pictures/Phone backup/IMG_20260412_183015.jpg', shots[0], 160)
mk('Downloads/Rishikesh morning (1).jpg', shots[1], 120)
mk('Desktop/wallpaper.jpg', shots[3], 200)
mk('Pictures/Phone backup/IMG_20260519_071144.jpg', shots[5], 140)
mk('Pictures/Trips 2026/Munnar tea gardens - Copy.jpg', shots[5], 100)
for i in range(18):
    mk(f'Pictures/Phone backup/IMG_2026{i:02d}{i+10}_1{i:02d}233.jpg', jpg(photo(100 + i, 800, 600), 70), 150 + i)

# Documents
mk('Documents/Travel/Goa trip itinerary.pdf', pdf('Goa trip itinerary', [
    'Day 1  Arrive Dabolim, check in at Candolim', 'Day 2  Fort Aguada, Sinquerim beach',
    'Day 3  Old Goa churches, Panjim Latin Quarter', 'Day 4  Palolem, kayaking at sunset',
    'Day 5  Spice plantation, fly back']), 20)
mk('Documents/Travel/Goa trip itinerary (1).pdf', pdf('Goa trip itinerary', [
    'Day 1  Arrive Dabolim, check in at Candolim', 'Day 2  Fort Aguada, Sinquerim beach',
    'Day 3  Old Goa churches, Panjim Latin Quarter', 'Day 4  Palolem, kayaking at sunset',
    'Day 5  Spice plantation, fly back']), 19)
mk('Documents/Finance/Household budget 2026.csv',
   'Month,Rent,Groceries,Travel,Savings\n' + ''.join(f'{m},25000,{random.randint(8,14)*1000},{random.randint(2,20)*1000},{random.randint(10,30)*1000}\n'
   for m in ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep']), 12)
mk('Documents/Finance/Rent agreement 2026.pdf', pdf('Rent agreement', ['Between the owner and the tenant,', 'for the flat at 4B, Green Park.', 'Term: 11 months from 1 April 2026.']), 200)
mk('Documents/Notes/Reading list.md', '# Reading list\n\n- [x] The Overstory\n- [ ] A Suitable Boy\n- [ ] Sapiens\n- [ ] The Ministry of Utmost Happiness\n\n## Notes\n\nFinish one book a month. Start with the shortest.\n', 3)
mk('Documents/Notes/Ideas.txt', 'App ideas\n- a weekly letter circle on a typewriter\n- a map of every bookshelf in the neighbourhood\n- plant watering reminders that learn from the weather\n', 1)
mk('Documents/Work/Quarterly report Q3.pdf', pdf('Quarterly report Q3', ['Revenue up 18% on Q2.', 'New customers: 412.', 'Next: launch the mobile app.']), 15)
mk('Documents/Work/Quarterly report Q3 - final.pdf', pdf('Quarterly report Q3', ['Revenue up 18% on Q2.', 'New customers: 412.', 'Next: launch the mobile app.']), 14)
mk('Documents/Work/Presentation draft.pptx', os.urandom(2_400_000), 40)

# Code project with a forgotten node_modules
mk('Projects/weather-widget/package.json', '{\n  "name": "weather-widget",\n  "version": "0.3.0",\n  "dependencies": { "chart.js": "^4.4.0" }\n}\n', 210)
mk('Projects/weather-widget/src/app.js', "import Chart from 'chart.js/auto';\n\nexport async function render(city) {\n  const res = await fetch(`/api/forecast?city=${city}`);\n  const days = await res.json();\n  return new Chart(document.querySelector('#chart'), {\n    type: 'line',\n    data: { labels: days.map(d => d.date), datasets: [{ data: days.map(d => d.max) }] }\n  });\n}\n", 210)
for i in range(40):
    mk(f'Projects/weather-widget/node_modules/pkg{i}/index.js', os.urandom(random.randint(20_000, 400_000)), 210)

# Downloads clutter
mk('Downloads/VSCodeUserSetup-x64.exe', os.urandom(5_200_000), 90)
mk('Downloads/ZoomInstaller.exe', os.urandom(3_100_000), 75)
mk('Downloads/old-backup-2024.zip', os.urandom(8_000_000), 420)
mk('Downloads/Boarding pass BLR-GOI.pdf', pdf('Boarding pass', ['BLR to GOI', 'Seat 14C', 'Gate 22']), 25)
mk('Music/Morning raga.mp3', os.urandom(4_000_000), 300)
mk('Videos/Goa drone clip.mp4', os.urandom(9_000_000), 28)
print('demo files in', root)
