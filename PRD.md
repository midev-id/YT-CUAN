# PRD — YT-CUAN

Status: agent v1 dibangun · hub belum · draft v0.3 · 2026-10-15 · pemilik: mahdi

> **v0.3:** YT-CUAN jadi dua bagian, yaitu **agent** di setiap VPS (aplikasi v1 yang sudah ada) dan **hub** pusat di
> `ytcuan.id` untuk memantau dan mengontrol banyak VPS. Satu VPS = satu akun Google/YouTube. Lihat §13.
> Bagian §1–§12 tetap berlaku untuk agent, kecuali yang diubah §13.

## 1. Ringkasan

YT-CUAN adalah satu aplikasi server untuk menjalankan banyak live stream YouTube 24/7 dari
library video dan musik. Aplikasi ini menggabungkan dua tool lama yang sekarang dipakai tim:

- **YTMmusic v3.4**: mengirim video dan musik acak ke RTMP YouTube lewat ffmpeg.
- **YTMAuto / YTMSSv4**: membuat broadcast lewat YouTube Data API (judul, deskripsi,
  thumbnail, go-live, cek penonton).

Tool lama berupa exe Windows berlisensi pihak ketiga (omahbot.com). Masalahnya: crash saat RAM
penuh, tidak bisa dipantau dari jauh, dan bergantung pada server lisensi orang lain.
YT-CUAN berjalan di VPS (Docker) dan dikontrol dari browser.

## 2. Tujuan & bukan tujuan

**Tujuan v1**
- Satu dashboard web untuk mengelola channel, stream key, media, judul, dan live, durasi live.
- Mudah dipasang: `git clone` → isi `.env` → `docker compose up -d`.
- Stabil 24/7: reconnect otomatis, pulih sendiri setelah restart server, tidak ada proses yatim.
- Mendukung banyak channel. Polanya 1 channel = 3 key, 1 key = maks 3 live, jadi 9 live per channel.

**Bukan tujuan v1** (desain jangan menghalangi, tapi tidak dibangun dulu)
- Sistem lisensi, billing, multi-tenant (rencana: internal dulu, jual nanti).
- Notifikasi WA/Telegram (v1 cukup di dashboard).
- Jadwal jam per key dan mode impor JSON/CSV dari tool lama.
- Banyak mesin worker (v1: satu VPS).

## 3. Pengguna

Operator internal tim Autopreneur. Mereka mengurus beberapa channel musik (contoh nama:
GARASI02, LIONROOT, Smooth Sax Lounge), mengunggah video dan musik, mengisi daftar judul,
lalu membiarkan sistem berjalan.

## 4. Konsep & istilah

| Istilah | Arti |
|---|---|
| **Channel** | Akun YouTube yang sudah login OAuth ke YT-CUAN. |
| **Stream key** | Ingest RTMP milik channel (`liveStream` di API). Satu proses ffmpeg per key. |
| **Broadcast** | Satu "video live" di YouTube (`liveBroadcast`). Satu key bisa dipakai beberapa broadcast. |
| **Slot** | Satu broadcast aktif di satu key. Maks slot per key = *Duplicate per Key* (default 3). |
| **Media library** | Video dan audio milik channel, sudah dinormalisasi saat upload. |
| **Title pool** | Daftar judul per channel, dirotasi. |
| **Description template** | Teks dengan spintax `{a|b|c}`. |

## 5. Fitur v1

### F1. Channel & OAuth
- Satu Google Cloud project (client_secret di `.env`) untuk banyak channel.
- Tombol "Hubungkan channel" menjalankan alur OAuth web dengan scope `youtube.force-ssl`.
  Refresh token disimpan terenkripsi per channel.
- Daftar stream key ditarik dari `liveStreams.list(mine=true)` (tombol *Sinkron*). Key baru dibuat di
  YouTube Studio lalu disinkron; tidak ada input key manual, karena broadcast butuh ID stream dari API
  untuk `bind`. Key yang dipakai harus berupa key permanen (reusable).
- Status token tampil di dashboard (valid / perlu login ulang).

### F2. Media library + normalisasi saat upload
Upload lewat dashboard ke disk VPS per channel. Setiap file **langsung di-transcode di
background** ke format seragam, supaya saat live cukup memakai `-c copy` (ringan di CPU) dan
concat antar file aman:

