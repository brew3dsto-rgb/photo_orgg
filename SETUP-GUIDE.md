# Immich + NSFW Sidecar Setup Guide
## RTX 4070 Ti Super · CUDA-accelerated · Self-hosted Photo Library

---

## Prerequisites

### Hardware
- NVIDIA RTX 4070 Ti Super (16GB VRAM)
- 8GB+ system RAM (16GB recommended)
- SSD for database (any size, 50GB is plenty)
- Large storage drive for photos (HDD/NAS is fine)

### Software
- Ubuntu 22.04/24.04 or Debian 12
- Docker Engine + Docker Compose plugin
- NVIDIA driver >= 545
- NVIDIA Container Toolkit

---

## Step 1: Install NVIDIA driver + Container Toolkit

```bash
# Check current driver version
nvidia-smi

# If driver < 545, update it:
sudo apt update
sudo apt install -y nvidia-driver-545

# Install NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify GPU is accessible from Docker
docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi
```

---

## Step 2: Create directories

```bash
# App config
sudo mkdir -p /opt/immich

# Photo storage (adjust to your large drive)
sudo mkdir -p /srv/immich/photos
sudo mkdir -p /srv/immich/pgdata

# Set ownership
sudo chown -R $USER:$USER /opt/immich /srv/immich
```

---

## Step 3: Deploy the stack

```bash
# Copy all files from this package to /opt/immich
cp docker-compose.yml /opt/immich/
cp .env.example /opt/immich/.env
cp watcher.py /opt/immich/
cp Dockerfile /opt/immich/

cd /opt/immich

# Edit .env — at minimum, set a strong DB_PASSWORD
nano .env

# Start the stack (first run downloads ~8GB of images + models)
docker compose up -d

# Watch logs to confirm everything starts
docker compose logs -f immich-server
```

---

## Step 4: Initial Immich setup

1. Open `http://YOUR_SERVER_IP:2283`
2. Create your admin account
3. Go to **Administration > Settings**:
   - **Storage Template**: Enable and set to `{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}`
   - **Machine Learning**: Verify it shows GPU/CUDA in the logs
   - **Video Transcoding**: Set Hardware Acceleration to **NVENC**

---

## Step 5: Generate API key for the NSFW sidecar

1. In Immich, click your profile icon → **Account Settings**
2. Go to **API Keys** → **New API Key**
3. Name it `nsfw-sidecar`, grant full permissions
4. Copy the key and paste it into your `.env` file as `NSFW_SIDECAR_API_KEY`
5. Restart the sidecar:

```bash
docker compose restart nsfw-watcher
```

---

## Step 6: Set up users and permissions

### Create users
In **Administration > User Management**, create accounts for each person:
- Each user gets their own private library
- Admin can set storage quotas per user

### Album-based permission model

| User type | Access | Sees NSFW? |
|-----------|--------|------------|
| Admin | Full RW to everything | Yes — shared to NSFW album |
| Partner | RW on shared albums | Yes — if shared NSFW album with them as editor |
| Family/Kids | RO on specific albums | No — never shared to NSFW album |

### How to configure:
1. The sidecar auto-creates an **NSFW** album
2. Share that album **only** with users who should see NSFW content
3. Set those users as **Editor** (RW) or **Viewer** (RO) on the album
4. Create separate SFW albums (e.g., "Family Vacation 2026") and share with everyone
5. Use **Partner Sharing** for users who should see your full (non-NSFW) timeline

---

## Step 7: Install mobile apps

1. Download Immich from App Store / Play Store / F-Droid
2. Server URL: `http://YOUR_SERVER_IP:2283` (or your domain if using reverse proxy)
3. Enable background auto-backup in app settings

---

## Tuning the NSFW sidecar

### Adjusting sensitivity
In `.env`, change `NSFW_THRESHOLD`:
- `0.60` — aggressive, catches more but more false positives
- `0.75` — balanced (default)
- `0.85` — conservative, fewer false positives
- `0.95` — very conservative, only flags obvious content

After changing, restart:
```bash
docker compose restart nsfw-watcher
```

### Checking sidecar logs
```bash
docker compose logs -f nsfw-watcher
```

### Re-scanning existing library
The sidecar tracks what it has already checked via the `nsfw-checked` tag.
To re-scan everything:
1. In Immich, delete the `nsfw-checked` tag (this untags all assets)
2. Restart the sidecar — it will re-process all images

---

## Reverse proxy (recommended for remote access)

### Caddy (simplest)
```
photos.yourdomain.com {
    reverse_proxy localhost:2283
    request_body {
        max_size 50GB
    }
}
```

### Nginx
```nginx
server {
    listen 443 ssl;
    server_name photos.yourdomain.com;

    client_max_body_size 50000M;

    location / {
        proxy_pass http://localhost:2283;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (required for Immich)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## Backup strategy

```bash
#!/bin/bash
# backup-immich.sh — run daily via cron
BACKUP_DIR="/backup/immich"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"

# 1. Database dump (critical — contains all metadata, faces, albums)
docker compose -f /opt/immich/docker-compose.yml exec -T database \
    pg_dumpall -U postgres | gzip > "$BACKUP_DIR/db-${DATE}.sql.gz"

# 2. Photo originals (rsync for incremental)
rsync -a --delete /srv/immich/photos/ "$BACKUP_DIR/photos/"

# 3. Clean old DB dumps (keep 30 days)
find "$BACKUP_DIR" -name "db-*.sql.gz" -mtime +30 -delete

echo "Backup complete: $(date)"
```

---

## GPU resource usage summary

Your RTX 4070 Ti Super (16GB VRAM) handles all three GPU workloads:

| Service | VRAM usage | When active |
|---------|-----------|-------------|
| Immich ML (face detection + CLIP) | ~2-4 GB | During library scan, new uploads |
| Immich transcoding (NVENC) | ~500 MB | When processing video |
| NSFW classifier (Falconsai) | ~500 MB-1 GB | When new images uploaded |

Total peak: ~5-6 GB — well within your 16GB budget. The GPU is idle most of
the time and only spins up when new content arrives or jobs run.
