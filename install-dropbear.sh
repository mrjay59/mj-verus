#!/data/data/com.termux/files/usr/bin/bash
# ============================================
# Installer Dropbear + auto-start via startup.sh
# ============================================

PREFIX="/data/data/com.termux/files/usr"
HOME="/data/data/com.termux/files/home"
LOG_DIR="$HOME/logs"

mkdir -p "$LOG_DIR"

echo "=== [INSTALL] Dropbear SSH Server ==="

# 1. Install dropbear
pkg update -y
pkg install dropbear -y

# 2. Set password kalau belum
echo "Silakan buat password untuk user Termux:"
passwd

# 3. Pastikan folder .ssh ada
mkdir -p ~/.ssh
chmod 700 ~/.ssh

# 4. Tambah contoh authorized_keys (opsional)
if [ ! -f ~/.ssh/authorized_keys ]; then
    echo "# Masukkan public key di sini" > ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
fi

# 5. Tambah baris ke startup.sh biar auto jalan
STARTUP_SCRIPT="$HOME/.termux/boot/startup.sh"
if ! grep -q "dropbear -E -F -p 8022" "$STARTUP_SCRIPT" 2>/dev/null; then
    echo "" >> "$STARTUP_SCRIPT"
    echo "# Auto-start Dropbear SSH server" >> "$STARTUP_SCRIPT"
    echo "$PREFIX/bin/dropbear -E -F -p 8022 >> $LOG_DIR/dropbear.log 2>&1 &" >> "$STARTUP_SCRIPT"
    echo "[INFO] Dropbear ditambahkan ke $STARTUP_SCRIPT"
fi

echo "=== [DONE] Dropbear siap jalan di port 8022 ==="
echo "Cek log di: $LOG_DIR/dropbear.log"