| Parameter | Nilai |
|---|---|
| Video | H.264 (libx264), `yuv420p`, profile high, 30 fps konstan |
| Resolusi | pilihan per channel: 720p / 1080p (default 1080p), skala + pad, rasio dijaga |
| GOP | keyframe tiap 2 detik (`-g 60 -keyint_min 60 -sc_threshold 0`), sesuai syarat YouTube |
| Rate control | **VBR** (default): CRF/capped, mis. `-crf 21 -maxrate 6000k -bufsize 12000k` · **CBR** (cadangan bila live tampil blank/buffering): `-b:v = -minrate = -maxrate`, `-bufsize 2×`, `nal-hrd=cbr` |
| Audio | AAC-LC **192 kbps**, 48 kHz, stereo |
| Container | MP4 dengan `+faststart` |

- File audio saja (mp3/wav/flac/m4a) dinormalisasi ke AAC 192k 48 kHz stereo (.m4a).
- Mode VBR/CBR dipilih per channel. Mengganti mode akan me-*re-encode* library di background.
- File asli dihapus setelah normalisasi berhasil, untuk hemat disk (opsi ini bisa dimatikan).
- Status per file: antri → proses (persen) → siap / gagal (stderr ffmpeg disimpan).
- Antrian transcode dibatasi N job bersamaan (default 1), supaya live yang sedang jalan tidak kekurangan CPU.

### F3. Stream engine (pengganti YTMmusic)
- Per key: pilih N video dan M audio acak dari library channel, lalu buat concat list, lalu jalankan:
  `ffmpeg -re -stream_loop -1 -f concat -i videos.txt -stream_loop -1 -f concat -i audios.txt -map 0:v -map 1:a -c copy -f flv rtmp://a.rtmp.youtube.com/live2/<key>`
- Opsi "kunci pilihan" (locked selection): set file acak yang sama dipakai lagi setelah restart.
- Satu proses ffmpeg per key. Pengecekan di DB dan proses mencegah key yang sama jalan dua kali.
- **Auto-reconnect**: ffmpeg mati → tunggu backoff (5s, 10s, 30s, 60s, lalu 60s) → jalankan ulang.
  Setelah X gagal beruntun (default 10), key ditandai *error* dan dihentikan.
- Kalau ffmpeg mati < 5 detik setelah start, dianggap error input dan ditampilkan sebagai error konfigurasi.
- Health monitor membaca output `-progress` ffmpeg (fps, bitrate, speed). Speed < 0.9 berarti
  bandwidth atau CPU kurang, dan ditandai *degraded*.
- Saat container restart, semua key yang statusnya *running* dijalankan lagi otomatis.
- Bagian kecil dari log stderr ffmpeg disimpan per key, rotasi otomatis (log tidak boleh tumbuh tanpa batas).

### F4. Broadcast orchestrator (pengganti YTMAuto)
Alur satu slot:
1. Ambil judul berikutnya dari title pool, render deskripsi dari spintax, pilih thumbnail acak.
2. `liveBroadcasts.insert` (privacy sesuai setting, default public), lalu `bind` ke stream key.
3. `thumbnails.set`. Kalau gagal, lanjut tanpa thumbnail dan catat warning.
4. Tunggu `liveStreams` berstatus *active* (ffmpeg dari F3 sudah mengirim data). Beri jitter acak
   (default 5–30 detik). Lalu `transition` → `live`, atau pakai `enableAutoStart` bila memungkinkan.
5. Live berjalan selama *durasi siklus* (default 12 jam; 0 = tanpa batas).
6. Akhir siklus: `transition → complete`. Slot dibuat lagi dengan judul baru (kalau *Change Title = Yes*)
   atau judul yang sama.
- **Duplicate per Key**: maks broadcast aktif per key (default 3).
- **Batas aktif global**: maks broadcast aktif di seluruh sistem / per channel.
- **Delay antar job**: jeda antar pembuatan broadcast supaya tidak membanjiri API.
- **Hapus judul setelah dipakai** (opsional). Kalau tidak, judul dirotasi berputar.
- Broadcast yang gagal dibuat → dihapus (`liveBroadcasts.delete`) supaya tidak menumpuk draft.
- Key gagal N kali beruntun → slot key itu dijeda sampai di-reset manual.
- Error kuota/rate limit → backoff eksponensial. Kalau kuota harian habis, orchestrator berhenti
  membuat broadcast baru sampai kuota reset (tengah malam Pacific Time). Live yang sudah berjalan tetap jalan.

### F5. Filter View (opsional, default mati)
- Di akhir siklus, cek `liveBroadcasts.list(part=statistics).concurrentViewers`.
  - Viewer > 0 → siklus diperpanjang satu periode lagi.
  - Viewer = 0 → broadcast diakhiri dan dibuat broadcast baru.
