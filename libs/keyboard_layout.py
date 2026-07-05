import pygame

# Maps Thai character keycodes (when Thai layout is active) back to their English physical key equivalents
THAI_TO_ENG_MAP = {
    3654: pygame.K_q,  # ๆ -> q
    3652: pygame.K_w,  # ไ -> w
    3635: pygame.K_e,  # ำ -> e
    3614: pygame.K_r,  # พ -> r
    3632: pygame.K_t,  # ะ -> t
    3633: pygame.K_y,  # ั -> y
    3637: pygame.K_u,  # ี -> u
    3619: pygame.K_i,  # ร -> i
    3609: pygame.K_o,  # น -> o
    3618: pygame.K_p,  # ย -> p
    3610: pygame.K_LEFTBRACKET,  # บ -> [
    3621: pygame.K_RIGHTBRACKET,  # ล -> ]
    3587: pygame.K_BACKSLASH,  # ฃ -> \
    3615: pygame.K_a,  # ฟ -> a
    3627: pygame.K_s,  # ห -> s
    3585: pygame.K_d,  # ก -> d
    3592: pygame.K_f,  # ด -> f
    3648: pygame.K_g,  # เ -> g
    3657: pygame.K_h,  # ้ -> h
    3656: pygame.K_j,  # ่ -> j
    3634: pygame.K_k,  # า -> k
    3626: pygame.K_l,  # ส -> l
    3623: pygame.K_SEMICOLON,  # ว -> ;
    3591: pygame.K_QUOTE,  # ง -> '
    3612: pygame.K_z,  # ผ -> z
    3611: pygame.K_x,  # ป -> x
    3649: pygame.K_c,  # แ -> c
    3629: pygame.K_v,  # อ -> v
    3636: pygame.K_b,  # ิ -> b
    3639: pygame.K_n,  # ื -> n
    3607: pygame.K_m,  # ท -> m
    3617: pygame.K_COMMA,  # ม -> ,
    3651: pygame.K_PERIOD,  # ใ -> .
    3613: pygame.K_SLASH,  # ฝ -> /
}

def normalize_events(events):
    """
    Normalizes pygame KEYDOWN and KEYUP events by replacing Thai keycodes with their standard QWERTY English keycodes.
    This fixes hotkeys when the user is in the Thai keyboard layout, while preserving event.unicode so typing still works.
    """
    normalized = []
    for e in events:
        if e.type in (pygame.KEYDOWN, pygame.KEYUP):
            mapped_key = THAI_TO_ENG_MAP.get(e.key, e.key)
            # Create a new event with the mapped key, preserving everything else (like unicode for chat typing)
            # We use getattr to safely fallback if an attribute doesn't exist on older pygame versions
            new_event = pygame.event.Event(
                e.type, 
                key=mapped_key, 
                unicode=getattr(e, 'unicode', ''), 
                scancode=getattr(e, 'scancode', 0), 
                mod=getattr(e, 'mod', 0)
            )
            normalized.append(new_event)
        else:
            normalized.append(e)
    return normalized
