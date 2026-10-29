from flask import Flask, render_template, Response, jsonify, request, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import cv2
import pytesseract
import time
import datetime
import RPi.GPIO as GPIO
from picamera2 import Picamera2
import threading
import os

app = Flask(__name__)
app.secret_key = "super_secret_pfe_key"


free_spots = 6
web_frame = None
gate_is_open = False
last_seen_time = 0
gate_close_timer = 5
current_plate_display = "Scanning..."


reserved_spots = {}


reservation_plates = {}


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

def get_db_connection():
    conn = sqlite3.connect('users.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT, role TEXT)')
    conn.commit()
    conn.close()

init_db()

class User(UserMixin):
    def __init__(self, id, email, role):
        self.id = id
        self.email = email
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    user_data = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if user_data:
        return User(user_data['id'], user_data['email'], user_data['role'])
    return None

def is_admin():
    return current_user.is_authenticated and current_user.role == 'admin'


PLATES_FILE = "allowed_plates.txt"
DEFAULT_PLATES = ["ABC", "XYF", "HFK", "MEW", "PVR", "ACE", "3ACE"]

def load_plates():
    if not os.path.exists(PLATES_FILE):
        with open(PLATES_FILE, "w") as f:
            for p in DEFAULT_PLATES:
                f.write(p + "\n")
        return DEFAULT_PLATES.copy()
    else:
        with open(PLATES_FILE, "r") as f:
            return [line.strip() for line in f.readlines() if line.strip()]

def save_plates():
    with open(PLATES_FILE, "w") as f:
        for p in ALLOWED_PLATES:
            f.write(p + "\n")

ALLOWED_PLATES = load_plates()

def reservation_expiry_watcher():
    """Runs every 30 seconds. Removes reservation-only plates once their
    leave time has passed. Admin-added plates are never touched."""
    global reservation_plates, reserved_spots
    while True:
        try:
            now = time.time()
            expired_plates = [p for p, exp in list(reservation_plates.items()) if now >= exp]
            for plate in expired_plates:
                if plate in ALLOWED_PLATES:
                    ALLOWED_PLATES.remove(plate)
                    print(f"\n[EXPIRY] Plate {plate} reservation expired, removed from allowed list.")
                del reservation_plates[plate]

           
            for spot_num, plate in list(reserved_spots.items()):
                if plate not in ALLOWED_PLATES and plate not in reservation_plates:
                    del reserved_spots[spot_num]
        except Exception as e:
            print(f"[Expiry watcher error]: {e}")
        time.sleep(30)

threading.Thread(target=reservation_expiry_watcher, daemon=True).start()


SERVO_PIN = 17

SPOT_PINS = [22, 24, 26, 16, 19, 13]

custom_config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=ACEFHKMNPRUVWXY34'

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(SERVO_PIN, GPIO.OUT)
pwm = GPIO.PWM(SERVO_PIN, 50)
pwm.start(0)

for pin in SPOT_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

print("Starting Camera...")
try:
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(
        main={"format": 'RGB888', "size": (640, 480)},
        controls={"FrameRate": 30}
    )
    picam2.configure(config)
    picam2.start()
    time.sleep(2)
except Exception as e:
    print(f"CAMERA ERROR: {e}")
    exit()


lcd_active = False
lcd_lock = threading.Lock()   # prevent concurrent writes to LCD

try:
    from RPLCD.i2c import CharLCD
    try:
        lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1,
                      cols=16, rows=2, charmap='A02')
        lcd_active = True
    except Exception:
        lcd = CharLCD(i2c_expander='PCF8574', address=0x3F, port=1,
                      cols=16, rows=2, charmap='A02')
        lcd_active = True
    if lcd_active:
        lcd.clear()
        print("LCD initialized successfully.")
except Exception as e:
    print(f"LCD not available: {e}")



def lcd_write(line1: str, line2: str):
    """Write two padded lines to the 16×2 LCD."""
    if not lcd_active:
        return
    with lcd_lock:
        try:
            lcd.clear()
            lcd.write_string(line1[:16].ljust(16))
            lcd.crlf()
            lcd.write_string(line2[:16].ljust(16))
        except Exception as e:
            print(f"[LCD write error]: {e}")

def lcd_idle():
    """Revert to the default welcome/idle screen."""
    lcd_write("Smart Parking   ", "  Bienvenue!    ")

_lcd_reset_timer = None

def lcd_show_timed(line1: str, line2: str, duration: int = 12):
    """Display a message for `duration` seconds, then show idle screen."""
    global _lcd_reset_timer
    if _lcd_reset_timer is not None:
        _lcd_reset_timer.cancel()
    lcd_write(line1, line2)
    _lcd_reset_timer = threading.Timer(duration, lcd_idle)
    _lcd_reset_timer.daemon = True
    _lcd_reset_timer.start()


SPOT_PRIORITY = [1, 4, 2, 5, 3, 6]

SPOT_DIRECTIONS = {
    1: ("Droite>Droite  ", "Place P1       "),
    2: ("Droite>Droite  ", "Place P2       "),
    3: ("Droite>Droite  ", "Place P3       "),
    4: ("Droite>Gauche  ", "Place P4       "),
    5: ("Droite>Gauche  ", "Place P5       "),
    6: ("Droite>Gauche  ", "Place P6       "),
}