- Pengaman wajib: durasi live minimum sebelum boleh diakhiri, dan **maks restart per key per hari**.
  Restart live sepi dengan judul berganti massal berisiko melanggar kebijakan spam YouTube (lihat §9).

### F6. Konten: judul & deskripsi
- Title pool per channel: editor teks di dashboard (satu judul per baris) + upload .txt.
  Hitungan sisa judul selalu tampil.
- Description template per channel, dengan pratinjau hasil spintax. Mendukung `{a|b}` bertingkat.
- Folder thumbnail per channel (jpg/png ≤ 2 MB, 16:9). Thumbnail di atas 2 MB dikompres otomatis.
- Pengaturan default per channel: kategori (Music), privacy, tag, *made for kids = false*.

### F7. Dashboard
- **Ringkasan**: jumlah live aktif, key running/error, pemakaian kuota API hari ini (estimasi),
  bandwidth upload, CPU, disk.
- **Per channel**: key, slot aktif (judul, sejak kapan, viewer terakhir, link YouTube), tombol start/stop per key dan per slot.
- **Log event**: live sukses, error, reconnect, key dijeda, kuota habis.
- Login dashboard: satu akun admin (username + password di `.env`) untuk v1.

## 6. Kebutuhan non-fungsional

- **Instalasi**: repo GitHub publik/privat. `docker compose up -d` cukup untuk menjalankan app + ffmpeg.
  README berisi langkah membuat Google Cloud project + OAuth client.
- **Platform**: Linux x86-64 (Ubuntu 22.04+). Docker image berisi ffmpeg statis.
- **Penyimpanan**: SQLite (satu file, volume Docker), media di volume `./data/media`.
- **Kapasitas**: 1 VPS harus kuat 9 key (3 channel × 3 key) dengan `-c copy`. Kebutuhan
  utama adalah bandwidth upload: ≈ jumlah key × (bitrate video + 192 kbps). Contoh 9 key × 6.5 Mbps ≈ 60 Mbps sustained.
- **Ketahanan**: restart container/VPS tidak menghentikan live permanen. Semua state ada di DB.
- **Keamanan**: refresh token & stream key dienkripsi (kunci dari `.env`). Stream key tidak pernah
  ditulis utuh ke log. Dashboard hanya lewat HTTPS (reverse proxy / Cloudflare tunnel).
- **Waktu**: semua jadwal dalam Asia/Jakarta, reset kuota mengikuti Pacific Time.

## 7. Arsitektur (garis besar)

```
Browser ──HTTPS──> FastAPI (dashboard + REST + OAuth callback)
                     │
                     ├── SQLite (channel, key, slot, media, judul, event)
                     ├── Transcode worker   (antrian ffmpeg normalisasi upload)
                     ├── Stream supervisor  (1 ffmpeg/key, reconnect, health)
                     └── Broadcast orchestrator (siklus slot via YouTube Data API)
```
Satu proses Python (asyncio). ffmpeg sebagai subprocess. Tanpa Redis/Celery di v1.
Kalau nanti butuh banyak mesin, supervisor dipisah jadi worker.

## 8. Anggaran kuota YouTube Data API

Default 10.000 unit/hari per Google Cloud project, **dibagi semua channel**.
Perkiraan biaya: tiap call tulis (insert, bind, transition, thumbnails.set, delete) ≈ 50 unit,
list ≈ 1 unit.

| Kejadian | Unit (perkiraan) |
|---|---|
| Satu siklus broadcast (insert + bind + thumbnail + videos.update kategori/tag + transition live + transition complete) | ≈ 300 |
| Polling status/viewer | ≈ 1 per call |

→ **≈ 30 siklus broadcast per hari per project.** Dengan 9 slot dan siklus 12 jam, butuh 18 siklus/hari
(aman). Tapi Filter View atau siklus pendek bisa cepat menghabiskan kuota. Dashboard wajib menampilkan
sisa kuota estimasi. Dari sini, *durasi siklus minimum* dan *maks restart/hari* ditentukan.
Kalau perlu lebih, ajukan kenaikan kuota ke Google atau pakai project tambahan (v2).

## 9. Risiko

| Risiko | Mitigasi |
|---|---|
| Kebijakan YouTube: konten berulang/spam (judul rotasi massal, restart live sepi, musik yang sama di banyak live) bisa membuat channel kena strike/terminasi | Filter View default mati + batas restart. Siklus panjang. Pakai media dengan hak cipta yang jelas. Rekomendasi: tiap channel punya library berbeda |
| 1 stream key dipakai 3 broadcast: perilaku API perlu diverifikasi | Spike teknis di minggu pertama (§11) |
| Kuota API habis | §8 + backoff + live berjalan tetap jalan |
| Bandwidth VPS kurang → buffering/blank | Health monitor (speed, bitrate). Opsi CBR. Rekomendasi spesifikasi VPS di README |
| Transcode berat menurunkan live | Batasi antrian transcode, nice/ionice ffmpeg transcode |

