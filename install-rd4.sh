#!/data/data/com.termux/files/usr/bin/bash
# === INSTALL SCRIPT AUTO STARTUP + WIFI/HOTSPOT + MINER + DISABLE APP ===

echo "[INFO] Buat direktori service.d dan logs..." 
mkdir -p $HOME/logs

# --- 1. Buat wrapper untuk service.d ---
WRAPPER="$HOME/startup-wrapper.sh"
cat > "$WRAPPER" <<'EOF'
#!/system/bin/sh
# Wrapper service.d untuk jalankan Termux startup dengan auto wifi/hostpot

sleep 120
BASH_BIN="/data/data/com.termux/files/usr/bin/bash"
STARTUP_SCRIPT="/data/data/com.termux/files/home/.termux/boot/startup.sh"
LOG_FILE="/data/local/tmp/service-startup.log"

echo "=== Service start $(date) ===" >> $LOG_FILE

check_inet() { ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; return $?; }

USER_SSHD=$($BASH_BIN -c "whoami" 2>/dev/null)
[ -z "$USER_SSHD" ] && USER_SSHD="u$(id -u)"
HOTSPOT_SSID="rd4a_${USER_SSHD}"

# --- Coba koneksi ulang WiFi max 10x ---
i=0
while [ $i -lt 10 ]; do
    if check_inet; then
        echo "[INFO] Internet aktif setelah percobaan $i" >> $LOG_FILE
        break
    fi
    echo "[WARN] Internet belum aktif (percobaan $i)" >> $LOG_FILE
    su -c "svc wifi enable" 2>> $LOG_FILE
    sleep 5
    i=$((i+1))
done

# Kalau tetap gagal → nyalakan hotspot
if ! check_inet; then
    echo "[WARN] Internet gagal, nyalakan hotspot SSID=$HOTSPOT_SSID" >> $LOG_FILE
    su -c "cmd connectivity tethering start wifi" 2>> $LOG_FILE
    su -c "settings put global tether_dun_required 0" 2>> $LOG_FILE
    su -c "settings put global wifi_ap_ssid $HOTSPOT_SSID" 2>> $LOG_FILE
    su -c "settings put global wifi_ap_passphrase 'Termux1234'" 2>> $LOG_FILE
fi

# Jalankan startup.sh Termux  
if [ -x "$BASH_BIN" ] && [ -f "$STARTUP_SCRIPT" ]; then
    $BASH_BIN "$STARTUP_SCRIPT" >> $LOG_FILE 2>&1 &
    echo "[INFO] Startup script dijalankan" >> $LOG_FILE
else
    echo "[ERROR] Tidak ditemukan path bash atau script startup" >> $LOG_FILE
    echo "[DEBUG] BASH_BIN=$BASH_BIN" >> $LOG_FILE
    echo "[DEBUG] STARTUP_SCRIPT=$STARTUP_SCRIPT" >> $LOG_FILE
fi
EOF

chmod 755 "$WRAPPER"
su -c "mkdir -p /data/adb/service.d"
su -c "cp $WRAPPER /data/adb/service.d/startup.sh"
su -c "chmod 755 /data/adb/service.d/startup.sh"

# --- 2. Buat startup utama di ~/.termux/boot/startup.sh ---
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/startup.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Termux Boot Auto Startup Script (WiFi + miner + sshd)

BIN="/data/data/com.termux/files/usr/bin"
HOME="/data/data/com.termux/files/home"
LOG_DIR="$HOME/logs"
KEEP_DAYS=3
TS=$($BIN/date +"%Y%m%d-%H%M%S")
BOOT_LOG="$LOG_DIR/boot.log"
MINER_DIR="$HOME/ccminer"
MINER_SCRIPT="$MINER_DIR/miner.sh"
MINER_LOG="$LOG_DIR/miner.log"
PING_HOST="1.1.1.1"
WATCHDOG_INTERVAL=120

mkdir -p "$LOG_DIR"

echo "=== Boot Start $($BIN/date) ===" >> "$BOOT_LOG"
echo "Boot-ID: $TS" >> "$BOOT_LOG"

# Enable WiFi
if command -v su >/dev/null 2>&1; then
    su -c "svc wifi enable"
    echo "[INFO] WiFi enable" >> "$BOOT_LOG"
fi

# SSH Server
$BIN/sshd
echo "[INFO] SSH Server aktif di port 8022" >> "$BOOT_LOG"

# ADB TCP/IP
if command -v su >/dev/null 2>&1; then
    su -c "setprop service.adb.tcp.port 5555 && stop adbd && start adbd"
    echo "[INFO] ADB over TCP/IP enabled" >> "$BOOT_LOG"
fi

# Fungsi miner
start_miner() {
    if [ -x "$MINER_SCRIPT" ]; then
        if ! $BIN/pgrep -f "ccminer" >/dev/null; then
            cd "$MINER_DIR" || return 1
            $BIN/bash "$MINER_SCRIPT" >> "$MINER_LOG" 2>&1 &
            echo "[INFO] Miner start $($BIN/date)" >> "$BOOT_LOG"
        fi
    else
        echo "[ERROR] Miner script $MINER_SCRIPT tidak ditemukan" >> "$BOOT_LOG"
    fi
}
stop_miner() {
    if $BIN/pgrep -f "ccminer" >/dev/null; then
        $BIN/pkill -f "ccminer"
        echo "[WARN] Miner stop $($BIN/date)" >> "$BOOT_LOG"
    fi
}

# Tunggu internet
until $BIN/ping -c1 -W2 "$PING_HOST" >/dev/null 2>&1; do
    echo "[WARN] Menunggu internet..." >> "$BOOT_LOG"
    sleep 5
done
start_miner

# Watchdog
(
while true; do
    if $BIN/ping -c1 -W2 "$PING_HOST" >/dev/null 2>&1; then
        if ! $BIN/pgrep -f "ccminer" >/dev/null; then
            echo "[INFO] Miner restart $($BIN/date)" >> "$BOOT_LOG"
            start_miner
        fi
    else
        stop_miner
    fi
    sleep $WATCHDOG_INTERVAL
done
) &

# Bersihkan log lama
$BIN/find "$LOG_DIR" -type f -mtime +$KEEP_DAYS -delete
EOF

chmod +x ~/.termux/boot/startup.sh

# --- 3. Disable bloatware ---
echo "[INFO] Disable apps yang tidak dipakai..."
DISABLE_LIST="
com.google.android.gms
com.android.vending
com.android.contacts
com.android.gallery3d
com.android.camera
com.android.fmradio
com.miui.gamecenter
com.android.thememanager
"
for pkg in $DISABLE_LIST; do
    su -c "pm disable-user --user 0 $pkg" 2>/dev/null
    echo "[DISABLED] $pkg"
done

echo "[INFO] Install selesai. Reboot untuk uji coba."
