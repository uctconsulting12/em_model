import cv2
import psycopg2
import time

# ==========================
# CONFIG
# ==========================
VIDEO_SOURCE = "assets/day_2.avi"
ORG_ID = 1
CAM_ID = 1

# ==========================
# DB FUNCTIONS
# ==========================
def get_db_conn():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="test",
        user="postgres",
        password="admin123"
    )

def load_workstations_from_db(org_id=1, cam_id=1):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT name, x1, y1, x2, y2
            FROM workstations
            WHERE org_id=%s AND cam_id=%s
            ORDER BY name
        """, (org_id, cam_id))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {name: (x1, y1, x2, y2) for name, x1, y1, x2, y2 in rows}
    except Exception as e:
        print(f"DB Error: {e}")
        return {}

def save_workstations_to_db(workstations, org_id=1, cam_id=1):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        # Clear old for this cam
        cur.execute("DELETE FROM workstations WHERE org_id=%s AND cam_id=%s", (org_id, cam_id))
        
        # Insert new
        for name, (x1, y1, x2, y2) in workstations.items():
            cur.execute("""
                INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (org_id, cam_id, name, x1, y1, x2, y2))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Save Error: {e}")
        return False

# ==========================
# GLOBAL STATE
# ==========================
workstations = load_workstations_from_db(ORG_ID, CAM_ID)
drawing = False
start_point = None
current_mouse_rect = None # The box being dragged

# Mode: 'IDLE' or 'NAMING'
app_mode = 'IDLE' 
pending_rect = None       # The rect waiting for a name
input_text = ""           # The text buffer

status_msg = "Loaded ROIs."
status_time = time.time()

# ==========================
# MOUSE CALLBACK
# ==========================
def mouse_callback(event, x, y, flags, param):
    global drawing, start_point, current_mouse_rect
    global app_mode, pending_rect, input_text, workstations, status_msg, status_time

    # Only allow mouse interaction if we aren't currently typing a name
    if app_mode == 'NAMING':
        return

    # --- DELETE ROI (Right Click) ---
    if event == cv2.EVENT_RBUTTONDOWN:
        # Check if click is inside any box
        to_delete = None
        for name, (x1, y1, x2, y2) in workstations.items():
            if x1 < x < x2 and y1 < y < y2:
                to_delete = name
                break
        
        if to_delete:
            del workstations[to_delete]
            status_msg = f"Deleted '{to_delete}'"
            status_time = time.time()

    # --- DRAW ROI (Left Click Drag) ---
    elif event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        start_point = (x, y)

    elif event == cv2.EVENT_MOUSEMOVE and drawing:
        current_mouse_rect = (start_point[0], start_point[1], x, y)

    elif event == cv2.EVENT_LBUTTONUP:
        drawing = False
        x1, y1 = start_point
        # Normalize coordinates (handle dragging up/left)
        rect = (min(x1, x), min(y1, y), max(x1, x), max(y1, y))
        
        # Don't save tiny accidental clicks
        if (rect[2] - rect[0]) > 10 and (rect[3] - rect[1]) > 10:
            pending_rect = rect
            app_mode = 'NAMING'  # Switch to typing mode
            input_text = ""
            current_mouse_rect = None

# ==========================
# MAIN LOOP
# ==========================
cap = cv2.VideoCapture(VIDEO_SOURCE)
ret, base_frame = cap.read()
cap.release()

if not ret:
    raise RuntimeError("Could not read video frame")

cv2.namedWindow("ROI Manager")
cv2.setMouseCallback("ROI Manager", mouse_callback)

while True:
    display = base_frame.copy()

    # 1. Draw Saved Workstations (Green)
    for name, (x1, y1, x2, y2) in workstations.items():
        cv2.rectangle(display, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(display, name, (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    # 2. Draw dragging rectangle (Blue outline)
    if drawing and current_mouse_rect:
        x1, y1, x2, y2 = current_mouse_rect
        cv2.rectangle(display, (x1, y1), (x2, y2), (255, 0, 0), 1)

    # 3. Draw Pending Rectangle (Blue filled mostly) + Text Input
    if app_mode == 'NAMING' and pending_rect:
        x1, y1, x2, y2 = pending_rect
        cv2.rectangle(display, (x1, y1), (x2, y2), (255, 100, 0), 2)
        
        # Draw text input box
        cv2.putText(display, f"Name: {input_text}_", (x1, y1 - 10), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 100, 0), 2)
        
        cv2.putText(display, "Enter to Confirm | Esc to Cancel", (10, 30), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

    # 4. Draw Status Message (Fade out after 3s)
    if time.time() - status_time < 3.0:
        cv2.putText(display, status_msg, (10, display.shape[0] - 20), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    # 5. Instructions
    if app_mode == 'IDLE':
        help_text = "Drag: New | Right-Click: Delete | 'S': Save DB | 'Q': Quit"
        cv2.putText(display, help_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 2)

    cv2.imshow("ROI Manager", display)
    
    # --- KEYBOARD HANDLING ---
    key = cv2.waitKey(1) & 0xFF

    if app_mode == 'NAMING':
        if key == 13: # Enter Key
            if input_text.strip():
                workstations[input_text] = pending_rect
                status_msg = f"Added '{input_text}'"
                status_time = time.time()
            app_mode = 'IDLE'
            pending_rect = None
        elif key == 27: # Esc Key
            app_mode = 'IDLE'
            pending_rect = None
        elif key == 8: # Backspace
            input_text = input_text[:-1]
        elif key < 255: # Regular Characters
            # Filter for alphanumeric/basic chars
            char = chr(key)
            if char.isalnum() or char in "_ -":
                input_text += char

    elif app_mode == 'IDLE':
        if key == ord('s'):
            if save_workstations_to_db(workstations, ORG_ID, CAM_ID):
                status_msg = "Successfully Saved to Database!"
            else:
                status_msg = "Error Saving to DB"
            status_time = time.time()
        
        elif key == ord('q') or key == 27:
            break

cv2.destroyAllWindows()