## 10. Metrik keberhasilan

- Uptime live per key ≥ 99% per minggu (di luar gangguan YouTube).
- 0 crash aplikasi yang butuh intervensi manual selama 7 hari berjalan.
- Waktu setup channel baru (OAuth + upload + judul + start) < 15 menit.
- Pemakaian kuota API harian < 80% batas.

## 11. Rencana rilis

1. **Spike** (1–2 hari): verifikasi 1 key → 3 broadcast, `enableAutoStart` vs transition manual, biaya
   kuota nyata per siklus.
2. **M1**: OAuth multi-channel + media upload & normalisasi + stream engine + reconnect.
3. **M2**: Broadcast orchestrator (judul, spintax, thumbnail, duplicate per key, batas aktif, delay).
4. **M3**: Filter View + dashboard ringkasan & kuota + hardening (restart recovery).
5. **v2 (nanti)**: jadwal per key, notifikasi WA, multi-tenant + lisensi, multi worker.

## 12. Keputusan (dulu pertanyaan terbuka)

1. Durasi live default **12 jam**, bisa diubah per channel; 0 = tanpa akhir.
2. Per key default **20 video + 20 audio** acak dari library, bisa diubah per key.
3. Slot pada key yang sama menampilkan isi yang sama (satu sumber ffmpeg). Diterima.
4. Privacy default **public**, bisa diubah per channel.
5. Impor data dari tool lama **tidak** masuk v1. Judul bisa ditambah lewat upload .txt.
6. Repo GitHub: belum diputuskan (ditanyakan saat push).
7. Video hasil normalisasi **tanpa audio**: suara selalu dari library audio. Setiap key wajib punya
   minimal 1 video dan 1 audio yang siap.

## 13. Hub & multi-VPS (v0.3)

### 13.1 Bentuk produk
```
                 ytcuan.id (hub)  <- user login Google
                /       |          agent VPS-1     agent VPS-2     agent VPS-3     (1 akun YouTube per VPS)
```
- **Agent** = aplikasi §5 (stream engine, orchestrator, transcode). Tetap bisa jalan mandiri; dashboard lokalnya
  jadi cadangan. Live tidak berhenti kalau hub mati.
- **Hub** = website pusat. User login dengan Google, melihat semua VPS miliknya, dan **mengontrol penuh** tiap
  VPS (semua yang bisa dilakukan di dashboard agent).
- **Satu VPS = satu akun Google.** Setup agent tidak lagi menawarkan multi-akun. Kalau akun itu punya beberapa
  channel (brand account), tiap channel tetap login sendiri, tapi semuanya di VPS yang sama.

### 13.2 Menambahkan VPS (pairing)
1. Di hub, user klik **Tambah VPS**. Hub membuat kode pairing sekali pakai (berlaku 30 menit) dan menampilkan
   satu perintah: `curl -fsSL https://ytcuan.id/install | bash -s -- <KODE>`.
2. Script install di VPS: pasang Docker bila belum ada, tarik image agent, buat `.env` (SECRET_KEY, password
   dashboard lokal acak), daftarkan service yang hidup lagi setelah reboot, tukar kode pairing jadi
   **token agent** permanen, lalu cetak hasilnya.
3. VPS muncul di hub dalam hitungan detik. Token agent bisa dicabut dari hub (VPS terputus, live tetap jalan).

Update agent: tombol **Perbarui** di hub (agent menarik image baru dan restart sendiri) atau `yt-cuan update`
di VPS.

### 13.3 Koneksi agent <-> hub
- **Agent yang memanggil keluar** lewat WebSocket `wss://ytcuan.id/agent` dengan token agent. VPS tidak perlu
  domain, HTTPS, atau port terbuka, dan tetap jalan di belakang NAT. Putus -> sambung ulang dengan backoff.
- **Status** (agent -> hub): snapshot tiap 10 detik dan langsung saat ada perubahan: key (status, speed),
  broadcast (judul, sejak, viewer), media (status, progres), kuota, CPU, disk, bandwidth, versi, event baru.
- **Perintah** (hub -> agent): RPC `{id, action, params}` -> `{id, ok, result|error}`. Satu daftar aksi yang sama
  dipakai dashboard lokal dan hub (start/stop key, config key, akhiri live, pengaturan, judul, media, sinkron
  key, log). Agent memvalidasi semua parameter sendiri; hub tidak dipercaya buta.
