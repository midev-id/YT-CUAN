# YT-CUAN

Live stream YouTube 24/7 dari library video + musik, dikelola dari dashboard web.
Satu ffmpeg per stream key (copy, ringan CPU), broadcast dibuat otomatis lewat YouTube Data API:
judul bergilir, deskripsi spintax, thumbnail acak, beberapa live per key, reconnect otomatis.
Spesifikasi lengkap: [PRD.md](PRD.md).

## Pasang di VPS

Satu VPS = satu akun Google. Butuh Linux x86-64 (Ubuntu 22.04+ disarankan). Bandwidth upload menentukan
jumlah live: kira-kira jumlah key × (bitrate video + 0,2 Mbps), jadi 9 key 1080p ≈ 60 Mbps.

Di hub (https://ytcuan.autopreneur.id) klik **Tambah VPS**, salin perintahnya, lalu jalankan di VPS sebagai root:

```bash
curl -fsSL https://ytcuan.autopreneur.id/install | sudo bash -s -- <KODE>
```

Installer memasang Docker (kalau belum ada), membuat `/opt/yt-cuan/.env` dengan password dan kunci enkripsi
acak, menjalankan agent, lalu memasangkannya ke hub. Tanpa kode, agent jalan mandiri dan bisa dipasangkan
nanti dengan `yt-cuan pair <KODE>`. Menjalankan installer lagi aman: data dan `.env` tidak disentuh.

Perintah di VPS: `yt-cuan status | logs | update | restart | pair <KODE> | password`.
Semua data ada di `/opt/yt-cuan/data`, jadi folder itu yang di-backup.

Dashboard lokal hanya mendengar di `127.0.0.1` dan dibuka lewat SSH tunnel:
`ssh -L 8000:localhost:8000 root@<ip-vps>`, lalu buka http://localhost:8000. Karena alamatnya `localhost`,
login Google dengan client tipe Desktop juga jalan dari situ.

Image agent dibangun otomatis oleh GitHub Actions (`.github/workflows/image.yml`) ke
`ghcr.io/<owner>/yt-cuan-agent` setiap push ke `main`. Package itu harus diset **Public** di GitHub
(Package settings → Change visibility) supaya VPS bisa menarik tanpa login; repo kode tetap privat.

## Pasang di VPS Windows

Windows Server 2019+ / Windows 10+ 64-bit, tanpa Docker. Masuk lewat Remote Desktop, buka PowerShell sebagai
Administrator, lalu tempel perintah dari halaman **Tambah server** di hub:

```powershell
irm https://ytcuan.autopreneur.id/install.ps1 | iex; Install-YtCuan <KODE>
```

Semua ada di `C:\YT-CUAN` (`.env`, `data\`, `ffmpeg\`, `logs\`). Agent jalan sebagai scheduled task
**YT-CUAN Agent** (akun SYSTEM, mulai saat boot, restart sendiri kalau crash). Ikon di pojok taskbar
menunjukkan status (merah = ada live, hijau = jalan, kuning = perlu dicek, abu = agent mati) dan berisi
menu buka dashboard, lihat password, mulai ulang, dan perbarui. Dashboard lokal: shortcut
**YT-CUAN Dashboard** di desktop (`http://localhost:8000`). Installer juga mengatur jam aktif Windows Update
05.00–23.00 supaya restart otomatis tidak terjadi siang hari.

Perintah: `yt-cuan status | restart | update | pair <KODE> | password | uninstall`.

Rilis: tag `v*` → GitHub Actions (`windows.yml`) membangun `yt-cuan-agent-win64.zip` dan melampirkannya ke
GitHub Release. Karena repo privat, file itu disalin ke hub (`hub/data/dist/`) supaya installer bisa
mengunduh dari `https://ytcuan.autopreneur.id/download/yt-cuan-agent-win64.zip`.

## Siapkan Google Cloud (sekali)

1. Buka https://console.cloud.google.com, buat project baru.
2. **APIs & Services → Library**: aktifkan **YouTube Data API v3**.
3. **OAuth consent screen**: tipe *External*, isi nama app + email. Di bagian *Test users*,
   tambahkan email Google pemilik tiap channel. Selama status *Testing*, token kedaluwarsa
   tiap 7 hari. Untuk pemakaian jangka panjang, ajukan *Publish app*.
4. **Credentials → Create credentials → OAuth client ID**, tipe *Web application*.
   *Authorized redirect URI*: `<BASE_URL>/oauth/callback`, contoh `https://yt.contoh.com/oauth/callback`.
5. Salin Client ID & Client secret ke `.env`, **atau** taruh file `client_secrets.json` yang diunduh
   dari Google Cloud ke folder `data/` (file dari tool lama juga bisa dipakai).

Sudah punya `token.pickle` dari tool lama? Klik **Impor token.pickle** di halaman utama; channel
langsung terhubung tanpa login ulang. Client tipe *Desktop* (seperti file tool lama) hanya bisa login
lewat `http://localhost`, jadi di VPS pakai impor token.pickle atau buat client tipe *Web*.

Kuota default 10.000 unit/hari per project, dibagi semua channel. Satu siklus broadcast
memakai ≈ 300 unit, jadi ≈ 30 siklus/hari. Pemakaian tampil di dashboard.

## Alur pakai

1. **Hubungkan channel** di halaman utama, lalu login dengan akun pemilik channel.
2. Di YouTube Studio, buat stream key **permanen** (misalnya 3 per channel), lalu klik **Sinkron dari YouTube**.
3. Upload **video**, **audio**, dan **thumbnail**. Tiap file otomatis dinormalisasi:
   video ke H.264 30fps tanpa audio, audio ke AAC 192k 48 kHz stereo, thumbnail ke JPG 1280×720 ≤ 2 MB.
   Tunggu sampai status `ready`.
4. Isi **judul** (satu per baris) dan **template deskripsi** (spintax `{a|b|c}`).
5. Atur durasi live, live per key, privacy, dan lainnya, lalu **Start** tiap key.
   ffmpeg mulai mengirim, dan begitu YouTube menerima stream, slot broadcast dibuat sampai penuh.

Kalau live sering blank/buffering padahal bandwidth cukup, ganti bitrate ke **CBR**
(library akan di-encode ulang).

## Pengembangan lokal

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
python -m tests.test_core          # self-check: transcode, stream engine, siklus slot (butuh ffmpeg)
ADMIN_PASSWORD=x SECRET_KEY=... BASE_URL=http://localhost:8000 .venv/bin/uvicorn app.main:app --reload
```

## Struktur

| File | Isi |
|---|---|
| `app/main.py` | FastAPI: dashboard, OAuth, aksi |
| `app/transcode.py` | Antrian normalisasi upload |
| `app/streamer.py` | Supervisor ffmpeg per key, reconnect, health |
| `app/orchestrator.py` | Siklus broadcast: buat → live → akhiri, Filter View |
| `app/youtube.py` | YouTube Data API + catatan kuota |
| `app/db.py` | Skema SQLite |
