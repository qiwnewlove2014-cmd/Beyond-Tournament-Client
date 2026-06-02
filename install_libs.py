import os
import sys
import subprocess

def install(package):
    print(f"[*] กำลังติดตั้ง {package}...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        print(f"[+] ติดตั้ง {package} สำเร็จ\n")
    except subprocess.CalledProcessError as e:
        print(f"[!] เกิดข้อผิดพลาดในการติดตั้ง {package}\n")

# รายชื่อไลบรารีทั้งหมดที่โปรเจ็กต์นี้ใช้งาน
packages = [
    "pygame==2.6.1",        # ระบบเกมและหน้าต่าง
    "cyal",                 # ระบบเสียง OpenAL 3D
    "pyogg",                # ถอดรหัสไฟล์เสียง OGG
    "requests",             # ดึงข้อมูลจากอินเทอร์เน็ต
    "yt-dlp",               # ค้นหาและดึงเพลงจาก YouTube (สำหรับ Music Bot)
    "ffmpeg-downloader",    # ใช้สำหรับหา FFMPEG (บางเครื่อง)
    "pyenet",               # ระบบ Network Multiplayer (enet)
    "accessible_output2",   # ระบบอ่านหน้าจอ (Screen Reader)
    "appdirs",              # จัดการโฟลเดอร์สำหรับเก็บข้อมูลเกม
    "cryptography",         # ระบบเข้ารหัส (ถ้ามี)
    "psutil",               # เช็คระบบการทำงานเครื่อง
    "pySmartDL",            # ระบบดาวน์โหลดไฟล์
    "pyperclip",            # จัดการคลิปบอร์ด (คัดลอก/วาง)
    "semver",               # เช็คเวอร์ชั่นเกม
    "urlextract",           # สกัด URL จากข้อความ
    "linkpreview"           # พรีวิวลิงก์
]

def main():
    print("========================================")
    print("เริ่มการติดตั้งไลบรารีที่จำเป็นสำหรับ Final Hour Client...")
    print("========================================")

    # อัปเกรด pip ก่อนเสมอเพื่อป้องกันปัญหาติดตั้งแพ็กเกจไม่ได้
    print("[*] กำลังอัปเกรด pip...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    except:
        pass
    print("\n")

    for pkg in packages:
        install(pkg)

    print("========================================")
    print("ติดตั้งไลบรารีเสร็จสิ้น! คุณสามารถรันเกมหรือนำไปสานต่อได้เลยครับ")
    print("========================================")

if __name__ == "__main__":
    main()
