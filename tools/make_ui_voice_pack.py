"""Build the UI voice-pack staging tree under client/ui_voice_pack.

The pack holds one placeholder per spoken line of the client's pre-game menus
(the fresh-launch experience: intro, main menu, login and account flows, the
Options tree, the speaker test and the updater). Each placeholder is a .txt
named like the .ogg it will become; when the voice clips are generated the
.txt files are replaced by converted .ogg files and nothing else moves.

Every entry records the game's own words (the label or prompt exactly as the
code speaks it) plus a Thai rendering, so either language can be fed to the
TTS generator without re-reading the client source. Run from client/:

    python tools/make_ui_voice_pack.py
"""
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent / "ui_voice_pack"

# (folder, slug, english, thai, note)
# folder: screen folder inside the pack; note: where the line lives in code.
ENTRIES = [
    # --- startup (game.py start) ---
    ("startup", "game_title", "Beyond Tournament!",
     "บียอนด์ ทัวร์นาเมนต์",
     "Spoken right after intro.ogg at launch (game.py start). intro.ogg itself already exists in data/."),
    # --- main menu (menus.main_menu) ---
    ("main_menu", "menu_title", "Main menu.",
     "เมนูหลัก",
     "menus.py main_menu(): the title spoken when the menu opens."),
    ("main_menu", "login", "Login",
     "เข้าสู่ระบบ",
     "Opens the account picker, or the no-account menu when none are saved."),
    ("main_menu", "set_account", "Set account",
     "ตั้งค่าบัญชี",
     "Username then password prompts, then logs in."),
    ("main_menu", "create_account", "Create account",
     "สร้างบัญชี",
     "Agreement question, then username and password prompts."),
    ("main_menu", "options", "options",
     "ตัวเลือก",
     "Opens the Options menu."),
    ("main_menu", "check_for_updates", "Check for Updates",
     "ตรวจสอบอัปเดต",
     "Compiled builds only; source builds say they bypass the updater."),
    ("main_menu", "speaker_test", "Test Speakers and Headphones",
     "ทดสอบลำโพงและหูฟัง",
     "Opens the speaker test menu."),
    ("main_menu", "restart_client", "Restart Client",
     "รีสตาร์ทเกม",
     "Opens the restart confirmation."),
    ("main_menu", "exit", "Exit",
     "ออก",
     "Fades the audio out and quits; Esc on the root menu reaches it too."),
    # --- accounts menu (menus.accounts_menu) ---
    ("accounts", "menu_title", "Select an account to login with",
     "เลือกบัญชีที่จะใช้เข้าสู่ระบบ",
     "menus.py accounts_menu(): title. One 'Login with <name>' and one 'Delete <name>' line exists per saved account -- record the template once; the name is spoken live."),
    ("accounts", "login_with_template", "Login with",
     "เข้าสู่ระบบด้วย",
     "Dynamic line: 'Login with <username>'. Only the fixed part can be pre-recorded."),
    ("accounts", "delete_template", "Delete (Note: Removes the account from this client only, server data is unaffected)",
     "ลบบัญชี (ลบจากเครื่องนี้เท่านั้น ข้อมูลในเซิร์ฟเวอร์ไม่ถูกกระทบ)",
     "Dynamic line: 'Delete <username>' plus this fixed note."),
    ("accounts", "go_back", "Go back",
     "ย้อนกลับ",
     "Returns to the main menu."),
    # --- no account menu (menus.no_account) ---
    ("no_account", "menu_title", "you have no account set, would you like to set an account or create a new one?",
     "คุณยังไม่ได้ตั้งบัญชี ต้องการจะตั้งบัญชีด้วยบัญชีที่มีอยู่เดิม หรือสร้างบัญชีใหม่?",
     "menus.py no_account(): title of the menu reached from Login with no saved accounts."),
    ("no_account", "set_existing", "Set an account with existing credentials",
     "ตั้งค่าบัญชีด้วยชื่อผู้ใช้และรหัสผ่านที่มีอยู่เดิม",
     "Username then password prompts, then logs in."),
    ("no_account", "create_new", "Create a new account from scratch",
     "สร้างบัญชีใหม่ตั้งแต่ต้น",
     "Same flow as the main menu's Create account."),
    ("no_account", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to the main menu."),
    # --- create account flow (game.create_account) ---
    ("create_account", "agreement_title", "Do you agree with this game's agreement?",
     "คุณยอมรับข้อตกลงของเกมนี้หรือไม่?",
     "game.py create_account(): title of the agreement question."),
    ("create_account", "read_agreement", "Read the agreement",
     "อ่านข้อตกลง",
     "Opens the scrollable agreement reader; its lines are the agreement document itself and stay screen-reader speech."),
    ("create_account", "agree", "Yes, I have read, understood, and agreed to everything in the agreement.",
     "ใช่ ฉันได้อ่าน เข้าใจ และยอมรับทุกข้อในข้อตกลงนี้",
     "Continues to the username prompt."),
    ("create_account", "disagree", "No, I disagree.",
     "ไม่ ฉันไม่ยอมรับ",
     "Returns to the main menu."),
    ("create_account", "agreement_back", "Back to the agreement question",
     "กลับไปที่คำถามเรื่องข้อตกลง",
     "Last line of the agreement reader."),
    ("create_account", "enter_username", "Enter your username.",
     "กรอกชื่อผู้ใช้ของคุณ",
     "Input prompt (create_account flow)."),
    ("create_account", "username_invalid", "Canceled. Your username must be 4 to 25 characters.",
     "ยกเลิกแล้ว ชื่อผู้ใช้ต้องมีความยาว 4 ถึง 25 ตัวอักษร",
     "game.py create_account2() rejection message."),
    ("create_account", "enter_password", "Enter your password.",
     "กรอกรหัสผ่านของคุณ",
     "Input prompt (hidden typing)."),
    ("create_account", "password_invalid", "Canceled. Your password must be less than 70 characters.",
     "ยกเลิกแล้ว รหัสผ่านต้องสั้นกว่า 70 ตัวอักษร",
     "game.py create_account3() rejection message."),
    ("create_account", "creating", "Please wait. Creating your account...",
     "โปรดรอ กำลังสร้างบัญชีของคุณ",
     "game.py creating(): spoken once the server connection opens."),
    # --- login flow (game.login / login2 / set_account_done) ---
    ("login", "connecting", "Connecting to the server. Please wait...",
     "กำลังเชื่อมต่อกับเซิร์ฟเวอร์ โปรดรอ",
     "game.py login(): spoken before the address walk starts."),
    ("login", "logging_in", "Logging in. Please wait...",
     "กำลังเข้าสู่ระบบ โปรดรอ",
     "game.py login2(): spoken once the handshake comes back."),
    ("login", "set_done", "done.",
     "เสร็จเรียบร้อย",
     "game.py set_account_done(): spoken after saving the entered credentials."),
    ("login", "failed_official", "Failed to connect to the official server.",
     "เชื่อมต่อกับเซิร์ฟเวอร์หลักไม่สำเร็จ",
     "game.py _report_login_failure(): production-build wording."),
    # --- restart and exit (game.ask_to_restart_client / start_exit_fade) ---
    ("restart_exit", "restart_title", "Restart the client? This closes and reopens the game to clear all audio and client resources.",
     "รีสตาร์ทเกมหรือไม่? การทำเช่นนี้จะปิดและเปิดเกมใหม่ เพื่อล้างทรัพยากรเสียงและตัวเกมทั้งหมด",
     "game.py ask_to_restart_client(): title of the confirmation."),
    ("restart_exit", "restart_yes", "Yes, restart the client",
     "ใช่ รีสตาร์ทเกม",
     "Restarts the process."),
    ("restart_exit", "restart_no", "No, return to the main menu",
     "ไม่ กลับไปที่เมนูหลัก",
     "Cancels the restart."),
    ("restart_exit", "exiting", "Exiting",
     "กำลังออกจากเกม",
     "Announced by the exit fade (game.py start_exit_fade)."),
    ("restart_exit", "disconnecting", "Disconnecting",
     "กำลังยกเลิกการเชื่อมต่อ",
     "Announced by the in-game logout fade."),
    # --- speaker test (menus.speaker_test_menu) ---
    ("speaker_test", "menu_title", "Speaker test. Up and Down move between the lines, and Enter plays the position the line names. Nothing is heard until you press Enter, and the menu stays open so you can press it again. Escape, or Back, leaves the test.",
     "ทดสอบลำโพง กดขึ้นลงเพื่อเลื่อนดูรายการ แล้วกด Enter เพื่อเล่นเสียงตำแหน่งที่รายการนั้นระบุ จะยังไม่มีเสียงจนกว่าจะกด Enter และเมนูจะเปิดค้างไว้ให้กดซ้ำได้ กด Escape หรือ Back เพื่อออกจากการทดสอบ",
     "Title plus SPEAKER_TEST_INTRO, spoken together when the menu opens."),
    ("speaker_test", "left", "Left speaker only",
     "ลำโพงฝั่งซ้ายเท่านั้น",
     "Plays the left sample hard-panned left."),
    ("speaker_test", "centre", "Centre speaker only",
     "ลำโพงกลางเท่านั้น",
     "Plays the centre sample dead centre."),
    ("speaker_test", "right", "Right speaker only",
     "ลำโพงฝั่งขวาเท่านั้น",
     "Plays the right sample hard-panned right."),
    ("speaker_test", "walk", "Left, centre, then right",
     "ซ้าย กลาง แล้วขวา",
     "Walks the three placements in order, left to right."),
    ("speaker_test", "stop", "Stop the test",
     "หยุดการทดสอบ",
     "Stops any playing sample or walk."),
    ("speaker_test", "back", "Back",
     "ย้อนกลับ",
     "Returns to the main menu."),
    ("speaker_test", "muted_notice", "Audio is muted, so nothing will be heard. Turn the sound back on first.",
     "เสียงถูกปิดอยู่ จะไม่มีเสียงให้ได้ยิน กรุณาเปิดเสียงก่อน",
     "Spoken when a placement is pressed while muted."),
    ("speaker_test", "sample_note_LEFT", "The samples (ui/speaker_test_left|center|right.ogg) already exist and are panned mono -- regenerate them only to match a new voice.",
     "ไฟล์เสียงตัวอย่าง (ซ้าย/กลาง/ขวา) มีอยู่แล้วและเป็นโมโนสำหรับจัดตำแหน่ง -- สร้างใหม่เฉพาะเมื่อจะเปลี่ยนเสียงให้เข้าชุดกัน",
     "Not a clip: a note about the existing samples."),
    # --- updater (updater.Updater / menus.update_question) ---
    ("updater", "checking", "Checking for updates...",
     "กำลังตรวจสอบอัปเดต",
     "updater.py: spoken while the version check runs."),
    ("updater", "update_question_title", "An update is available! Would you like to update now?",
     "มีอัปเดตใหม่! ต้องการอัปเดตตอนนี้หรือไม่?",
     "menus.py update_question(): title of the Yes/No menu."),
    ("updater", "yes", "Yes",
     "ใช่",
     "Starts the download and install."),
    ("updater", "no", "No",
     "ไม่",
     "Skips the update."),
    ("updater", "download_complete", "Download complete. Installing...",
     "ดาวน์โหลดเสร็จแล้ว กำลังติดตั้ง",
     "updater.py: spoken when the download finishes."),
    ("updater", "download_failed", "Download failed. Please try again later.",
     "ดาวน์โหลดไม่สำเร็จ โปรดลองอีกครั้งภายหลัง",
     "updater.py: spoken when the download fails."),
    ("updater", "installing", "Installing update. The game will close now.",
     "กำลังติดตั้งอัปเดต เกมจะปิดตัวลงตอนนี้",
     "updater.py: spoken right before the update is applied."),
    ("updater", "bypass_note_dev_only", "Bypassing updater in uncompiled version...",
     "ข้ามตัวตรวจอัปเดตเนื่องจากเป็นเวอร์ชันรันจากซอร์ส",
     "Source-builds only (game.py start)."),
    # --- options menu (menus.options_menu) ---
    ("options", "menu_title", "Options menu",
     "เมนูตัวเลือก",
     "menus.py options_menu(): title."),
    ("options", "output_device", "Select output device",
     "เลือกอุปกรณ์เล่นเสียง",
     "Line prefix; the current device name follows live. Dev builds also list Server hostname / Server port above it."),
    ("options", "server_hostname_dev_only", "Server hostname",
     "โฮสต์เนมของเซิร์ฟเวอร์",
     "Non-production builds only (menus.py options_menu endpoint_items)."),
    ("options", "server_port_dev_only", "Server port",
     "พอร์ตของเซิร์ฟเวอร์",
     "Non-production builds only."),
    ("options", "input_device", "Select input device",
     "เลือกอุปกรณ์รับเสียง",
     "Line prefix; the current device name follows live."),
    ("options", "instrument_input_device", "Select instrument input device",
     "เลือกอุปกรณ์รับเสียงสำหรับเครื่องดนตรี",
     "Line prefix; the current device name follows live."),
    ("options", "jitter_buffer", "Voice Chat Jitter Buffer",
     "บัฟเฟอร์จัดการเสียงสั่นของแชทเสียง",
     "Line prefix; the current value follows live. The prompt text is its own entry below."),
    ("options", "voice_chat", "Voice Chat",
     "แชทเสียง",
     "Toggle item."),
    ("options", "voice_chat_mode", "Voice chat mode. Press Enter to change.",
     "โหมดแชทเสียง กด Enter เพื่อเปลี่ยน",
     "Cycle item; the current setting's label follows live (see the two mode labels below)."),
    ("options", "voice_chat_mode_toggle", "Tap to talk (press once to start, press again to stop)",
     "แตะเพื่อพูด (กดครั้งเดียวเพื่อเริ่ม กดอีกครั้งเพื่อหยุด)",
     "options.VOICE_CHAT_MODES['toggle']."),
    ("options", "voice_chat_mode_ptt", "Push to talk (hold the key while speaking)",
     "กดค้างไว้เพื่อพูด (กดปุ่มค้างขณะพูด)",
     "options.VOICE_CHAT_MODES['ptt']."),
    ("options", "microphone", "microphone",
     "ไมโครโฟน",
     "Toggle item."),
    ("options", "player_beacons", "Player beacons",
     "บิกเกอร์บอกตำแหน่งผู้เล่น",
     "Toggle item."),
    ("options", "wall_tone", "Wall proximity tone",
     "เสียงเตือนใกล้กำแพง",
     "Toggle item."),
    ("options", "compass_turn_cue", "Compass turn cue",
     "เสียงบอกทิศทางเข็มทิศ",
     "Toggle item."),
    ("options", "turning_sensitivity", "Turning sensitivity. Current setting:",
     "ความไวในการหมุน ค่าที่ตั้งอยู่:",
     "Line prefix; the level label and 'level N of 4' follow live. Level labels: Very low / Low / Medium / High."),
    ("options", "turning_mode", "Turning mode. Current setting:",
     "โหมดการหมุน ค่าที่ตั้งอยู่:",
     "Line prefix; one of the three mode labels below follows live."),
    ("options", "turning_mode_degrees", "Degrees",
     "องศา",
     "options.TURN_MODES['degrees']."),
    ("options", "turning_mode_clock_continuous", "Clock face, continuous turning",
     "หน้าปัดนาฬิกา หมุนต่อเนื่อง",
     "options.TURN_MODES['clock_continuous']."),
    ("options", "turning_mode_clock_hour", "Clock face, one hour per press",
     "หน้าปัดนาฬิกา ชั่วโมงละหนึ่งครั้งต่อการกด",
     "options.TURN_MODES['clock_hour']."),
    ("options", "play_intro", "play intro at start up",
     "เล่นเสียงเปิดเกมตอนสตาร์ท",
     "Toggle item."),
    ("options", "stream_ambience", "Stream ambience: turning this off might introduce more memory usage and map loading time, but better performance and less CPU usage",
     "สตรีมเสียงบรรยากาศ: การปิดจะใช้หน่วยความจำและเวลาโหลดแผนที่มากขึ้น แต่ได้ประสิทธิภาพดีขึ้นและใช้ CPU น้อยลง",
     "Toggle item."),
    ("options", "high_performance", "High performance mode: turning this on raises the game framerate from 60 to 120, so incoming music notes, voices, and your key presses reach your ears up to twice as fast. It uses more CPU, so turn it off if your computer gets hot or slows down",
     "โหมดประสิทธิภาพสูง: เปิดไว้จะเพิ่มเฟรมเรตจาก 60 เป็น 120 ทำให้โน้ตเพลง เสียงพูด และการกดปุ่มของคุณถึงหูเร็วขึ้นเกือบสองเท่า แต่ใช้ CPU มากขึ้น หากเครื่องร้อนหรือช้าลงให้ปิด",
     "Toggle item."),
    ("options", "mute_on_focus_loss", "Mute audio when the game window does not have focus",
     "ปิดเสียงเมื่อหน้าต่างเกมไม่ได้ถูกเลือกไว้",
     "Toggle item."),
    ("options", "mute_speech_on_focus_loss", "Mute speech when out of the game window",
     "ปิดเสียงพูดเมื่ออยู่นอกหน้าต่างเกม",
     "Toggle item."),
    ("options", "keyboard_typing_sounds", "Keyboard typing sounds",
     "เสียงพิมพ์คีย์บอร์ด",
     "Toggle item."),
    ("options", "speak_on_turn", "speak your direction when finished turning",
     "บอกทิศทางเมื่อหมุนเสร็จ",
     "Toggle item."),
    ("options", "typing_indicators", "receive typing indicators",
     "รับการแจ้งเตือนว่ากำลังพิมพ์",
     "Toggle item."),
    ("options", "buffer_timestamps", "Set how you would like timestamps in the end of buffer items to be displayed",
     "ตั้งค่ารูปแบบเวลาที่แสดงท้ายรายการในบัฟเฟอร์",
     "Opens the buffer timing menu."),
    ("options", "hrtf_model", "Set which HRTF Model you would like to use.",
     "ตั้งค่าโมเดล HRTF ที่ต้องการใช้",
     "Line prefix; the current model follows live. Opens the HRTF menu."),
    ("options", "sound_system", "Set which sound system you would like to use.",
     "ตั้งค่าระบบเสียงที่ต้องการใช้",
     "Line prefix; the live description follows. Opens the sound system menu."),
    ("options", "location_announcement", "Configure location announcement.",
     "ตั้งค่าการประกาศตำแหน่ง",
     "Line prefix; the current setting name follows live. Opens the location menu."),
    ("options", "key_bindings", "Configure key bindings.",
     "ตั้งค่าปุ่มลัด",
     "Opens the key binding menu."),
    ("options", "drum_keys", "Configure drum keys.",
     "ตั้งค่าปุ่มกลอง",
     "Opens the drum key menu."),
    ("options", "presence_sounds_dev_only", "Configure custom online and offline sounds",
     "ตั้งค่าเสียงออนไลน์และออฟไลน์แบบกำหนดเอง",
     "In-game and non-production builds only (menus.py options_menu)."),
    ("options", "back", "Back",
     "ย้อนกลับ",
     "Returns to the parent (main menu, or gameplay)."),
    # --- options: buffer timing (menus.buffer_timing_menu) ---
    ("options_buffer_timing", "menu_title", "How would you like timestamps to be displayed in buffer items?",
     "ต้องการให้แสดงเวลาในรายการบัฟเฟอร์แบบใด?",
     "menus.py buffer_timing_menu(): title."),
    ("options_buffer_timing", "absolute", "Absolute time",
     "เวลาแบบสัมบูรณ์",
     "Shows clock time."),
    ("options_buffer_timing", "relative", "Relative time",
     "เวลาแบบสัมพัทธ์",
     "Shows time relative to now."),
    ("options_buffer_timing", "none", "Don't display timestamps",
     "ไม่ต้องแสดงเวลา",
     "Hides timestamps."),
    ("options_buffer_timing", "back", "Back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: HRTF model menu (menus.hrtf_model_menu) ---
    ("options_hrtf", "menu_title", "Select your HRTF model",
     "เลือกโมเดล HRTF ของคุณ",
     "menus.py hrtf_model_menu(): title. The model list itself is whatever this machine's OpenAL offers and stays screen-reader speech."),
    ("options_hrtf", "disable", "Disable HRTF",
     "ปิดใช้งาน HRTF",
     "Turns HRTF off."),
    ("options_hrtf", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: sound system menu (menus.sound_system_menu) ---
    ("options_sound_system", "menu_title", "Select your sound system",
     "เลือกระบบเสียงของคุณ",
     "menus.py sound_system_menu(): title. The rendering list is machine-specific and stays screen-reader speech."),
    ("options_sound_system", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: output devices (menus.output_menu) ---
    ("options_output_devices", "menu_title", "select audio output",
     "เลือกอุปกรณ์เล่นเสียง",
     "menus.py output_menu(): title. Device names are machine-specific and stay screen-reader speech."),
    ("options_output_devices", "system_default", "system default",
     "ค่าเริ่มต้นของระบบ",
     "First line of the output menu."),
    ("options_output_devices", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: input devices (menus.input_menu) ---
    ("options_input_devices", "menu_title", "select audio input",
     "เลือกอุปกรณ์รับเสียง",
     "menus.py input_menu(): title for the voice-chat microphone."),
    ("options_input_devices", "system_default", "system default",
     "ค่าเริ่มต้นของระบบ",
     "First line of the input menu."),
    ("options_input_devices", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: instrument inputs (menus.input_menu, target="instrument") ---
    ("options_instrument_inputs", "menu_title", "select instrument audio input",
     "เลือกอุปกรณ์รับเสียงสำหรับเครื่องดนตรี",
     "menus.py input_menu(): title for the instrument input."),
    ("options_instrument_inputs", "system_default", "system default",
     "ค่าเริ่มต้นของระบบ",
     "First line of the instrument input menu."),
    ("options_instrument_inputs", "go_back", "go back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- options: jitter buffer prompt (menus.configure_jitter_buffer) ---
    ("options_jitter_prompt", "prompt",
     "Enter the value for your voice chat Jitter buffer. This is how long the client should wait to start playing voice chats to allow for audio data to back up, preventing stuttering. A lower jitter buffer will decrease latency but may cause stuttering if internet is not stable enough. A higher jitter buffer will increase latency but will have a more stable sound. Minimum is 20ms and maximum is 120ms.",
     "กรอกค่าบัฟเฟอร์แชทเสียงของคุณ นี่คือระยะเวลาที่เกมจะรอก่อนเริ่มเล่นเสียงแชท เพื่อให้ข้อมูลเสียงสะสมพอ ป้องกันเสียงสั่น ค่าต่ำจะลดความหน่วงแต่อาจเสียงสั่นหากเน็ตไม่เสถียร ค่าสูงจะเพิ่มความหน่วงแต่เสียงนิ่งกว่า ต่ำสุด 20 มิลลิวินาที สูงสุด 120 มิลลิวินาที",
     "The input prompt for the jitter buffer value."),
    # --- options: location announcement (menus.configure_location_template) ---
    ("options_location", "menu_title", "Configure location announcement.",
     "ตั้งค่าการประกาศตำแหน่ง",
     "menus.py configure_location_template(): title; the current setting name follows live."),
    ("options_location", "preset_full", "Use Full details.",
     "ใช้รายละเอียดเต็ม",
     "Preset line prefix; the spoken example follows live."),
    ("options_location", "preset_compact", "Use Compact.",
     "ใช้แบบกระชับ",
     "Preset line prefix."),
    ("options_location", "preset_coordinates", "Use Coordinates only.",
     "ใช้เฉพาะพิกัด",
     "Preset line prefix."),
    ("options_location", "preset_navigation", "Use Navigation.",
     "ใช้แบบนำทาง",
     "Preset line prefix."),
    ("options_location", "preset_surface", "Use Surface and posture.",
     "ใช้แบบพื้นผิวและท่าทาง",
     "Preset line prefix."),
    ("options_location", "build_custom", "Build a custom announcement by choosing individual parts",
     "สร้างการประกาศแบบกำหนดเองโดยเลือกทีละส่วน",
     "Opens the part picker."),
    ("options_location", "advanced_editor", "Advanced raw template editor",
     "ตัวแก้ไขเทมเพลตดิบสำหรับผู้เชี่ยวชาญ",
     "Opens the raw template input."),
    ("options_location", "preview", "Preview current announcement",
     "ฟังตัวอย่างการประกาศปัจจุบัน",
     "Speaks an example with preview values."),
    ("options_location", "reset_default", "Reset to default Full details",
     "รีเซ็ตเป็นรายละเอียดเต็มแบบเริ่มต้น",
     "Restores the default template."),
    ("options_location", "back", "Back",
     "ย้อนกลับ",
     "Returns to Options."),
    ("options_location", "custom_title", "Build a custom location announcement. Choose each part to include or exclude it.",
     "สร้างการประกาศตำแหน่งแบบกำหนดเอง เลือกแต่ละส่วนเพื่อรวมหรือตัดออก",
     "Title of the part picker."),
    ("options_location", "part_x", "X coordinate",
     "พิกัด X",
     "Part picker line (prefix); Included/Excluded follows live."),
    ("options_location", "part_y", "Y coordinate",
     "พิกัด Y",
     "Part picker line (prefix)."),
    ("options_location", "part_z", "Z coordinate",
     "พิกัด Z",
     "Part picker line (prefix)."),
    ("options_location", "part_tile", "Surface or tile",
     "พื้นผิวหรือชนิดพื้น",
     "Part picker line (prefix)."),
    ("options_location", "part_direction", "Facing direction",
     "ทิศที่หันอยู่",
     "Part picker line (prefix)."),
    ("options_location", "part_angle", "Horizontal angle",
     "มุมแนวนอน",
     "Part picker line (prefix)."),
    ("options_location", "part_pitch", "Vertical pitch",
     "มุมแนวตั้ง",
     "Part picker line (prefix)."),
    ("options_location", "part_lean", "Lean angle",
     "มุมเอียงตัว",
     "Part picker line (prefix)."),
    ("options_location", "part_balance", "Balance status",
     "สถานะการทรงตัว",
     "Part picker line (prefix)."),
    ("options_location", "included", "Included",
     "รวมอยู่",
     "State word spoken after a part's label."),
    ("options_location", "excluded", "Excluded",
     "ตัดออก",
     "State word spoken after a part's label."),
    ("options_location", "preview_custom", "Preview custom announcement",
     "ฟังตัวอย่างการประกาศที่กำหนดเอง",
     "Part picker line."),
    ("options_location", "save_custom", "Save custom announcement",
     "บันทึกการประกาศที่กำหนดเอง",
     "Part picker line."),
    ("options_location", "back_to_choices", "Back to location announcement choices",
     "กลับไปที่ตัวเลือกการประกาศตำแหน่ง",
     "Part picker line."),
    # --- options: presence sounds (menus.presence_sounds_menu) ---
    ("options_presence_sounds", "menu_title", "Custom online and offline sounds",
     "เสียงออนไลน์และออฟไลน์แบบกำหนดเอง",
     "menus.py presence_sounds_menu(): title (dev/source builds)."),
    ("options_presence_sounds", "upload_online", "Upload online sound.",
     "อัปโหลดเสียงออนไลน์",
     "Line prefix; the current status follows live."),
    ("options_presence_sounds", "upload_offline", "Upload offline sound.",
     "อัปโหลดเสียงออฟไลน์",
     "Line prefix; the current status follows live."),
    ("options_presence_sounds", "restore_online", "Restore default online sound",
     "คืนค่าเสียงออนไลน์เริ่มต้น",
     "Clears the custom online sound."),
    ("options_presence_sounds", "restore_offline", "Restore default offline sound",
     "คืนค่าเสียงออฟไลน์เริ่มต้น",
     "Clears the custom offline sound."),
    ("options_presence_sounds", "back", "Back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- key binding menu (menus.keyconfig_menu) ---
    ("keyconfig", "menu_title", "Please select a function to bind a key to.",
     "โปรดเลือกฟังก์ชันที่จะผูกปุ่ม",
     "menus.py keyconfig_menu(): title. Each function line is dynamic ('<function>: <key>') and stays screen-reader speech."),
    ("keyconfig", "back", "Back",
     "ย้อนกลับ",
     "Returns to Options."),
    # --- drum keys (menus.drum_keyconfig_menu / drum_pad_keyconfig_menu) ---
    ("drum_keys", "menu_title", "Configure drum keys.",
     "ตั้งค่าปุ่มกลอง",
     "menus.py drum_keyconfig_menu(): title. Pad lines are dynamic (pad name + keys)."),
    ("drum_keys", "restore_defaults", "Restore default drum keys.",
     "คืนค่าปุ่มกลองเริ่มต้น",
     "Restores the default bindings."),
    ("drum_keys", "clear_all", "Clear drum keys.",
     "ล้างปุ่มกลองทั้งหมด",
     "Unbinds every pad."),
    ("drum_keys", "back", "Back",
     "ย้อนกลับ",
     "Returns to Options."),
    ("drum_keys", "pad_menu_prefix", "Configure",
     "ตั้งค่าปุ่ม",
     "Pad menu title prefix: 'Configure <pad> keys.'"),
    ("drum_keys", "pad_set_primary", "Set primary key.",
     "ตั้งปุ่มหลัก",
     "Line prefix; the current key follows live."),
    ("drum_keys", "pad_set_alternate", "Set alternate key.",
     "ตั้งปุ่มสำรอง",
     "Line prefix; the current key follows live."),
    ("drum_keys", "pad_clear_alternate", "Clear alternate key.",
     "ล้างปุ่มสำรอง",
     "Only shown when an alternate key exists."),
    ("drum_keys", "pad_back", "Back",
     "ย้อนกลับ",
     "Returns to the pad list."),
]

HEADER = """screen: {screen}
slug: {slug}
english: {english}
thai: {thai}
note: {note}

How to use: generate this clip on the TTS site with either text above
(English matches the game's own words exactly; Thai is a faithful rendering),
download it next to this file, then run convert_to_ogg.py from the pack root
to turn it into the Ogg Vorbis mono 48 kHz file the game's decoder reads.
This placeholder is replaced by <slug>.ogg when the converted clip lands.
"""

README = """# UI voice pack (staging)

เสียงพูดสำหรับปุ่มและข้อความทุกอันของหน้าเมนูก่อนเข้าเกม (ฝั่ง client):
เปิดเกมครั้งแรก, เมนูหลัก, ล็อกอิน/สร้างบัญชี, Options ทั้งต้นไม้,
ทดสอบลำโพง และตัวอัปเดต

**โฟลเดอร์นี้ยังไม่ถูกเกมอ่าน** -- ยังไม่ได้เด็ดขาดเสียงลงในเกม (ตามที่สั่งไว้)
เป็นที่รวมคลิปก่อนย้ายเข้า data/ ในภายหลังเท่านั้น

## วิธีใช้ (ลำดับการทำ)
1. เปิด https://luminatts.vercel.app/ แล้วเลือกเสียงที่ต้องการ
   (ไม่ล็อกอินก็ generate ได้ด้วยเสียงระบบ -- มีกระเป๋าเครดิตให้ใช้
   ล็อกอินเฉพาะเมื่อจะใช้เสียงที่โคลนไว้ในบัญชี)
2. เปิด `CHECKLIST.md` แล้วไล่ทีละบรรทัด: ก๊อปข้อความจากไฟล์ `.txt`
   ของปุ่มนั้น (มีทั้งอังกฤษตามคำของเกม และคำแปลไทย) ใส่ในเว็บ กด
   Generate ฟัง แล้วดาวน์โหลด
3. บันทึกไฟล์ลงโฟลเดอร์ของหน้าจอนั้น ตั้งชื่อให้ตรง slug
   เช่น `main_menu/login.mp3` (นามสกุลอะไรก็ได้)
4. พอได้ครบก้อน รัน `python convert_to_ogg.py` ในโฟลเดอร์นี้
   มันจะแปลงทุกไฟล์เป็น Ogg Vorbis โมโน 48 kHz ชื่อเดียวกับ placeholder
   (ตัวถอดรหัสของเกมอ่านได้เฉพาะ Vorbis -- ไฟล์ Opus/WebM เปิดไม่ออกเลย)
   แล้วลบไฟล์ดิบทิ้งให้เอง

## ทำไมต้องกดเอง (ข้อกติกาของเว็บ)
FAQ ของ Lumina TTS ระบุว่าการใช้งานต้องทำผ่านหน้าเว็บโดยผู้ใช้จริง
(มนุษย์) เท่านั้น ห้ามใช้สคริปต์อัตโนมัติ/บอท หากตรวจพบจะระงับบัญชีทันที
(Terms ข้อ 7) -- การให้เอเจนต์สั่ง generate แทนถือเป็นการอัตโนมัติ
ตามเงื่อนไขข้อนี้ จึงต้องเหลือรอบกด ๆ ให้ทำเอง
ส่วนการโคลนเสียงใหม่มีขั้นยืนยันเจ้าของเสียง (อ่านข้อความยินยอม
ออกเสียงจริงภายใน 15 นาที) ซึ่งทำแทนกันไม่ได้อยู่แล้ว

## โครงสร้าง
- หนึ่งโฟลเดอร์ต่อหน้าจอ (`main_menu/`, `options/`, ...)
- หนึ่งไฟล์ `.txt` ต่อหนึ่งปุ่ม/ข้อความ ชื่อเดียวกับ `.ogg` ที่มันจะกลายเป็น
  ข้างในมีข้อความอังกฤษ (คำของเกมตามโค้ดเป๊ะ ๆ) + คำแปลไทย + หมายเหตุว่าอยู่ตรงไหน
- `CHECKLIST.md` คิวการทำเรียงตามความสำคัญ (ชั้น 1 ครอบคลุมประสบการณ์
  เข้าเกมเบื้องต้นทั้งหมด ~40 คลิป, ชั้น 2 Options, ชั้น 3 เมนูย่อยลึก)
- `MANIFEST.md` ตารางรวมทุกคลิป
- `_raw_downloads/` ที่ทิ้งไฟล์ดิบที่ยังไม่รู้จะจัดโฟลเดอร์ไหน

## รอบที่สอง (ยังไม่อยู่ในชุดนี้)
เมนูในเกม (กด Backspace), เมนูลงแผนที่/ช่างเทคนิค และเมนูจากเซิร์ฟเวอร์อื่น ๆ
ต้องเก็บแยกรอบหน้า
"""

# Checklist order: tier 1 covers the whole fresh-launch experience a player
# actually hears before touching Options; tier 2 is the Options tree; tier 3
# is the deep submenus that most players open once, if ever.
TIERS = (
    ("ชั้น 1 -- เข้าเกมถึงล็อกอิน (ทำก่อน)",
     ("startup", "main_menu", "accounts", "no_account", "create_account",
      "login", "restart_exit")),
    ("ชั้น 2 -- Options และอัปเดต",
     ("options", "updater", "speaker_test")),
    ("ชั้น 3 -- เมนูย่อยของ Options",
     ("options_buffer_timing", "options_hrtf", "options_sound_system",
      "options_output_devices", "options_input_devices",
      "options_instrument_inputs", "options_jitter_prompt",
      "options_location", "options_presence_sounds", "keyconfig",
      "drum_keys")),
)

CHECKLIST_HEADER = """# CHECKLIST -- คิวสร้างเสียง (กดกดเองที่เว็บ)

วิธีทำตามบรรทัด: เปิดไฟล์ .txt ของรายการนั้น ก๊อปข้อความ (อังกฤษหรือไทย)
ไปวางในเว็บ กด Generate ฟัง ดาวน์โหลด แล้วบันทึกเป็น
`<โฟลเดอร์>/<slug>.mp3` (นามสกุลอะไรก็ได้) -- ครบกี่ตัวก็รัน
`python convert_to_ogg.py` ได้เลย

หมายเหตุ: บางรายการ (เช่น ชื่ออุปกรณ์เสียง, Login with ชื่อผู้ใช้) มีส่วนที่
เปลี่ยนตามเครื่อง/บัญชีอยู่ในข้อความจริง -- ดูโน้ตในไฟล์ .txt ของรายการนั้น
ว่าส่วนต้นที่คงที่คืออะไร เสียงอัดได้เฉพาะส่วนต้นคงที่

"""

MANIFEST_HEADER = """# MANIFEST -- ทุกคลิปในชุดนี้

ภาษาที่แนะนำ: อังกฤษ (คำของเกมตามโค้ด) หรือไทย (คำแปลในไฟล์ .txt ของแต่ละปุ่ม)
รูปแบบไฟล์เป้าหมาย: Ogg Vorbis, mono, 48000 Hz (แปลงด้วย convert_to_ogg.py)

| โฟลเดอร์ | ไฟล์ | ข้อความอังกฤษ |
|---|---|---|
"""


def build():
    for folder, slug, english, thai, note in ENTRIES:
        target = PACK / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{slug}.txt").write_text(
            HEADER.format(screen=folder, slug=slug, english=english, thai=thai, note=note),
            encoding="utf-8", newline="\n",
        )
    (PACK / "_raw_downloads").mkdir(exist_ok=True)
    rows = "\n".join(
        f"| {folder} | {slug}.ogg | {english.replace('|', '/')} |"
        for folder, slug, english, _, _ in ENTRIES
    )
    (PACK / "MANIFEST.md").write_text(MANIFEST_HEADER + rows + "\n",
                                      encoding="utf-8", newline="\n")
    tier_of = {folder: tier for tier, folders in TIERS for folder in folders}
    sections = []
    for tier, folders in TIERS:
        lines = [f"## {tier}", ""]
        for folder in folders:
            items = [e for e in ENTRIES if e[0] == folder]
            if not items:
                continue
            lines.append(f"### {folder}/")
            for _, slug, english, _, _ in items:
                label = english if len(english) <= 60 else english[:57] + "..."
                lines.append(f"- [ ] `{slug}` -- {label}")
            lines.append("")
        sections.append("\n".join(lines))
    untiered = [e[0] for e in ENTRIES if e[0] not in tier_of]
    (PACK / "CHECKLIST.md").write_text(
        CHECKLIST_HEADER + "\n".join(sections)
        + (f"\n(ยังไม่ได้จัดชั้น: {', '.join(sorted(set(untiered)))})\n" if untiered else "\n"),
        encoding="utf-8", newline="\n",
    )
    tier1 = sum(1 for e in ENTRIES if tier_of.get(e[0]) == TIERS[0][0])
    (PACK / "README.md").write_text(
        README.replace("~40 คลิป", f"{tier1} คลิป"),
        encoding="utf-8", newline="\n",
    )
    print(f"wrote {len(ENTRIES)} placeholders across "
          f"{len({e[0] for e in ENTRIES})} screens into {PACK}")


if __name__ == "__main__":
    build()
