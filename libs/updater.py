import os
import shutil
import zipfile
import subprocess
import sys
import tempfile
import pygame
import requests
import pySmartDL as dl
from . import state, menu, version
from .speech import speak
from . import path_utils

nothing = lambda: None

# =============================================================================
# GitHub Auto-Updater
# =============================================================================
# ผู้เล่นกด "Check for Updates" ใน main menu → client เรียก GitHub API
# เปรียบเทียบ tag ของ release ล่าสุดกับ version.py → ถ้าใหม่กว่าดาวน์โหลด
# .zip จาก release asset → แตกไฟล์ทับ → restart ผ่าน move_to/rm_dir handshake
# =============================================================================

GITHUB_OWNER = "qiwnewlove2014-cmd"
GITHUB_REPO = "Beyond-Tournament-Client"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def get_latest_release():
    """เรียก GitHub API ดึง release ล่าสุด
    คืน dict {tag, zip_url, notes, name} หรือ None ถ้าผิดพลาด"""
    try:
        r = requests.get(
            GITHUB_API,
            timeout=10,
            headers={"User-Agent": "BeyondTournament-Updater"},
        )
        if r.status_code == 404:
            # ยังไม่มี release บน GitHub — ไม่ใช่ปัญหาอินเทอร์เน็ต
            return {"tag": "", "zip_url": None, "notes": "", "name": "", "no_release": True}
        if not r.ok:
            return None
        data = r.json()
        tag = data.get("tag_name", "")  # เช่น "v1.3.0"
        # หา zip asset (ไฟล์ .zip ที่แนบใน release)
        zip_url = None
        for asset in data.get("assets", []):
            if asset.get("name", "").lower().endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break
        return {
            "tag": tag,
            "zip_url": zip_url,
            "notes": data.get("body", ""),
            "name": data.get("name", ""),
        }
    except Exception:
        return None


def parse_tag(tag):
    """แปลง 'v1.3.0' → (1, 3, 0) หรือ None ถ้า parse ไม่ได้"""
    if not tag:
        return None
    tag = tag.strip().lstrip("vV")
    parts = tag.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def is_newer(tag, current):
    """เช็คว่า tag (str) ใหม่กว่า current (tuple major,minor,patch) หรือไม่"""
    parsed = parse_tag(tag)
    if not parsed or len(parsed) != 3:
        return False
    return parsed > current


def _current_version_tuple():
    """ดึงเวอร์ชันปัจจุบันจาก version.py เป็น tuple (major, minor, patch)"""
    v = version.version
    return (v.major, v.minor, v.patch)


def _current_version_string():
    """ดึงเวอร์ชันปัจจุบันเป็น string 'x.y.z'"""
    v = version.version
    return f"{v.major}.{v.minor}.{v.patch}"