- Hub hanya menyimpan cache status terakhir untuk ditampilkan. Sumber kebenaran tetap DB di agent.

### 13.4 Login YouTube & kuota
- **User membawa Google Cloud project sendiri** (`client_secrets.json`), dipandu langkah demi langkah di hub.
  Alasannya: kuota 10.000 unit/hari per project (§8) tidak dibagi antar pelanggan, dan aplikasi tidak perlu
  verifikasi Google untuk scope sensitif `youtube.force-ssl`.
- Satu project bisa dipakai untuk beberapa VPS milik user yang sama (kuota dibagi), atau satu project per VPS
  (kuota penuh per VPS). Hub menampilkan pemakaian kuota per project.
- Login channel dilakukan di hub (domain HTTPS, jadi client tipe **Web** dengan redirect
  `https://ytcuan.id/oauth/callback`). Hub menukar kode jadi token, mengirim refresh token ke agent tujuan lewat
  WebSocket, lalu **tidak menyimpannya**. Token hanya ada di agent (terenkripsi).
- Client tipe Desktop (file tool lama) tetap didukung lewat cara "salin link": login, lalu tempel alamat
  `localhost` yang gagal dibuka ke kotak di hub.
- Nanti (v2), setelah lolos verifikasi Google dan kuota dinaikkan: opsi "login satu klik" pakai project
  milik ytcuan.id.

### 13.5 Media ke VPS
File GB tidak lewat hub.
- **Utama: impor dari link.** User menempel link Google Drive (file/folder berbagi publik) atau URL langsung.
  Agent mengunduh sendiri, lalu masuk antrean normalisasi (§F2). Progres tampil di hub.
- **Cadangan: unggah langsung ke VPS.** Halaman hub mengunggah file langsung ke agent lewat HTTPS
  `https://<ip-vps>.sslip.io` (Caddy, sertifikat otomatis), memakai token unggah sekali pakai dari hub.

### 13.6 Hub: akun & tampilan
- Login Google (OpenID, hanya email/nama). Satu user = banyak VPS. Belum ada tim/role di v1 hub.
- **Beranda**: semua VPS dalam satu layar, tiap VPS satu baris: channel, live aktif, key bermasalah,
  kuota, bandwidth, terakhir terlihat. VPS offline ditandai jelas.
- **Halaman VPS**: sama dengan dashboard agent (key, broadcast, judul, pengaturan, media, log) plus
  panduan setup (Google Cloud -> login channel -> stream key -> konten -> mulai live).
- Notifikasi tetap di dashboard (WA/Telegram nanti).

### 13.7 Keamanan
- Token agent disimpan sebagai hash di hub. Semua koneksi lewat TLS.
- Kebocoran hub tidak membocorkan token YouTube atau stream key (keduanya hanya di agent).
- Aksi destruktif dari hub dicatat (siapa, kapan, VPS mana) di log event agent.

### 13.8 Stack hub
FastAPI + PostgreSQL (atau SQLite di awal), satu container, di VPS Autopreneur di belakang Cloudflare.
Template dan komponen UI dipakai bersama dengan agent.

### 13.9 Rencana rilis (menggantikan §11 untuk bagian yang belum dibangun)
1. **H1** Agent: lapisan aksi bersama (dipakai route lokal + RPC), klien WebSocket, script install + pairing,
   mode satu akun.
2. **H2** Hub: login Google, tambah/cabut VPS, beranda status, kontrol penuh lewat RPC.
3. **H3** Login YouTube lewat hub (Web + salin link), impor media dari link, unggah langsung via sslip.io.
4. **H4** Update agent dari hub, pemakaian kuota per project.
5. Spike §11.1 (1 key -> 3 broadcast, biaya kuota nyata) tetap wajib, bisa paralel dengan H1.

### 13.10 Keputusan (2026-10-15)
1. Dijual berlangganan: **ya, nanti**. Paket, pembayaran, dan trial dirancang setelah hub jalan.
2. Domain hub: **`ytcuan.autopreneur.id`** (bukan `ytcuan.id`; contoh di atas dibaca dengan domain ini).
3. Repo kode **privat**. Image agent di **GitHub Container Registry, package publik** selama fase internal
   (VPS menarik tanpa login). Saat dijual: package privat, kredensial tarik dibagikan hub saat pairing.
4. Dashboard lokal agent hanya di `127.0.0.1`, dibuka lewat SSH tunnel. HTTPS publik di VPS (sslip.io) baru
   dipasang di H3 untuk unggah langsung.
