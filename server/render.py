"""Drawing boxes with a unique, stable color per class."""
import colorsys

import cv2

# Curated high-contrast colors (BGR) for the most common classes.
_COMMON = {
    "person": (60, 20, 220),       # red
    "bicycle": (255, 144, 30),     # blue
    "car": (0, 215, 255),          # yellow
    "motorcycle": (255, 0, 255),   # magenta
    "bus": (0, 140, 255),          # orange
    "truck": (50, 205, 50),        # green
    "dog": (255, 255, 0),          # cyan
}


def class_colors(names):
    """One unique BGR color per class index."""
    used = set(_COMMON.values())
    colors = []
    k = 0
    for name in names:
        if name in _COMMON:
            colors.append(_COMMON[name])
            continue
        while True:  # golden-ratio hue walk, skipping any exact collisions
            h = (k * 0.618033988749895) % 1.0
            s = 0.65 + 0.35 * ((k // 3) % 2)
            v = 0.95 - 0.25 * ((k // 2) % 2)
            k += 1
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            c = (int(b * 255), int(g * 255), int(r * 255))
            if c not in used:
                break
        used.add(c)
        colors.append(c)
    return colors


def bgr_to_hex(c):
    b, g, r = c
    return f"#{r:02x}{g:02x}{b:02x}"


def draw(frame, dets, names, colors):
    h, w = frame.shape[:2]
    short = min(h, w)
    thick = max(2, round(short / 400))
    font_scale = max(0.45, short / 1400)
    font_thick = max(1, thick // 2)
    for x1, y1, x2, y2, score, cls in dets:
        cls = int(cls)
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        col = colors[cls]
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, thick)
        label = f"{names[cls]} {score:.2f}"
        (tw, th), bl = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, font_thick)
        ty = max(y1, th + bl + 4)
        cv2.rectangle(frame, (x1, ty - th - bl - 4), (x1 + tw + 6, ty), col, -1)
        text_col = (0, 0, 0) if (0.114 * col[0] + 0.587 * col[1] + 0.299 * col[2]) > 140 else (255, 255, 255)
        cv2.putText(frame, label, (x1 + 3, ty - bl - 1), cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_col,
                    font_thick, cv2.LINE_AA)
    return frame
