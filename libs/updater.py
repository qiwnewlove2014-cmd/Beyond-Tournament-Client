import os
import shutil
import zipfile
import subprocess
import sys
import tempfile
from pygame import key
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
    """แตก zip ไป temp dir → copy ทับไดเรกทอรีปัจจุบัน → restart ผ่าน
    move_to/rm_dir handshake (scaffolding ที่มีอยู่แล้วใน game.parse_arguments)

    ขั้นตอน:
    1. แตก zip ไป temp dir
    2. หาโฟลเดอร์ย่อยที่มี Beyond Tournament.exe (release อาจห่อโฟลเดอร์)
    3. คัดลอกทับไดเรกทอรีปัจจุบัน
    4. relaunch exe ใหม่ + สั่งลบไดเรกทอรีเก่า
    """
    # หาไดเรกทอรีปัจจุบัน (ที่ exe รันอยู่)
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
        # ค้นหาในโฟลเดอร์ย่อยระดับเดียว
        for entry in os.listdir(source_dir):
            sub = os.path.join(source_dir, entry)
            if os.path.isdir(sub) and os.path.exists(os.path.join(sub, exe_name)):
                source_dir = sub
                break

    if not os.path.exists(os.path.join(source_dir, exe_name)):
        speak("Update package does not contain the game executable.", True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return

    # คัดลอกทับ (ใช้ path_utils.copy_folder เหมือน move_to handshake)
    speak("Installing update...", True)
    try:
        path_utils.copy_folder(source_dir, current_dir)
    except Exception as e:
        speak(f"Failed to install update: {e}", True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return

    # ทำความสะอาด temp
    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        os.remove(zip_path)
    except Exception:
        pass

    # Restart: relaunch exe ใหม่ในไดเรกทอรีปัจจุบัน + สั่งให้มันลบตัวเองทิ้ง
    # (ใช้ rm_dir handshake ของ parse_arguments)
    speak("Update installed. Restarting...", True)
    new_exe = os.path.join(current_dir, exe_name)
    try:
        subprocess.Popen(
            [new_exe, "rm_dir", current_dir, str(os.getpid())],
            cwd=current_dir,
        )
        # ออกจากโปรแกรมปัจจุบันให้ process ใหม่ทำงานต่อ
        os._exit(0)
    except Exception as e:
        speak(f"Failed to restart: {e}. Please restart manually.", True)


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
# Legacy Updater State (คงไว้เพื่อความเข้ากันได้ — game.py/menus.py ยังอ้างถึง)
# =============================================================================
class Updater(state.State):
    """State wrapper ที่อัปเดตจาก GitHub ตอนเปิดแอป (compiled mode).

    ใช้ check_and_update ใน enter() แทน stub เดิม ถ้าไม่มีอัปเดตหรือผิดพลาด
    จะไป main_menu ตามปกติ ถ้ามีอัปเดตจะดาวน์โหลด+restart ให้อัตโนมัติ"""

    def __init__(self, game, check=True):
        super().__init__(game)
        self.check = check

    def enter(self):
        from . import menus
        super().enter()
        if self.check:
            # เช็คแบบเงียบตอนเปิดแอป ถ้าไม่มีอัปเดต → ไป main menu
            # ถ้ามีอัปเดต → check_and_update จะพูดแจ้งและ restart เอง
            try:
                updated = check_and_update(self.game, interactive=False)
                if updated:
                    return  # กำลัง restart อยู่ อย่าไป main menu
            except Exception:
                pass
        menus.main_menu(self.game)

    def exit(self):
        super().exit()

    def update(self, events):
        super().update(events)
