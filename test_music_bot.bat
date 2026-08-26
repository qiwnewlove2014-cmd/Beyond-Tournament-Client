@echo off
echo ========================================
echo   MUSIC BOT PACKET LOSS TEST
echo ========================================
echo.
echo วิธีใช้:
echo 1. เปิดเกม
echo 2. เปิด Music Bot Broadcast
echo 3. ดับเบิ้ลคลิกไฟล์นี้
echo 4. ดู log ว่า gaps/underruns เท่าไหร่
echo.
echo กด Ctrl+C เพื่อหยุด
echo ========================================
echo.
python -c "import time; print('กำลังเริ่ม test...'); time.sleep(2)"
python -c "from libs import music_bot; print('Music Bot module loaded')"
python -c "
import time
print('='*50)
print('Packet Loss Simulation Ready!')
print('='*50)
print()
print('ในเกม ให้พิมพ์คำสั่งนี้ใน Python console:')
print()
print('  game.music_streams[0].toggle_sim_loss(0.05)')
print()
print('หรือดู log ตรงๆ:')
print('  - gaps(>60ms) = packet loss')
print('  - large(>200ms) = หายไปนาน')
print('  - underruns = buffer ไม่ทัน')
print()
print('ปิดหน้าต่างนี้ได้เลย')
print('='*50)
"
pause
