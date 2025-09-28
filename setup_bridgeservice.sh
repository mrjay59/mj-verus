#!/data/data/com.termux/files/usr/bin/bash
# setup_bridgeservice.sh
# Script otomatis untuk setup environment Termux + dependencies bridge service

echo "[*] Update & upgrade Termux packages..."
pkg update -y && pkg upgrade -y

echo "[*] Install dependencies (Python, pip, OpenSSL, libffi, clang, git)..."
pkg install -y python python-pip openssl libffi clang make git

echo "[*] Reinstall python jika diperlukan..."
pkg uninstall -y python
pkg install -y python

echo "[*] Upgrade pip, setuptools, wheel..."
python -m pip install --upgrade pip setuptools wheel

echo "[*] Install library Python yang dibutuhkan..."
python -m pip install websocket-client adbutils

echo "[*] Buat folder project bridgeservice..."
mkdir -p ~/bridgeservice

echo "[*] Setup selesai. Cek instalasi SSL..."
python - <<'EOF'
import ssl
print("✅ Modul SSL tersedia:", ssl.OPENSSL_VERSION)
EOF

echo "[*] Jalankan project di folder ~/bridgeservice"
echo "cd ~/bridgeservice"