def install_and_restart(zip_path):
    """แตก zip ไป temp dir → สร้าง batch script → ปิดเกม → batch คัดลอกทับ → popup แจ้งเสร็จ

    ใช้ Batch Script Handoff เพื่อหลีกเลี่ยง WinError 5 (Access Denied)
    เนื่องจาก Windows ล็อกไฟล์ .pyd/.dll ที่กำลังถูกใช้งานอยู่

    ขั้นตอน:
    1. แตก zip ไป temp dir
    2. หาโฟลเดอร์ย่อยที่มี Beyond Tournament.exe
    3. สร้าง batch script ที่รอให้เกมปิดก่อน แล้วคัดลอกทับ + popup แจ้งเสร็จ
    4. สั่งรัน batch script แล้วปิดเกมทันที
    """
    current_dir = os.getcwd()

    # แตก zip ไป temp
    extract_dir = tempfile.mkdtemp(prefix="bt_update_")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        speak(f"Failed to extract update: {e}", True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return

    # หาโฟลเดอร์ที่มี exe (release อาจห่อโฟลเดอร์ย่อย)
    source_dir = extract_dir
    exe_name = "Beyond Tournament.exe"
    if not os.path.exists(os.path.join(source_dir, exe_name)):
        for entry in os.listdir(source_dir):
            sub = os.path.join(source_dir, entry)
            if os.path.isdir(sub) and os.path.exists(os.path.join(sub, exe_name)):
                source_dir = sub
                break

    if not os.path.exists(os.path.join(source_dir, exe_name)):
        speak("Update package does not contain the game executable.", True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return

    # สร้าง batch script สำหรับคัดลอกทับหลังเกมปิด
    bat_path = os.path.join(tempfile.gettempdir(), "bt_update_install.bat")
    pid = os.getpid()
    src = source_dir.replace('/', '\\')
    dst = current_dir.replace('/', '\\')
    ext = extract_dir.replace('/', '\\')
    zp = zip_path.replace('/', '\\')

    with open(bat_path, 'w', encoding='utf-8') as f:
        f.write('@echo off\n')
        f.write('chcp 65001 >nul\n')
        # รอให้ process เกมปิดตัวก่อน (สูงสุด 30 วินาที)
        f.write('set /a count=0\n')
        f.write(':waitloop\n')
        f.write('set /a count+=1\n')
        f.write('if %count% gtr 30 goto install\n')
        f.write(f'tasklist /FI "PID eq {pid}" 2>nul | find /I "{pid}" >nul\n')
        f.write('if not errorlevel 1 (\n')
        f.write('    timeout /t 1 /nobreak >nul\n')
        f.write('    goto waitloop\n')
        f.write(')\n')
        f.write(':install\n')
        # คัดลอกไฟล์ทับ (ตอนนี้ไม่มีไฟล์ถูกล็อกแล้ว)
        f.write(f'xcopy /E /Y /Q "{src}\\*" "{dst}\\"\n')
        # แสดง popup แจ้งอัปเดตเสร็จ
        f.write('powershell -WindowStyle Hidden -Command "')
        f.write("Add-Type -AssemblyName System.Windows.Forms; ")
        f.write("[System.Windows.Forms.MessageBox]::Show(")
        f.write("'Update installed successfully! Please restart the game.', ")
        f.write("'Beyond Tournament Update', 'OK', 'Information')")
        f.write('"\n')
        # ทำความสะอาด temp
        f.write(f'rmdir /s /q "{ext}"\n')
        f.write(f'del "{zp}" 2>nul\n')
        f.write('del "%~f0"\n')

    speak("Installing update. The game will close now.", True)
    import time
    time.sleep(2)  # ให้เวลาเสียงพูดจบก่อนปิด
    subprocess.Popen(
        ['cmd', '/c', bat_path],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    os._exit(0)


def check_and_update(game, interactive=True):
    """Entry point หลัก — เช็ค + ดาวน์โหลด + restart

    เรียกจาก main menu "Check for Updates"
    - interactive=True: พูดสถานะตลอด (สำหรับผู้เล่น)
    - interactive=False: เงียบ (สำหรับ auto-check ตอนเปิดแอป)

    คืน True ถ้ามีอัปเดตและเริ่มดาวน์โหลด, False ถ้าไม่มี/ผิดพลาด
    """
    if interactive:
        speak("Checking for updates...", True)

    release = get_latest_release()
    if not release:
        if interactive:
            speak(
                "Could not check for updates. Check your internet connection.",
                True,
            )
        return False
    if release.get("no_release"):
        if interactive:
            speak(
                "No releases are available on GitHub yet. "
                "Updates will appear here once published.",
                True,
            )
        return False

    current = _current_version_tuple()
    current_str = _current_version_string()

    if not is_newer(release["tag"], current):
        if interactive:
            speak(
                f"You are up to date. Current version {current_str}.",
                True,
            )
        return False

    # มีอัปเดต
    tag = release["tag"]
    if not release["zip_url"]:
        if interactive:
            speak(
                f"Update {tag} is available, but no download file was found "
                f"on GitHub. Please download it manually.",
                True,
            )
        return False

    if interactive:
        speak(
            f"Update available: {tag}. You are on {current_str}. Downloading...",
            True,
        )

    # ดาวน์โหลด .zip ไป temp
    tmp_zip = os.path.join(tempfile.gettempdir(), "bt_update.zip")
    try:
        smart = dl.SmartDL(
            release["zip_url"],
            tmp_zip,
            threads=2,
            progress_bar=False,
            timeout=120,
        )
        smart.start()
        smart.wait()  # รอจนดาวน์โหลดเสร็จ (blocking — ทำงานใน callback ของ menu)
        if not smart.isSuccessful():
            if interactive:
                speak("Download failed. Please try again later.", True)
            return False
    except Exception as e:
        if interactive:
            speak(f"Download failed: {e}", True)
        return False

    if interactive:
        speak("Download complete. Installing...", True)

    # แตก + ติดตั้ง + restart (ฟังก์ชันนี้จะ os._exit หลัง relaunch)
    install_and_restart(tmp_zip)
    return True


# =============================================================================
# Updater State — เช็คอัปเดตตอนเปิดแอป พร้อมถามยืนยันก่อนดาวน์โหลด
# =============================================================================
class Updater(state.State):
    """State wrapper ที่เช็คอัปเดตจาก GitHub ตอนเปิดแอป (compiled mode).

    ถ้าเจออัปเดตใหม่จะถามผู้เล่นก่อน (Enter = ดาวน์โหลด, Escape = ข้าม)
    ระหว่างดาวน์โหลดกดปุ่มเพื่อดูสถานะได้:
      Space = เปอร์เซ็นต์, 1 = ความเร็ว, 2 = ขนาด, 3 = เวลาเหลือ
    ถ้าไม่มีอัปเดตหรือผิดพลาดจะไป main_menu ตามปกติ"""

    def __init__(self, game, check=True):
        super().__init__(game)
        self.check = check
        self.update_info = None
        self.downloading = False
        self.smart_dl = None  # อ้างอิง SmartDL object สำหรับดูสถานะ

    def enter(self):
        from . import menus
        super().enter()
        if self.check:
            try:
                release = get_latest_release()
                if release and not release.get("no_release"):
                    current = _current_version_tuple()
                    if is_newer(release["tag"], current) and release.get("zip_url"):
                        self.update_info = release
                        current_str = _current_version_string()
                        speak(
                            f"Update available: {release['tag']}. "
                            f"You are on {current_str}. "
                            f"Press Enter to download, or Escape to skip.",
                            True,
                        )
                        return  # รอ input จากผู้เล่นใน update()
            except Exception:
                pass
        menus.main_menu(self.game)

    def exit(self):
        super().exit()

    def update(self, events):
        super().update(events)
        if not self.update_info:
            return

        for event in events:
            if event.type != pygame.KEYDOWN:
                continue

            # === ยังไม่ได้ดาวน์โหลด: รอยืนยัน ===
            if not self.downloading:
                if event.key == pygame.K_RETURN:
                    self.downloading = True
                    self._start_download()
                elif event.key == pygame.K_ESCAPE:
                    from . import menus
                    self.update_info = None
                    menus.main_menu(self.game)
                return

            # === กำลังดาวน์โหลด: กดดูสถานะ ===
            if self.smart_dl and not self.smart_dl.isFinished():
                if event.key == pygame.K_SPACE:
                    self._speak_progress()
                elif event.key == pygame.K_1:
                    self._speak_speed()
                elif event.key == pygame.K_2:
                    self._speak_size()
                elif event.key == pygame.K_3:
                    self._speak_eta()

    # === Status Reporting (Accessibility) ===

    def _speak_progress(self):
        """Space: พูดเปอร์เซ็นต์การดาวน์โหลด"""
        try:
            pct = int(self.smart_dl.get_progress() * 100)
            speak(f"{pct} percent", True)
        except Exception:
            speak("Calculating...", True)

    def _speak_speed(self):
        """1: พูดความเร็วดาวน์โหลด"""
        try:
            speed = self.smart_dl.get_speed()
            if speed > 1024 * 1024:
                speak(f"{speed / (1024 * 1024):.1f} megabytes per second", True)
            elif speed > 1024:
                speak(f"{speed / 1024:.0f} kilobytes per second", True)
            else:
                speak(f"{int(speed)} bytes per second", True)
        except Exception:
            speak("Calculating...", True)

    def _speak_size(self):
        """2: พูดขนาดที่ดาวน์โหลดแล้ว / ขนาดทั้งหมด"""
        try:
            dl_size = self.smart_dl.get_dl_size()
            total = self.smart_dl.filesize or 0
            dl_mb = dl_size / (1024 * 1024)
            if total > 0:
                total_mb = total / (1024 * 1024)
                speak(f"{dl_mb:.1f} of {total_mb:.1f} megabytes downloaded", True)
            else:
                speak(f"{dl_mb:.1f} megabytes downloaded", True)
        except Exception:
            speak("Calculating...", True)

    def _speak_eta(self):
        """3: พูดเวลาที่เหลือ"""
        try:
            eta = self.smart_dl.get_eta()
            if eta <= 0:
                speak("Calculating...", True)
            elif eta < 60:
                speak(f"About {int(eta)} seconds remaining", True)
            else:
                minutes = int(eta // 60)
                seconds = int(eta % 60)
                if seconds > 0:
                    speak(f"About {minutes} minutes and {seconds} seconds remaining", True)
                else:
                    speak(f"About {minutes} minutes remaining", True)
        except Exception:
            speak("Calculating...", True)

    # === Download & Install ===

    def _start_download(self):
        """เริ่มดาวน์โหลดใน background thread เพื่อไม่บล็อกตัวเกม"""
        import threading
        # เฟดเพลงลงก่อนเริ่มอัปเดต
        try:
            pygame.mixer.music.fadeout(1000)
        except Exception:
            pass
        speak(
            "Beyond Tournament Updating. "
            "Press Space for progress, 1 for speed, 2 for size, 3 for time remaining.",
            True,
        )
        t = threading.Thread(target=self._download_thread, daemon=True)
        t.start()

    def _download_thread(self):
        """ดาวน์โหลด .zip จาก GitHub แล้วเรียก install บน main thread"""
        release = self.update_info
        tmp_zip = os.path.join(tempfile.gettempdir(), "bt_update.zip")
        try:
            smart = dl.SmartDL(
                release["zip_url"],
                tmp_zip,
                threads=2,
                progress_bar=False,
                timeout=120,
            )
            self.smart_dl = smart  # เก็บอ้างอิงให้ main thread ดูสถานะได้
            smart.start()
            smart.wait()
            if not smart.isSuccessful():
                self.game.put(lambda: self._on_download_failed(
                    "Download failed. Please try again later."
                ))
                return
        except Exception as e:
            msg = str(e)
            self.game.put(lambda: self._on_download_failed(
                f"Download failed: {msg}"
            ))
            return

        # ดาวน์โหลดเสร็จ → สั่งติดตั้งบน main thread
        self.game.put(lambda: install_and_restart(tmp_zip))

    def _on_download_failed(self, msg):
        """ดาวน์โหลดล้มเหลว → แจ้งแล้วไป main menu"""
        from . import menus
        speak(msg, True)
        self.smart_dl = None
        self.downloading = False
        self.update_info = None
        menus.main_menu(self.game)

