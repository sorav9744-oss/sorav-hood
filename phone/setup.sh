#!/data/data/com.termux/files/usr/bin/bash
# One-time setup of the Android phone (Termux from F-Droid). Run:  bash setup.sh
# Safe to run again (updates everything, keeps your keys).
set -e
echo "== packages (takes a few minutes)"
pkg update -y && pkg upgrade -y
pkg install -y python git nodejs-lts ffmpeg termux-api
pip install -U "yt-dlp[default]" requests pyyaml
echo "== code"
if [ ! -d ~/s-clips ]; then git clone https://github.com/prajmulik49-stack/s-clips.git ~/s-clips; fi
cd ~/s-clips && git pull --ff-only
if [ ! -f .env ]; then
  echo "== keys (stored only on this phone, in ~/s-clips/.env)"
  read -r -p "MEDIA_TOKEN (GitHub token, starts with ghp_): " T
  read -r -p "YOUTUBE_API_KEY (starts with AIza): " K
  printf "MEDIA_TOKEN=%s\nYOUTUBE_API_KEY=%s\n" "$T" "$K" > .env
  chmod 600 .env
fi
echo "== start automatically after every reboot (needs the Termux:Boot app, opened once)"
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-joshiclip.sh <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
pkill -f "joshiclip.phone loop" 2>/dev/null || true
cd ~/s-clips && nohup python -m joshiclip.phone loop >> ~/joshiclip-loop.log 2>&1 &
BOOT
chmod +x ~/.termux/boot/start-joshiclip.sh
echo "== check"
python -m joshiclip.phone check || { echo "CHECK FAILED - send the lines above to Claude"; exit 1; }
bash ~/.termux/boot/start-joshiclip.sh
echo "== DONE. The phone now runs every night at 00:30 IST. Log: ~/joshiclip-phone.log"