def guide_vehicle(plate: str):
    """
    Called in a background thread immediately after the gate opens.

    1. If the plate belongs to a reservation → guide to that specific spot.
    2. If not reserved → find the highest-priority free spot and guide there.
    3. If full → show 'Parking complet' message.
    """
    vals = [GPIO.input(p) for p in SPOT_PINS]   # fresh sensor read

    spot_for_plate = None
    for spot_num, p in reserved_spots.items():
        if p == plate:
            spot_for_plate = spot_num
            break

    if spot_for_plate is not None:
        dir_line, place_line = SPOT_DIRECTIONS[spot_for_plate]
        # Line 1: "Res. Place P1   "  (16 chars)
        line1 = ("Res. " + place_line).ljust(16)[:16]
        line2 = dir_line
        lcd_show_timed(line1, line2, duration=15)
        print(f"[LCD GUIDE] Reserved → '{line1}' | '{line2}'")
        return


    for spot_num in SPOT_PRIORITY:
        sensor_idx = spot_num - 1          # 0-based index into SPOT_PINS
        if vals[sensor_idx] == 1:          # 1 = free (pull-up, no car)
            dir_line, place_line = SPOT_DIRECTIONS[spot_num]
            line1 = ("Libre: " + place_line).ljust(16)[:16]
            line2 = dir_line
            lcd_show_timed(line1, line2, duration=12)
            print(f"[LCD GUIDE] Priority → '{line1}' | '{line2}'")
            return


    lcd_show_timed("Parking complet!", "Desole, complet.", duration=10)
    print("[LCD GUIDE] Parking full – message displayed.")


lcd_idle()

def move_gate_up():
    global gate_is_open
    if not gate_is_open:
        pwm.ChangeDutyCycle(2)
        time.sleep(0.3)
        pwm.ChangeDutyCycle(0)
        gate_is_open = True
        print("\n✅ GATE OPENED")

def move_gate_down():
    global gate_is_open
    if gate_is_open:
        pwm.ChangeDutyCycle(7)
        time.sleep(0.3)
        pwm.ChangeDutyCycle(0)
        gate_is_open = False
        print("\n🔒 GATE CLOSED")

def parking_system():
    global free_spots, web_frame, gate_is_open, last_seen_time
    global current_plate_display, reserved_spots

    plate_display_expire = 0
    frame_count = 0
    ocr_frame_skip = 2
    prev_vals = [1] * 6  

    while True:
        try:
            current_time = time.time()
            frame_count += 1

            vals = [GPIO.input(p) for p in SPOT_PINS]
            free_spots = sum(vals)
            is_full = (free_spots == 0)

            # Detect when car physically arrives at a reserved spot → clear it
            for i, (prev, curr) in enumerate(zip(prev_vals, vals)):
                spot_num = i + 1
                if prev == 1 and curr == 0:
                    if spot_num in reserved_spots:
                        print(f"\n🚗 Car arrived at reserved spot P{spot_num}, clearing reservation.")
                        del reserved_spots[spot_num]
            prev_vals = vals[:]

            frame = picam2.capture_array()
            frame_display = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            if current_time > plate_display_expire:
                current_plate_display = "Scanning..."

            if current_plate_display in ALLOWED_PLATES:
                last_seen_time = current_time

            if frame_count % ocr_frame_skip == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)

                text = pytesseract.image_to_string(thresh, config=custom_config)
                results = [text.strip()] if text.strip() else []

                if results:
                    for text in results:
                        text = fix_ocr_mistakes(text)
                        clean = text.replace(" ", "").replace("\n", "").upper()

                        if len(clean) > 1:
                            current_plate_display = clean
                            plate_display_expire = current_time + 2

                            if clean in ALLOWED_PLATES:
                                last_seen_time = current_time
                                if not is_full:
                                    gate_was_closed = not gate_is_open
                                    move_gate_up()
                                    # Only trigger LCD guidance at the instant gate opens
                                    if gate_was_closed:
                                        threading.Thread(
                                            target=guide_vehicle,
                                            args=(clean,),
                                            daemon=True
                                        ).start()

            web_frame = frame_display.copy()

            if gate_is_open and (current_time - last_seen_time) > gate_close_timer:
                move_gate_down()

            time.sleep(0.03)

        except Exception as e:
            print(f"[Loop Error]: {e}")
            time.sleep(1)

def fix_ocr_mistakes(text):
    text = text.replace("|", "I").replace("!", "I").replace(")", "")
    text = text.replace("(", "").replace("[", "").replace("]", "")
    return text

