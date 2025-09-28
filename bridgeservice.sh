#!/data/data/com.termux/files/usr/bin/sh
# bridgeservice.sh
# Simple installer / starter for bridgeservice.py in Termux

BASE_DIR="$HOME/bridgeservice"
PY="$PREFIX/bin/python"
PIP="$PREFIX/bin/pip"

mkdir -p "$BASE_DIR"
cat > "$BASE_DIR/bridgeservice.py" <<'PYCODE'
# (isi file bridgeservice.py disisip di bawah — lihat file kedua di response)
PYCODE

cat > "$BASE_DIR/run.sh" <<'RUNSH'
#!/data/data/com.termux/files/usr/bin/sh
# wrapper to keep the service running
cd "$HOME/bridgeservice"
termux-wake-lock
nohup python bridgeservice.py >> bridgeservice.log 2>&1 &
echo "bridgeservice started (logs: $HOME/bridgeservice/bridgeservice.log)"
RUNSH

chmod +x "$BASE_DIR/run.sh"
echo "Bridgeservice files installed to $BASE_DIR"

case "$1" in
  start)
    sh "$BASE_DIR/run.sh"
    ;;
  stop)
    pkill -f bridgeservice.py || echo "no process found"
    termux-wake-unlock
    ;;
  restart)
    pkill -f bridgeservice.py || true
    sh "$BASE_DIR/run.sh"
    ;;
  install-deps)
    echo "Installing dependencies..."
    pkg update -y
    pkg install -y python clang git make openssh
    pkg install -y termux-api || true
    pip install --upgrade pip
    pip install websocket-client adbutils || true
    echo "Dependencies installed. For better UI automation, consider: pip install uiautomator2"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|install-deps}"
    ;;
esac
