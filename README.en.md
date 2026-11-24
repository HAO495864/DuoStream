# Douyin Downloader · Multi-Device Web Edition

This wraps the download engine of the open-source project [douyin-downloader](https://github.com/jiji262/douyin-downloader) into a web service. Any phone, tablet or other computer on the same home/office LAN can open a browser to submit downloads and watch progress.

**The original project `D:\DouyinDownloader` is untouched** — `engine\` here is an independent copy, and all changes live only in `D:\DouyinWeb`.

---

## Directory layout

```
D:\DouyinWeb\
├── engine\              Download engine (a full independent copy of the original project)
├── webapp\
│   ├── web_server.py    Web service (FastAPI, listening on 0.0.0.0)
│   ├── extras.py        Extra features: trending list / keyword search / Whisper transcription
│   ├── progress.py      Overall download progress (real percentages, from the engine's item and byte callbacks)
│   ├── media.py         Media streaming (the host only sends bytes, decoding happens on the phone)
│   ├── ytdlp_engine.py  yt-dlp engine (1750+ sites, with progress parsing)
│   ├── yt_routes.py     Web API for yt-dlp: probing, engine status
│   ├── url_clean.py     Extracts links from shared text (the whole block an app copies)
│   └── index.html       Front end (single file, styles and scripts all inlined)
├── static\fonts\        Noto Serif SC subsets (woff2 170KB + ttf fallback)
├── static\img\          Section LOGOs (2MB originals, compressed to about 5KB)
├── whisper-models\      Whisper model cache (kept off the C drive)
├── downloads\           Default location for downloaded files
├── config.json          Web service config: port, save directory, quality, access code
├── 启动服务.bat          Double-click to start the service; the console prints the access address
├── 登录抖音.bat          Scan the QR code on the host machine (required before bulk profile scraping)
└── README.md
```

---

## 1. Starting it

Double-click **`启动服务.bat`**. A black console window opens and prints:

```
  Local access:   http://127.0.0.1:8790/
  Other devices:  http://192.168.x.x:8790/   (same LAN)
```

Keep that window open — closing it stops the service. On other devices, open the `192.168.x.x` address in a browser.

---

## 2. When other devices cannot connect

Nine times out of ten Windows Firewall is blocking it. Three workarounds, pick one:

1. On first launch Windows asks "Allow Python to access the network?" — click **Allow** (check Private networks).
2. Open PowerShell as administrator and run once (opens port 8790 permanently):
   ```powershell
   netsh advfirewall firewall add rule name="DouyinWeb 8790" dir=in action=allow protocol=TCP localport=8790
   ```
3. Temporarily disable the firewall to confirm that it is the culprit, then add the rule from step 2.

Also check:
- Both devices are on the **same router / same Wi-Fi** (some routers enable AP isolation, which blocks device-to-device traffic; turn it off in the router)
- Use the IP printed in the console, not `127.0.0.1`

---

## 3. Login (required for bulk profile scraping)

Douyin is very strict with unauthenticated requests. Log in **on the host machine**, because the QR window can only appear there:

1. Double-click `登录抖音.bat`
2. A browser window opens; scan the QR code with the Douyin app on your phone
3. Back in the console, press Enter; the cookie is written into `engine\config.yml`
4. The status in the top-right of the web page changes from "Not signed in" to "Signed in"

Run the same bat file whenever the cookie expires.

---

## 4. What the page can do

| Feature | Description |
|:---|:---|
| Batch submit | One link per line; supports posts / image posts / collections / profiles / live streams; short links can be pasted directly |
| Save directory | Editable; clicking "Apply settings" takes effect immediately and writes to config.json |
| Quality | Highest transcode tier / original / 1080p / 720p / lowest |
| Task list | Queued, downloading, finished, failed; shows succeeded/failed/skipped counts, elapsed time and failure reason; refreshes every 2 seconds |
| Open directory | Opens Explorer **on the host** (tapping it on a phone still opens it on the host) |
| Access address | The LAN address is shown at the top; tap to copy it and send it to other devices |
| **Trending list** | Top 50 real-time Douyin trends (no login needed); tap an entry to jump straight to search |
| **Keyword search** | Search posts, view engagement metrics, add them to the download queue in one tap (**login required**) |
| **Speech transcription** | Local Whisper transcription, optional SRT subtitles, runs on the GPU (RTX 2070) |
| **Playback** | Tap to play; **decoding happens locally on the phone**; unsupported formats get a conversion entry |
| **Multi-site download** | Bilibili / YouTube / X / TikTok / Instagram and **1750+ sites**; the engine is picked automatically from the link |
| **Two-level sections** | Level 1 "Douyin / Bilibili·other sites", level 2 the features of each section; both levels are draggable, tappable glass sliders |

---

## 5. Security: add an access code

By default it listens on `0.0.0.0` with no password, so anyone on the same LAN can use it. If you want it stricter, or plan to expose the port to the internet, **you must** set an access code first:

1. Stop the service
2. Edit `config.json` and change `"access_code": ""` to something like `"access_code": "mycode123"`
3. Restart; the browser will ask for the code, which is then remembered by that browser

The code is verified through a request header; each phone and computer enters it once.

---

## 6. Making it reachable from outside

Without a public IP on this machine, opening the port directly will not work. The usual approaches, pick one:

- **Cloudflare Tunnel** (free, no public IP needed, simplest): install `cloudflared` and run
  ```
  cloudflared tunnel --url http://127.0.0.1:8790
  ```
  It gives you a `https://xxx.trycloudflare.com` address that any device can open. Set the access code first.
- **frp / nps**: for when you own a server; configure frpc to point at port 8790 on this machine.
- **Router port forwarding**: only viable if your broadband has a public IP, and always set the access code.

All of these need extra installs or router changes, which I did not do on my own — tell me which one you want.

---

## 7. Configuration (config.json)

| Field | Meaning | Default |
|:---|:---|:---|
| `port` | Service port | 8790 |
| `download_path` | Download destination (a path on the host) | `D:\DouyinWeb\downloads` |
| `quality` | Default quality | `highest` |
| `access_code` | Access code; an empty string means none | empty |

Changes require a restart. Changing the save directory or quality on the page writes back to this file automatically, so no manual editing is needed.

---

## 8. Limitations and notes

- Tasks are **held in memory**: 500 entries / 24 hour limit; the list is cleared when the service restarts, downloaded files are unaffected
- Downloads happen **on the host** (this Windows machine); the phone only sends commands and watches progress, so nothing works while the host is off
- Live recording still uses the command line `engine\run.py -u <live room URL>`; the web page only submits the task and shows progress
- **Search** requires scanning the QR code on the host first; trending and transcription do not
- **Transcription** downloads the model automatically the first time a model is used (small ≈ 461MB), cached in `D:\DouyinWeb\whisper-models`, never on the C drive
- **Whether playback works depends only on codec and container, not on host performance**: the host just sends bytes
  - H.264 / MP4: playable on every mobile browser
  - HEVC / H.265: iPhone can play it, most Android Chrome cannot → the page offers "Convert to H.264"
  - MKV / FLV and other containers: mobile browsers do not recognise them → the page offers remuxing
  - Remuxing (`-c copy`) changes only the container, not the codec — sub-second, lossless, but **HEVC stays HEVC**;
    to be truly universal you must re-encode, which scales with the length of the video
- **iOS Safari needs Range partial responses** (206); without them you get a black screen, not "loading". `/api/media` implements this
  and was tested with `bytes=..`, suffix ranges, out-of-range 416 and HEAD
- Concurrency comes from `thread` in `engine\config.yml` (default 5) and cannot be changed from the page
- **The two engines split the work by domain, no manual choice**:
  - Douyin links → the dedicated engine (profiles / collections / live / comments, which yt-dlp cannot do)
  - Other sites → yt-dlp (`D:\yt-dlp\yt-dlp.exe`, or point `YTDLP_PATH` elsewhere)
- yt-dlp progress is **parsed from its real output**, not estimated; video and audio streams download in two passes,
  and percentages are weighted across the passes into a monotonically increasing overall progress, so you never see "62% jump back to 0%"
- To change the yt-dlp version: replace that exe, or point `YTDLP_PATH` at it; no code change needed
- **Both sections share the "Download" panel**: the engine decides from the link, not from the section you are in.
  Pasting a Bilibili link in the Douyin section still works, and vice versa — the section only decides which extra features appear next to it
- The level-2 slider remembers its last position per section (e.g. Douyin stays on "Search" when you switch away and back),
  and a page refresh restores the same section
- The engine is a read-only copy of the original project; to upgrade, pull the new code from the original project over `engine\`
  (keep `config.yml` and the cookie)

---

## 8.5 Fonts (Noto Serif SC)

The page uses **Noto Serif SC** (the Google version of Source Han Serif).

The local `C:\Windows\Fonts\NotoSerifSC-VF.ttf` is 24MB, far too slow to send to a phone,
so it was subset to the 422 characters the page uses:

| File | Size | Purpose |
|:---|---:|:---|
| `static/fonts/NotoSerifSC-subset.woff2` | 170 KB | Preferred by modern browsers |
| `static/fonts/NotoSerifSC-subset.ttf` | 348 KB | Fallback for old browsers |

**Re-run it after editing page text**, otherwise the new characters fall back to the system font:

```powershell
cd D:\DouyinWeb\webapp
python make_font_subset.py
```

The script scans `index.html` for every character that can be displayed (including messages inserted dynamically by JS),
so there is no character list to maintain by hand. The font endpoint is cached for a year, so visitors download it once.

Serif glyphs are smaller and thinner than sans-serif ones, so `font_tune.py` bumps small sizes up one step
(11px→12px, 12px→13px, etc.); otherwise it looks tiny and blurry on a phone.

---

### Section LOGOs

The square in the top-left changes with the section:

| Section | Image | Source |
|:---|:---|:---|
| Douyin | White V-shaped download icon on black | `F:\豆包\GitHub项目下载.png` |
| Bilibili · other sites | Download arrow TV icon on blue | `F:\豆包\GitHub项目下载 (1).png` |

The originals are 1536×1536 and about 2MB, while the LOGO is displayed at only 46px, so they were compressed to 256×256:
`logo-douyin.webp` 5.2KB, `logo-bili.webp` 6KB (with PNG fallbacks at about 20KB each).

To swap the images, re-run:

```powershell
cd D:\DouyinWeb\webapp
python make_logos.py
```

Switching is a **cross-fade between two images**, not a `src` swap — swapping `src` directly causes a white flash before the new image decodes.
Both images stay mounted and only opacity and a slight scale animate, while the outer glow transitions from Douyin red to Bilibili blue.

---

## 8.7 Pasting a whole share message

The "Copy link" button on Douyin / Bilibili / YouTube does not give you a URL but a whole block of text:

```
3.82 复制打开抖音，看看【鸽子蛋的作品】快乐过好每一天 https://v.douyin.com/9M5HtT0FXq8/ O@X.zT wFh:/ :2pm 04/10
```

Using that as a URL always fails (`urlparse` finds no hostname). `webapp/url_clean.py` extracts the link first:

| Input | Extracted result |
|:---|:---|
| A whole Douyin share message | `https://v.douyin.com/9M5HtT0FXq8/` |
| A bare domain without http | `https://` is added automatically |
| Several links in one message | All extracted and deduplicated |
| No link at all | A 400 with a clear explanation on submit |

The page also shows in real time which engine this text will go to, and tells you outright when no link can be found.

## 8.05 Why downloads "fail" (common causes)

**Not being signed in is the most common cause.** Douyin's API rejects it outright:

```
Douyin API HTTP failure: status=403
body='Blocked by ArgusSecurityPlugin Uifid Not Found'
```

This is not a bad link, it is risk control. The page writes the reason on the task card instead of just "Failed":

> Douyin rejected this request (no login state, blocked by risk control). Double-click 登录抖音.bat on the host, scan the QR code to sign in, then try again.

**The other common cause is a dead link** (post deleted / private / region-locked).

---

## 9. Why this is not a "pure HTML file"

Douyin's APIs need local signing, cookies and a direct connection to the IP; requests straight from a browser are blocked by CORS and risk control, so a pure static HTML page cannot get the watermark-free source. Hence this shape: **HTML front end + local server**. The page serves any device, while the actual downloading is done by the Python engine on the host. That is also what every comparable tool (the official desktop app included) does.