threading.Thread(target=parking_system, daemon=True).start()


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]
        if password != confirm_password:
            error = "Passwords do not match!"
        else:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            user_count = cur.fetchone()[0]
            role = 'admin' if user_count == 0 else 'user'
            try:
                conn.execute('INSERT INTO users (email, password, role) VALUES (?, ?, ?)',
                             (email, generate_password_hash(password), role))
                conn.commit()
                conn.close()
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "Email already registered."
                conn.close()
    return render_template("register.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        conn = get_db_connection()
        user_data = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()
        if user_data and check_password_hash(user_data['password'], password):
            user = User(user_data['id'], user_data['email'], user_data['role'])
            login_user(user)
            return redirect(url_for("home"))
        else:
            error = "Invalid email or password."
    return render_template("login.html", error=error)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    users = []
    if is_admin():
        conn = get_db_connection()
        users = conn.execute('SELECT id, email, role FROM users').fetchall()
        conn.close()
    return render_template("index.html", role=current_user.role, users=users)

def generate_frames():
    global web_frame
    while True:
        if web_frame is not None:
            ret, buffer = cv2.imencode('.jpg', web_frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.05)

@app.route("/video_feed")
@login_required
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/api/status")
@login_required
def status_api():
    vals = [GPIO.input(p) for p in SPOT_PINS]
    return jsonify({
        "s1": vals[0], "s2": vals[1], "s3": vals[2],
        "s4": vals[3], "s5": vals[4], "s6": vals[5],
        "plate":   current_plate_display,
        "gate":    "OPEN" if gate_is_open else "CLOSED",
        "parking": "FULL!" if sum(vals) == 0 else "OPEN",
        "r1": 1 in reserved_spots,
        "r2": 2 in reserved_spots,
        "r3": 3 in reserved_spots,
        "r4": 4 in reserved_spots,
        "r5": 5 in reserved_spots,
        "r6": 6 in reserved_spots,
    })

def manual_gate_cycle():
    global last_seen_time
    move_gate_up()
    last_seen_time = time.time() + 10
    time.sleep(3)
    move_gate_down()
    last_seen_time = time.time() - 10

@app.route("/toggle_gate", methods=["POST"])
@login_required
def toggle_gate():
    if not is_admin():
        return jsonify({"status": "error", "message": "Admin privileges required"}), 403
    threading.Thread(target=manual_gate_cycle).start()
    return jsonify({"status": "success"})


@app.route("/api/plates")
@login_required
def get_plates():
    if not is_admin():
        return jsonify({"status": "error", "message": "Admin privileges required"}), 403
    return jsonify({"plates": ALLOWED_PLATES})

@app.route("/api/add_plate", methods=["POST"])
@login_required
def add_plate():
    if not is_admin():
        return jsonify({"status": "error"}), 403
    data = request.get_json()
    new_plate = data.get("plate", "").strip().upper()
    if new_plate and new_plate not in ALLOWED_PLATES:
        ALLOWED_PLATES.append(new_plate)
        save_plates()
    return jsonify({"status": "success"})

@app.route("/api/remove_plate", methods=["POST"])
@login_required
def remove_plate():
    if not is_admin():
        return jsonify({"status": "error"}), 403
    data = request.get_json()
    plate_to_remove = data.get("plate", "").strip().upper()
    if plate_to_remove in ALLOWED_PLATES:
        ALLOWED_PLATES.remove(plate_to_remove)
        save_plates()
    return jsonify({"status": "success"})

@app.route("/api/toggle_role/<int:user_id>", methods=["POST"])
@login_required
def toggle_role(user_id):
    if not is_admin():
        return jsonify({"status": "error"}), 403
    conn = get_db_connection()
    user = conn.execute('SELECT role FROM users WHERE id = ?', (user_id,)).fetchone()
    if user:
        new_role = 'user' if user['role'] == 'admin' else 'admin'
        conn.execute('UPDATE users SET role = ? WHERE id = ?', (new_role, user_id))
        conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/reserve", methods=["POST"])
@login_required
def reserve_spot():
    global reserved_spots, reservation_plates
    data = request.get_json()
    spot_num = int(data.get('spot', 0))
    plate    = data.get('plate', '').strip().upper()
    arrive   = data.get('arrive', '')
    leave    = data.get('leave', '')

    if spot_num < 1 or spot_num > 6 or not plate:
        return jsonify({"status": "error", "message": "Invalid spot or plate"}), 400

    vals = [GPIO.input(p) for p in SPOT_PINS]
    if vals[spot_num - 1] == 0:
        return jsonify({"status": "error", "message": "Spot is already occupied"}), 400

    reserved_spots[spot_num] = plate
    print(f"\n[RESERVATION] User {current_user.email} reserved Spot P{spot_num} "
          f"for Plate {plate} from {arrive} to {leave}.")

    # Compute expiry from leave time (HH:MM), push to tomorrow if already past
    try:
        now_dt = datetime.datetime.now()
        leave_h, leave_m = map(int, leave.split(':'))
        expire_dt = now_dt.replace(hour=leave_h, minute=leave_m, second=0, microsecond=0)
        if expire_dt <= now_dt:
            expire_dt += datetime.timedelta(days=1)
        expire_ts = expire_dt.timestamp()
    except Exception:
        expire_ts = time.time() + 12 * 3600   # fallback: 12 hours

   
    if plate not in ALLOWED_PLATES:
        ALLOWED_PLATES.append(plate)
        reservation_plates[plate] = expire_ts

    return jsonify({"status": "success"})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
