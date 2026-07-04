import pygame
import os

class UIRenderer:
    """
    UI Renderer - จัดการการวาดกราฟิกทั้งหมดให้ Beyond Tournament
    Designed for Blind Developers: Extensive Logging & Fallback support
    """
    def __init__(self, screen):
        self.screen = screen
        self.width = screen.get_width()
        self.height = screen.get_height()
        
        # Load Fonts
        try:
            self.font_btn = pygame.font.SysFont("calibri", 24)
            self.font_logo = pygame.font.SysFont("impact", 60)
            self.font_debug = pygame.font.SysFont("consolas", 12)
        except:
            print("[UI] ⚠ Failed to load system fonts, using default")
            self.font_btn = pygame.font.Font(None, 24)
            self.font_logo = pygame.font.Font(None, 60)
            self.font_debug = pygame.font.Font(None, 18)

        # Assets Cache
        self.images = {}
        
        # Status Tracking (ป้องกันการวาดซ้ำซ้อน)
        self.current_screen = "unknown"
        self.last_screen = "unknown"

        print("[UI] Initializing UIRenderer...")
        self.load_assets()

    def load_assets(self):
        """โหลดภาพ UI ทั้งหมด พร้อม Log ละเอียด"""
        assets_path = "assets/ui/"
        
        # รายชื่อภาพที่ต้องการ
        target_images = {
            'bg_menu': 'bg_menu.png',
            'bg_game': 'bg_game.png',
            'btn_normal': 'btn_normal.png',
            'btn_hover': 'btn_hover.png',
            'btn_pressed': 'btn_pressed.png',
            'logo': 'logo.png'
        }
        
        print("\n[UI] --- Loading Assets ---")
        for key, filename in target_images.items():
            full_path = os.path.join(assets_path, filename)
            self.images[key] = self.load_image(full_path)
            
        print("[UI] --- Asset Load Complete ---\n")

    def load_image(self, path):
        """โหลดภาพแบบ Safe Mode"""
        try:
            if os.path.exists(path):
                img = pygame.image.load(path).convert_alpha()
                print(f"[UI] ✅ Loaded: {path} ({img.get_width()}x{img.get_height()})")
                return img
            else:
                print(f"[UI] ❌ File not found: {path} (Will use fallback)")
                return None
        except Exception as e:
            print(f"[UI] ❌ Error loading {path}: {e}")
            return None

    def clear_screen(self):
        """เคลียร์หน้าจอ (สำคัญมากเพื่อป้องกันภาพซ้อน!)"""
        self.screen.fill("black")

    def set_screen_context(self, screen_name):
        """บอก Renderer ว่าตอนนี้อยู่หน้าไหน (เพื่อ Log)"""
        if self.current_screen != screen_name:
            self.last_screen = self.current_screen
            self.current_screen = screen_name
            print(f"[UI] 📺 Screen Changed: {self.last_screen} -> {self.current_screen}")

    def draw_background(self, context="menu"):
        """วาดพื้นหลังตามบริบท"""
        self.set_screen_context(context)
        
        bg_key = 'bg_game' if context == 'game' else 'bg_menu'
        img = self.images.get(bg_key)

        if img:
            # Scale background to fit screen
            scaled_bg = pygame.transform.scale(img, (self.width, self.height))
            self.screen.blit(scaled_bg, (0, 0))
        else:
            # Fallback Gradient
            self.draw_fallback_gradient(context)

    def draw_fallback_gradient(self, context):
        """วาด Gradient สวยๆ แทนภาพพื้นหลัง"""
        import random
        
        # สีที่สว่างขึ้นและดูดีขึ้น
        if context == 'menu':
            top_color = (25, 35, 55)      # น้ำเงินเข้ม
            mid_color = (15, 25, 45)      # น้ำเงินกลาง
            bottom_color = (5, 10, 20)    # เกือบดำ
        else:  # game
            top_color = (20, 40, 25)      # เขียวเข้ม
            mid_color = (10, 30, 15)
            bottom_color = (5, 15, 10)
        
        # วาด Gradient แบบ 3 สี (สวยกว่า 2 สี)
        for y in range(self.height):
            if y < self.height // 2:
                # ครึ่งบน: top -> mid
                ratio = y / (self.height // 2)
                r = int(top_color[0] * (1 - ratio) + mid_color[0] * ratio)
                g = int(top_color[1] * (1 - ratio) + mid_color[1] * ratio)
                b = int(top_color[2] * (1 - ratio) + mid_color[2] * ratio)
            else:
                # ครึ่งล่าง: mid -> bottom
                ratio = (y - self.height // 2) / (self.height // 2)
                r = int(mid_color[0] * (1 - ratio) + bottom_color[0] * ratio)
                g = int(mid_color[1] * (1 - ratio) + bottom_color[1] * ratio)
                b = int(mid_color[2] * (1 - ratio) + bottom_color[2] * ratio)
            
            pygame.draw.line(self.screen, (r, g, b), (0, y), (self.width, y))
        
        # เพิ่ม Subtle Stars/Particles (Fixed positions based on seed)
        random.seed(42)  # Fixed seed = same pattern every frame
        for _ in range(30):
            star_x = random.randint(0, self.width)
            star_y = random.randint(0, self.height // 2)  # Stars only in top half
            brightness = random.randint(40, 80)
            size = random.randint(1, 2)
            pygame.draw.circle(self.screen, (brightness, brightness, brightness + 20), 
                             (star_x, star_y), size)

    def draw_button(self, text, x, y, width=300, height=50, selected=False, pressed=False):
        """วาดปุ่ม 3D"""
        
        # เลือกภาพที่เหมาะสม
        if pressed:
            img = self.images.get('btn_pressed')
        elif selected:
            img = self.images.get('btn_hover')
        else:
            img = self.images.get('btn_normal')

        # วาดปุ่ม
        if img:
            scaled_btn = pygame.transform.scale(img, (width, height))
            self.screen.blit(scaled_btn, (x, y))
        else:
            # 🎨 Enhanced Fallback Button Drawing (3D Effect)
            
            # กำหนดสี
            if pressed:
                main_color = (30, 50, 100)
                light_color = (50, 70, 130)
                dark_color = (15, 25, 50)
                glow_color = (60, 100, 180)
            elif selected:
                main_color = (50, 90, 160)
                light_color = (80, 130, 200)
                dark_color = (30, 50, 100)
                glow_color = (100, 150, 255)
            else:
                main_color = (45, 50, 60)
                light_color = (65, 70, 80)
                dark_color = (25, 28, 35)
                glow_color = None
            
            # วาดเงาด้านล่าง (3D Depth)
            shadow_rect = pygame.Rect(x + 3, y + 3, width, height)
            pygame.draw.rect(self.screen, (10, 10, 15), shadow_rect, border_radius=6)
            
            # วาดพื้นหลักของปุ่ม
            main_rect = pygame.Rect(x, y, width, height)
            pygame.draw.rect(self.screen, main_color, main_rect, border_radius=6)
            
            # วาด Highlight ด้านบน (ทำให้ดูนูน)
            highlight_rect = pygame.Rect(x + 2, y + 2, width - 4, height // 3)
            pygame.draw.rect(self.screen, light_color, highlight_rect, border_radius=4)
            
            # วาดเส้นขอบ
            border_color = glow_color if selected else (80, 85, 95)
            pygame.draw.rect(self.screen, border_color, main_rect, 2, border_radius=6)
            
            # ถ้า Selected ให้มี Glow Effect
            if selected and glow_color:
                glow_rect = pygame.Rect(x - 2, y - 2, width + 4, height + 4)
                pygame.draw.rect(self.screen, glow_color, glow_rect, 1, border_radius=8)

        # วาดข้อความ
        text_color = (255, 255, 220) if selected else (190, 195, 200)
        text_surf = self.font_btn.render(text, True, text_color)
        text_rect = text_surf.get_rect(center=(x + width//2, y + height//2))
        
        # เพิ่มเงาให้ข้อความ
        shadow_surf = self.font_btn.render(text, True, (20, 20, 25))
        shadow_rect = shadow_surf.get_rect(center=(x + width//2 + 1, y + height//2 + 1))
        self.screen.blit(shadow_surf, shadow_rect)
        self.screen.blit(text_surf, text_rect)
        
        # Debug Log (เฉพาะตอนเปลี่ยน selection เพื่อไม่ให้รก)
        # print(f"[UI] [{self.current_screen}] Button '{text}' at ({x},{y}) Selected={selected}")

    def draw_logo(self, y=50):
        """วาด Logo เกม"""
        img = self.images.get('logo')
        if img:
            x = (self.width - img.get_width()) // 2
            self.screen.blit(img, (x, y))
        else:
            # Fallback Text Logo
            logo_surf = self.font_logo.render("Beyond Tournament", True, (255, 50, 50))
            # Add Shadow
            shadow_surf = self.font_logo.render("Beyond Tournament", True, (0, 0, 0))
            
            x = (self.width - logo_surf.get_width()) // 2
            self.screen.blit(shadow_surf, (x+4, y+4))
            self.screen.blit(logo_surf, (x, y))

    def debug_draw_grid(self):
        """วาดเส้น Grid เพื่อช่วยเช็คตำแหน่ง (เปิด/ปิดได้)"""
        for x in range(0, self.width, 100):
            pygame.draw.line(self.screen, (50, 50, 50), (x, 0), (x, self.height))
        for y in range(0, self.height, 100):
            pygame.draw.line(self.screen, (50, 50, 50), (0, y), (self.width, y))
