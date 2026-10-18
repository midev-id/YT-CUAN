#!/usr/bin/env bash
# YT-CUAN agent installer (PRD §13.2). Shown by the hub with a one-time pairing code:
#   curl -fsSL https://ytcuan.autopreneur.id/install | sudo bash -s -- <KODE>
# Without a code the agent runs standalone; pair later with: yt-cuan pair <KODE>
# Safe to run again: keeps .env and data, only refreshes the image and compose file.
set -euo pipefail

CODE="${1:-}"
IMAGE="${YTCUAN_IMAGE:-ghcr.io/midev-id/yt-cuan-agent:latest}"
HUB_URL="${YTCUAN_HUB:-https://ytcuan.autopreneur.id}"
DIR=/opt/yt-cuan

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mGagal: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "jalankan sebagai root (tambahkan sudo)."
[ "$(uname -m)" = x86_64 ] || die "butuh VPS x86-64 (sekarang: $(uname -m))."

if ! command -v docker >/dev/null; then
  say "Memasang Docker…"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 tidak ada. Pasang paket docker-compose-plugin."
systemctl enable --now docker >/dev/null 2>&1 || true

mkdir -p "$DIR/data"
cd "$DIR"

if [ ! -f .env ]; then
  say "Membuat konfigurasi…"
  umask 077
  cat > .env <<EOF
# Dibuat installer $(date -u +%Y-%m-%dT%H:%MZ). SECRET_KEY jangan diganti: token channel tidak bisa dibaca lagi.
SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')
ADMIN_USER=admin
ADMIN_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
BASE_URL=http://localhost:8000
HUB_URL=$HUB_URL
EOF
fi

# Dashboard lokal hanya di 127.0.0.1: buka lewat SSH tunnel (lihat ringkasan di akhir). Kontrol harian lewat hub.
cat > compose.yml <<EOF
services:
  agent:
    image: $IMAGE
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./data:/data
    stop_grace_period: 20s
    logging:
      options: { max-size: "10m", max-file: "3" }
EOF

say "Mengunduh aplikasi…"
docker compose pull -q

if [ -n "$CODE" ]; then
  say "Memasangkan ke hub…"
  docker compose run --rm --no-deps agent python -m app.pair "$CODE" || die "kode pairing salah atau kedaluwarsa. Buat kode baru di hub, lalu: yt-cuan pair <KODE>"
fi

docker compose up -d

# "Perbarui" from the hub: the agent (inside Docker) drops data/update-requested, the host runs the update.
cat > /etc/systemd/system/yt-cuan-update.path <<'EOF'
[Unit]
Description=YT-CUAN: update requested by the hub
[Path]
PathExists=/opt/yt-cuan/data/update-requested
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/yt-cuan-update.service <<'EOF'
[Unit]
Description=YT-CUAN: update agent
[Service]
Type=oneshot
ExecStartPre=/bin/rm -f /opt/yt-cuan/data/update-requested
ExecStart=/usr/local/bin/yt-cuan update
EOF
systemctl daemon-reload
systemctl enable --now yt-cuan-update.path >/dev/null
touch data/.host-updater

cat > /usr/local/bin/yt-cuan <<'EOF'
#!/usr/bin/env bash
# Perintah harian YT-CUAN agent.
set -euo pipefail
cd /opt/yt-cuan
case "${1:-}" in
  update)   docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null && echo "Sudah versi terbaru." ;;
  restart)  docker compose restart ;;
  logs)     docker compose logs -f --tail=200 ;;
  status)   docker compose ps; curl -fsS http://127.0.0.1:8000/healthz && echo ;;
  pair)     [ -n "${2:-}" ] || { echo "Pakai: yt-cuan pair <KODE>"; exit 1; }
            docker compose exec agent python -m app.pair "$2" && docker compose restart ;;
  password) grep -E '^ADMIN_(USER|PASSWORD)=' .env ;;
  *) echo "Pakai: yt-cuan {status|logs|update|restart|pair <KODE>|password}" ;;
esac
EOF
chmod +x /usr/local/bin/yt-cuan

say "Menunggu aplikasi siap…"
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 || die "aplikasi tidak menyala. Cek: yt-cuan logs"

IP=$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
say "YT-CUAN terpasang."
if [ -n "$CODE" ]; then
  echo "Server ini sudah muncul di hub: $HUB_URL"
else
  echo "Belum dipasangkan ke hub. Buat kode di $HUB_URL lalu jalankan: yt-cuan pair <KODE>"
fi
cat <<EOF

Dashboard lokal (cadangan, kalau hub tidak bisa dibuka):
  1. Di komputer Anda:  ssh -L 8000:localhost:8000 root@$IP
  2. Buka http://localhost:8000  (user dan password: yt-cuan password)

Perintah: yt-cuan status | logs | update | restart | pair <KODE> | password
EOF
