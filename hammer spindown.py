"""
Калибровка (spin down) станка Saris/CycleOps Hammer по ANT+ или по Bluetooth.

ANT+: нужен ANT+ донгл (тот же, что для Zwift), калибровка по стандартному ANT+ FE-C.
Bluetooth: калибровка фирменными командами CycleOps, как в приложении Saris
(стандартного FTMS у Hammer нет).

Перед запуском закрыть Zwift: донгл одновременно может использовать только одна программа,
а по Bluetooth станок держит только одно подключение (телефон тоже отключить).

Перед калибровкой скрипт даёт 10 минут прогрева: холодный станок тормозит сильнее,
и время выбега выходит другим. Прогрев можно пропустить, если станок уже тёплый.

  python "hammer spindown.py"                окно: крупная скорость и цветная подсказка, что делать
  python "hammer spindown.py" --info         проверить связь со станком (в консоли)
  python "hammer spindown.py" --console      откалибровать в консоли
  python "hammer spindown.py" --ant / --ble  в консоли сразу выбрать ANT+ или Bluetooth
  python "hammer spindown.py" --no-warmup    без прогрева перед калибровкой
  python "hammer spindown.py" --en / --ru    язык: English / русский (в окне — переключатель внизу)

Всё важное дописывается в "hammer log.txt" рядом со скриптом.
Тексты на экране — русские, английские переводы в словаре EN в конце файла.
"""

import asyncio
import json
import os
import queue
import struct
import sys
import threading
import time
import traceback
from datetime import datetime

SEARCH_TIMEOUT = 30.0     # поиск станка, сек
CAL_TIMEOUT = 600.0       # вся калибровка вместе с прогревом, сек
INFO_SECONDS = 10.0       # сколько показывать данные в режиме проверки связи
LIVE_PERIOD = 0.25        # как часто обновлять скорость на экране, сек
WARMUP_SECONDS = 600.0    # прогрев перед калибровкой (Saris тоже советует 10 минут), сек
WARMUP_MIN_SPEED = 10.0   # прогрев засчитывается, только пока крутишь хотя бы так быстро, км/ч

# ANT+
START_TIMEOUT = 20.0      # станок должен начать калибровку после запроса, сек
ANT_RESPONSE_TIMEOUT = 2.0  # ответ донгла на команду, сек
TX_RETRIES = 6            # попытки отправить запрос калибровки

# Bluetooth
CONNECT_TIMEOUT = 20.0    # подключение, сек
CO_RESPONSE_TIMEOUT = 10.0  # ответ станка на команду, сек (как в приложении Saris)
RETRY_DELAYS = (1.0, 2.0, 5.0)   # паузы между повторами команды (как в приложении Saris)
POLL_INTERVAL = 1.0       # опрос этапа калибровки, сек (приложение Saris опрашивает раз в 3 сек)
POLL_TIMEOUT = 1.5        # ответ на опрос, сек: пропуск не страшен, следующий опрос через секунду
CAL_SPEED = 34.0          # с этой скорости приложение Saris запускает тест выбега, км/ч
CAL_WATTS = 50            # нагрузка, которую приложение Saris ставит перед калибровкой, Вт
WHEEL_M = 2.096           # окружность колеса, по которой приложение Saris считает скорость

# рядом со скриптом, а в собранном exe — рядом с exe: его __file__ лежит во временной папке
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
LOG_PATH = os.path.join(APP_DIR, "hammer log.txt")
SETTINGS_PATH = os.path.join(APP_DIR, "hammer settings.json")
_log = None
_live_on = False

STOP = threading.Event()  # кнопка «Стоп» в окне
LANG = "ru"               # язык: ru / en, выбирается при запуске (pick_lang), в окне переключается
LANG_NAMES = {"ru": "Русский", "en": "English"}


class Stopped(Exception):
    pass


def check_stop():
    if STOP.is_set():
        raise Stopped()


def T(text, **kw):
    """Текст на выбранном языке: русский текст сам служит ключом, английский берётся из EN."""
    if LANG == "en":
        text = EN.get(text, text)
    return text.format(**kw) if kw else text


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    settings = load_settings()
    settings.update(changes)
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


def pick_lang(args):
    """--en / --ru, иначе выбор, сохранённый в окне, иначе язык Windows."""
    if "--en" in args:
        return "en"
    if "--ru" in args:
        return "ru"
    saved = load_settings().get("lang")
    if saved in ("ru", "en"):
        return saved
    try:
        import ctypes
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except Exception:
        return "en"
    return "ru" if primary == 0x19 else "en"      # 0x19 — русский


def say(msg=""):
    """На экран (в консоль или окно) и в лог."""
    ui.message(msg)
    log_only(msg)


def log_only(msg):
    """Только в лог (с временем): техническая диагностика."""
    global _log
    try:
        if _log is None:
            _log = open(LOG_PATH, "a", encoding="utf-8")
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in msg.split("\n"):
            _log.write(f"{stamp} {line}\n" if line.strip() else "\n")
        _log.flush()
    except OSError:
        pass


def fmt(v, digits=0):
    return "-" if v is None else f"{v:.{digits}f}"


def close_window():
    try:
        input(T("\nНажми Enter, чтобы закрыть..."))
    except EOFError:
        pass


def u16(lo, hi):
    return lo | (hi << 8)


def mmss(seconds, up=False):
    """Минуты:секунды; up — с округлением вверх, для обратного отсчёта."""
    m, s = divmod(int(max(0.0, seconds) + (0.99 if up else 0)), 60)
    return f"{m}:{s:02d}"


def beep(kind):
    """Звук, чтобы не нужно было смотреть на экран: coast / ok / fail / warm (прогрев закончен)."""
    try:
        import winsound
    except ImportError:
        return
    tones = {"coast": [(1200, 250), (0, 120)] * 3, "ok": [(880, 150), (1320, 300)], "fail": [(440, 500)],
             "warm": [(660, 150), (880, 150), (1100, 300)]}[kind]

    def play():
        for freq, ms in tones:
            if freq:
                winsound.Beep(freq, ms)
            else:
                time.sleep(ms / 1000)

    threading.Thread(target=play, daemon=True).start()


# фазы калибровки, которые показываются подсказкой у скорости
HINTS = {"accelerate": "быстрее", "hold": "держи", "warmup": "держи, прогрев", "coast": "НЕ КРУТИ, жди"}


class ConsoleUI:
    """Вывод в консоль, как скрипт работал всегда."""

    def message(self, msg):
        global _live_on
        if _live_on:
            print()
            _live_on = False
        print(msg)

    def live(self, speed, target, power, phase, cadence=None, show_cadence=False):
        global _live_on
        line = "  " + (f"{speed:5.1f}" if speed is not None else "  -  ") + " " + T("км/ч")
        if show_cadence:
            line += " | " + (f"{cadence:3d}" if cadence is not None else "  -") + " " + T("об/мин")
        line += " | " + (f"{power:4d}" if power is not None else "   -") + " " + T("Вт")
        if target:
            line += " | " + T("цель {v:.1f}", v=target)
        if phase in HINTS:
            line += " | " + T(HINTS[phase])
        print("\r" + line.ljust(74), end="", flush=True)
        _live_on = True

    def phase(self, key, text=None):
        if key == "coast":
            beep("coast")

    def finish(self, ok, detail="", value=None, title=None):
        if ok is not None:
            beep("ok" if ok else "fail")

    def wait_ready(self, prompt):
        try:
            input(T("\n{prompt} и нажми Enter, чтобы начать калибровку...", prompt=prompt))
        except EOFError:
            pass

    def begin_warmup(self):
        self.skip_pressed()     # сбросить случайно нажатые раньше клавиши
        print(T("Нажми Enter, чтобы закончить прогрев и калибровать сейчас."))

    def warmup(self, left, speed, power, riding):
        global _live_on
        line = ("  " + T("прогрев: осталось {left}", left=mmss(left, up=True)) + " | "
                + (f"{speed:5.1f}" if speed is not None else "  -  ") + " " + T("км/ч"))
        if not riding:
            line += " | " + T("крути педали")
        print("\r" + line.ljust(74), end="", flush=True)
        _live_on = True

    def skip_pressed(self):
        try:
            import msvcrt
        except ImportError:
            return False
        hit = False
        while msvcrt.kbhit():
            hit = msvcrt.getwch() in "\r\n" or hit
        return hit


ui = ConsoleUI()


class Warmup:
    """Прогрев перед калибровкой: засчитывается только время, пока крутишь педали."""

    def __init__(self):
        self.ridden = 0.0
        self.started = self.last = time.monotonic()
        self.last_print = 0.0
        self.minutes_said = 0
        say(T("Прогрев {min:.0f} мин: крути спокойно, в любом темпе, время идёт, только пока крутишь. "
              "Если станок уже тёплый, прогрев можно пропустить.", min=WARMUP_SECONDS / 60))
        ui.phase("prewarm")
        ui.begin_warmup()

    def tick(self, speed, power):
        """True, когда прогрев закончен или его пропустили."""
        now = time.monotonic()
        riding = speed is not None and speed >= WARMUP_MIN_SPEED
        if riding:
            self.ridden += now - self.last
        self.last = now
        took = T("педали крутились {ridden} из {total}", ridden=mmss(self.ridden), total=mmss(now - self.started))
        if ui.skip_pressed():
            say(T("Прогрев пропущен ({took}).", took=took))
            return True
        if self.ridden >= WARMUP_SECONDS:
            say(T(">>> Прогрев закончен ({took}), начинаю калибровку.", took=took))
            ui.phase("prepare", T("Прогрев закончен, начинаю калибровку…"))
            beep("warm")
            return True
        if int(self.ridden // 60) > self.minutes_said:
            self.minutes_said = int(self.ridden // 60)
            say(T("  прогрев: {done} мин из {total:.0f}", done=self.minutes_said, total=WARMUP_SECONDS / 60))
        if now - self.last_print >= LIVE_PERIOD:
            self.last_print = now
            ui.warmup(WARMUP_SECONDS - self.ridden, speed, power, riding)
        return False


# каждая библиотека нужна только для своего способа связи
try:
    import logging
    import usb.core
    import usb.util
    logging.getLogger("usb").setLevel(logging.CRITICAL)
except ImportError:
    usb = None

try:
    import bleak
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    bleak = None


# ================================ ANT+ ================================

ANT_VID = 0x0FCF
ANT_PIDS = {0x1008: "ANT USB2", 0x1009: "ANT USB-m"}
ANTPLUS_KEY = bytes([0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45])
_IS_64 = struct.calcsize("P") == 8
ZWIFT_LIBUSB0 = os.path.join(
    r"C:\Program Files (x86)\Zwift\Windows ANT Dongle Driver",
    "amd64" if _IS_64 else "x86",
    "libusb0.dll" if _IS_64 else "libusb0_x86.dll",
)

# сообщения ANT
MSG_EVENT = 0x40
MSG_ASSIGN = 0x42
MSG_PERIOD = 0x43
MSG_SEARCH_TIMEOUT = 0x44
MSG_FREQ = 0x45
MSG_NETWORK_KEY = 0x46
MSG_RESET = 0x4A
MSG_OPEN = 0x4B
MSG_CLOSE = 0x4C
MSG_REQUEST = 0x4D
MSG_BROADCAST = 0x4E
MSG_ACKNOWLEDGED = 0x4F
MSG_CHANNEL_ID = 0x51
MSG_STARTUP = 0x6F

# события канала
EV_SEARCH_TIMEOUT = 0x01
EV_TX_COMPLETED = 0x05
EV_TX_FAILED = 0x06
EV_CHANNEL_CLOSED = 0x07
EV_GO_TO_SEARCH = 0x08

CH = 0
FE_DEVICE_TYPE = 0x11     # ANT+ Fitness Equipment
FE_PERIOD = 8192          # 4 Гц
FE_FREQ = 57              # 2457 МГц

PAGE_CALIBRATION = 0x01
PAGE_CAL_PROGRESS = 0x02
PAGE_GENERAL = 0x10
PAGE_TRAINER = 0x19
PAGE_USER_CONFIG = 0x37
CAL_SPINDOWN = 0x80

# настройки, которые станок просит флагом «нужна настройка пользователя».
# На калибровку выбега не влияют, Zwift при подключении присылает свои.
DEFAULT_USER_KG = 75.0
DEFAULT_BIKE_KG = 9.0
DEFAULT_WHEEL_M = 0.67    # 700c

SPEED_LOW = 0x40
SPEED_OK = 0x80


def is_timeout(e):
    tcls = getattr(usb.core, "USBTimeoutError", None)
    if tcls is not None and isinstance(e, tcls):
        return True
    codes = {110, 116, 138, 10060}
    if getattr(e, "errno", None) in codes or abs(getattr(e, "backend_error_code", 0) or 0) in codes:
        return True
    text = str(e).lower()
    return "timeout" in text or "timed out" in text


# ---------------- ANT+: USB / донгл ----------------

def candidate_backends():
    found = []
    try:
        import usb.backend.libusb0 as libusb0
        system_dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "libusb0.dll")
        backend = None
        for path in (system_dll, ZWIFT_LIBUSB0):
            if os.path.exists(path):
                backend = libusb0.get_backend(find_library=lambda _name, p=path: p)
                if backend is not None:
                    found.append((f"libusb0 ({path})", backend))
                    break
        if backend is None:
            backend = libusb0.get_backend()
            if backend is not None:
                found.append(("libusb0", backend))
        if backend is not None:
            try:
                libusb0._lib.usb_set_debug(0)   # не сыпать в консоль сообщениями о таймаутах
            except Exception:
                pass
    except Exception as e:
        log_only(T("  libusb0 недоступен: {e}", e=e))
    try:
        import usb.backend.libusb1 as libusb1
        try:
            import libusb_package
            backend = libusb1.get_backend(find_library=libusb_package.find_library)
        except ImportError:
            backend = libusb1.get_backend()
        if backend is not None:
            found.append(("libusb-1.0", backend))
    except Exception as e:
        log_only(T("  libusb-1.0 недоступен: {e}", e=e))
    return found


def find_stick(quiet=False):
    """ANT+ донгл или None. Почему не нашёлся, пишет только в лог: решает вызывающий."""
    backends = candidate_backends()
    if not backends:
        log_only(T("  не нашлась ни одна библиотека libusb, работать с донглом нечем"))
        return None
    for name, backend in backends:
        try:
            devs = [d for d in usb.core.find(find_all=True, idVendor=ANT_VID, backend=backend)
                    if d.idProduct in ANT_PIDS]
        except Exception as e:
            log_only(T("  {name}: ошибка поиска USB ({e})", name=name, e=e))
            continue
        if devs:
            d = devs[0]
            if not quiet:
                say(T("Донгл: {model} (0fcf:{pid:04x}) через {name}", model=ANT_PIDS[d.idProduct],
                      pid=d.idProduct, name=name))
            return d
        log_only(T("  {name}: ANT+ донгл не виден", name=name))
    return None


class AntStick:
    def __init__(self, dev):
        self.dev = dev
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.buf = bytearray()
        self.thread = None
        self.intf_number = 0

    def open(self):
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            say(f"  set_configuration: {e}")
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.intf_number = intf.bInterfaceNumber
        try:
            usb.util.claim_interface(self.dev, self.intf_number)
        except usb.core.USBError as e:
            raise RuntimeError(T("донгл занят другой программой, закрой Zwift ({e})", e=e)) from None
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if self.ep_out is None or self.ep_in is None:
            raise RuntimeError(T("у донгла не найдены USB-эндпоинты"))
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        errors = 0
        while not self.stop.is_set():
            try:
                chunk = self.ep_in.read(64, timeout=500)
                errors = 0
            except usb.core.USBError as e:
                if is_timeout(e):
                    continue
                errors += 1
                if errors >= 10:
                    self.q.put(("error", str(e)))
                    return
                time.sleep(0.05)
                continue
            self.buf.extend(bytes(chunk))
            for msg in self._parse():
                self.q.put(msg)

    def _parse(self):
        out = []
        b = self.buf
        while True:
            i = b.find(0xA4)
            if i < 0:
                b.clear()
                break
            if i > 0:
                del b[:i]
            if len(b) < 2:
                break
            n = b[1]
            if n > 32:
                del b[0]
                continue
            total = n + 4
            if len(b) < total:
                break
            frame = bytes(b[:total])
            x = 0
            for v in frame:
                x ^= v
            if x != 0:
                del b[0]
                continue
            out.append((frame[2], frame[3:3 + n]))
            del b[:total]
        return out

    def send(self, msg_id, data):
        data = bytes(data)
        frame = bytes([0xA4, len(data), msg_id]) + data
        x = 0
        for v in frame:
            x ^= v
        self.ep_out.write(frame + bytes([x]), timeout=1000)

    def next_msg(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def command(self, msg_id, data):
        self.send(msg_id, data)
        deadline = time.monotonic() + ANT_RESPONSE_TIMEOUT
        while time.monotonic() < deadline:
            msg = self.next_msg(0.1)
            if msg is None:
                continue
            if msg[0] == "error":
                raise RuntimeError(T("ошибка USB: {e}", e=msg[1]))
            mid, d = msg
            if mid == MSG_EVENT and len(d) >= 3 and d[1] == msg_id:
                if d[2] != 0:
                    raise RuntimeError(T("донгл отклонил команду 0x{id:02x} (код {code})", id=msg_id, code=d[2]))
                return
        raise RuntimeError(T("донгл не ответил на команду 0x{id:02x}", id=msg_id))

    def reset(self):
        self.send(MSG_RESET, [0x00])
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            msg = self.next_msg(0.1)
            if msg and msg[0] == MSG_STARTUP:
                break
        time.sleep(0.3)
        while self.next_msg(0.05) is not None:
            pass

    def open_fe_channel(self):
        self.reset()
        self.command(MSG_NETWORK_KEY, [0x00] + list(ANTPLUS_KEY))
        self.command(MSG_ASSIGN, [CH, 0x00, 0x00])                       # приёмный канал, сеть 0
        self.command(MSG_CHANNEL_ID, [CH, 0x00, 0x00, FE_DEVICE_TYPE, 0x00])  # любой номер станка
        self.command(MSG_PERIOD, [CH] + list(struct.pack("<H", FE_PERIOD)))
        self.command(MSG_SEARCH_TIMEOUT, [CH, min(255, int(SEARCH_TIMEOUT / 2.5))])
        self.command(MSG_FREQ, [CH, FE_FREQ])
        self.command(MSG_OPEN, [CH])

    def close(self):
        for msg_id, data in ((MSG_CLOSE, [CH]), (MSG_RESET, [0x00])):
            try:
                self.send(msg_id, data)
                time.sleep(0.2)
            except Exception:
                pass
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        try:
            usb.util.release_interface(self.dev, self.intf_number)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


# ---------------- ANT+: станок ----------------

class AntTrainer:
    def __init__(self):
        self.got_data = False
        self.device_number = None
        self.speed = None
        self.cadence = None
        self.power = None
        self.calib_flags = None
        self.state = "idle"        # idle / requested / started / accelerate / coast
        self.result = None         # ("ok", время_выбега_мс, температура) или ("fail", флаги)
        self.target = None
        self.speed_cond = None
        self.temp_cond = None
        self.tx_event = None
        self.search_timeout = False
        self.usb_error = None
        self.lost_warned = False
        self.live = False
        self.last_print = 0.0
        self.last_raw = {}
        self.old_responses = set()   # страницы 0x01, увиденные до запроса калибровки
        self.page_counts = {}
        self.coast_at = None
        self.seen_low = False
        self.max_speed = 0.0
        self.max_cadence = 0
        self.max_power = 0

    def handle(self, msg):
        mid, d = msg
        if mid == "error":
            self.usb_error = d
            return
        if mid == MSG_EVENT and len(d) >= 3 and d[1] == 0x01:
            code = d[2]
            if code in (EV_TX_COMPLETED, EV_TX_FAILED):
                self.tx_event = code
            elif code in (EV_SEARCH_TIMEOUT, EV_CHANNEL_CLOSED):
                self.search_timeout = True
            elif code == EV_GO_TO_SEARCH and self.got_data and not self.lost_warned:
                self.lost_warned = True
                say(T(">>> Связь со станком пропала, ищу снова..."))
            return
        if mid == MSG_CHANNEL_ID and len(d) >= 5:
            self.device_number = u16(d[1], d[2])
            return
        if mid not in (MSG_BROADCAST, MSG_ACKNOWLEDGED) or len(d) < 9:
            return
        p = d[1:9]
        self.got_data = True
        self.lost_warned = False
        page = p[0]
        if page not in self.page_counts:
            log_only(T("  первая стр. 0x{page:02x}: {hex}", page=page, hex=p.hex(" ")))
        self.page_counts[page] = self.page_counts.get(page, 0) + 1
        if page == PAGE_GENERAL:
            v = u16(p[4], p[5])
            self.speed = None if v == 0xFFFF else v * 0.0036
            if self.speed is not None:
                self.max_speed = max(self.max_speed, self.speed)
        elif page == PAGE_TRAINER:
            self.cadence = None if p[2] == 0xFF else p[2]
            w = p[5] | ((p[6] & 0x0F) << 8)
            self.power = None if w == 0xFFF else w
            if self.cadence is not None:
                self.max_cadence = max(self.max_cadence, self.cadence)
            if self.power is not None:
                self.max_power = max(self.max_power, self.power)
            flags = p[6] >> 4
            if flags != self.calib_flags:
                log_only(T("  флаги станка: {flags:#x} (1 мощность, 2 сопротивление, 4 настройка пользователя)",
                           flags=flags))
            self.calib_flags = flags
        elif page in (PAGE_CAL_PROGRESS, PAGE_CALIBRATION):
            if self.last_raw.get(page) != p:
                self.last_raw[page] = p
                log_only(T("  [{state}] стр. 0x{page:02x}: {hex}", state=self.state, page=page, hex=p.hex(" ")))
            if self.state == "idle":
                if page == PAGE_CALIBRATION:
                    self.old_responses.add(p)
                return
            if page == PAGE_CAL_PROGRESS:
                self._progress(p)
            elif self.state != "requested" or p not in self.old_responses:
                self._response(p)

    def temp_ok(self):
        # 0 = не применимо, 1 = холодный, 2 = норма, 3 = перегрет
        return self.temp_cond in (None, 0, 2)

    def at_target(self):
        # Hammer в первой странице калибровки пишет «скорость в норме» даже на 15 км/ч,
        # поэтому сверяемся со своей скоростью из страницы 0x10
        if self.target is None or self.speed is None:
            return self.seen_low
        return self.speed >= self.target - 1.0

    def _progress(self, p):
        cond = p[2]
        t = u16(p[4], p[5])
        self.target = None if t == 0xFFFF else t * 0.0036
        self.speed_cond = cond & 0xC0
        if self.state == "requested":
            self.state = "started"
            msg = T(">>> Станок начал калибровку.")
            if self.target:
                msg += " " + T("Целевая скорость {v:.1f} км/ч.", v=self.target)
            say(msg)
            say(T("Разгоняйся до целевой скорости и держи её, пока не появится «ПЕРЕСТАНЬ КРУТИТЬ»."))
        temp_cond = (cond >> 4) & 0x03
        if temp_cond != self.temp_cond:
            prev = self.temp_cond
            self.temp_cond = temp_cond
            if temp_cond == 1:
                say(T(">>> Станок ещё холодный: держи целевую скорость, он прогреется. Скрипт скажет, когда отпускать."))
            elif temp_cond == 3:
                say(T(">>> Станок перегрет: перестань крутить, дай ему остыть 10 минут и запусти калибровку заново."))
            elif temp_cond == 2 and prev in (1, 3):
                say(T(">>> Станок прогрелся."))
        if self.speed_cond == SPEED_LOW:
            self.seen_low = True
        if self.state == "started" and self.speed_cond in (SPEED_LOW, SPEED_OK):
            self.state = "accelerate"
        # отпускать педали только когда станок прогрет и скорость действительно достигнута
        if self.state == "accelerate" and self.speed_cond == SPEED_OK and self.temp_ok() and self.at_target():
            self.state = "coast"
            self.coast_at = time.monotonic()
            log_only(T("  команда отпускать на скорости {v} км/ч, цель {target}",
                       v=fmt(self.speed, 1), target=fmt(self.target, 1)))
            say(T(">>> ПЕРЕСТАНЬ КРУТИТЬ: отпусти педали и жди, станок выбегает."))
            ui.phase("coast")

    def _response(self, p):
        flags = p[1]
        if flags & CAL_SPINDOWN:
            sd = u16(p[6], p[7])
            temp = None if p[3] == 0xFF else p[3] * 0.5 - 25
            self.result = ("ok", None if sd == 0xFFFF else sd, temp)
        else:
            self.result = ("fail", flags)

    def show_live(self):
        if not self.live:
            return
        now = time.monotonic()
        if now - self.last_print < LIVE_PERIOD:
            return
        self.last_print = now
        phase = None
        if self.state == "coast":
            phase = "coast"
        elif self.state in ("started", "accelerate"):
            if self.speed_cond == SPEED_LOW or (self.speed_cond == SPEED_OK and not self.at_target()):
                phase = "accelerate"
            elif self.speed_cond == SPEED_OK:
                phase = "hold" if self.temp_ok() else "warmup"
        # Hammer каденс не передаёт (всегда 0), тогда не показываем
        ui.live(self.speed, self.target, self.power, phase, self.cadence, show_cadence=self.max_cadence > 0)


def pump(stick, tr, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_stop()
        msg = stick.next_msg(0.1)
        if msg:
            tr.handle(msg)
        if tr.usb_error:
            raise RuntimeError(T("ошибка USB: {e}", e=tr.usb_error))
        tr.show_live()


def send_ack(stick, tr, payload, what):
    """Отправить станку страницу с подтверждением доставки."""
    for attempt in range(1, TX_RETRIES + 1):
        tr.tx_event = None
        stick.send(MSG_ACKNOWLEDGED, [CH] + list(payload))
        end = time.monotonic() + 2.0
        while time.monotonic() < end and tr.tx_event is None:
            check_stop()
            msg = stick.next_msg(0.1)
            if msg:
                tr.handle(msg)
            if tr.usb_error:
                raise RuntimeError(T("ошибка USB: {e}", e=tr.usb_error))
        if tr.tx_event == EV_TX_COMPLETED:
            return True
        say(T("  попытка {n}: станок не подтвердил {what}, повторяю", n=attempt, what=what))
    return False


def send_calibration_request(stick, tr):
    payload = [PAGE_CALIBRATION, CAL_SPINDOWN, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
    return send_ack(stick, tr, payload, T("запрос калибровки"))


def user_config_page(user_kg=DEFAULT_USER_KG, bike_kg=DEFAULT_BIKE_KG, wheel_m=DEFAULT_WHEEL_M):
    uw = int(round(user_kg * 100))
    bw = int(round(bike_kg / 0.05)) & 0x0FFF
    wheel_offset_mm = 0
    return bytes([PAGE_USER_CONFIG, uw & 0xFF, uw >> 8, 0xFF,
                  (wheel_offset_mm & 0x0F) | ((bw & 0x0F) << 4), bw >> 4,
                  int(round(wheel_m * 100)), 0x00])          # передаточное число: 0 = не задано


def send_user_config(stick, tr):
    say(T("Станок просит настройки пользователя, отправляю стандартные ({user:.0f} кг, велосипед {bike:.0f} кг, "
          "колесо 700c). На калибровку не влияют.", user=DEFAULT_USER_KG, bike=DEFAULT_BIKE_KG))
    if not send_ack(stick, tr, user_config_page(), T("настройки")):
        say(T("  Станок не подтвердил настройки, продолжаю без них."))
        return
    pump(stick, tr, 1.5)
    log_only(T("  после настроек флаги станка: {flags:#x}", flags=tr.calib_flags))


def ant_warmup(stick, tr):
    wu = Warmup()
    while not wu.tick(tr.speed, tr.power):
        pump(stick, tr, 0.25)
        if tr.search_timeout:
            raise RuntimeError(T("связь со станком по ANT+ потеряна во время прогрева"))


def main_ant(info_only, dev, warmup=True):
    stick = AntStick(dev)
    stick.open()
    try:
        stick.open_fe_channel()
        say(T("Ищу станок по ANT+ (до {sec:.0f} сек). Покрути педали, чтобы он проснулся.", sec=SEARCH_TIMEOUT))
        ui.phase("search")
        tr = AntTrainer()
        deadline = time.monotonic() + SEARCH_TIMEOUT + 5
        while not tr.got_data:
            check_stop()
            msg = stick.next_msg(0.2)
            if msg:
                tr.handle(msg)
            if tr.usb_error:
                raise RuntimeError(T("ошибка USB: {e}", e=tr.usb_error))
            if tr.search_timeout or time.monotonic() > deadline:
                say(T("Станок по ANT+ не найден. Он включён? Zwift закрыт? Поднеси донгл ближе к станку."))
                return
        stick.send(MSG_REQUEST, [CH, MSG_CHANNEL_ID])
        pump(stick, tr, 1.5)
        say(T("Станок найден, ANT+ ID {id}.", id=tr.device_number if tr.device_number is not None else "?"))
        ui.phase("prepare", T("Станок найден"))
        if tr.calib_flags:
            need = []
            if tr.calib_flags & 0x1:
                need.append(T("калибровку мощности"))
            if tr.calib_flags & 0x2:
                need.append(T("калибровку сопротивления"))
            if tr.calib_flags & 0x4:
                need.append(T("настройку пользователя"))
            if need:
                say(T("Станок сам просит: {what}.", what=", ".join(need)))

        if info_only:
            say(T("Показываю данные {sec:.0f} сек, покрути педали:", sec=INFO_SECONDS))
            ui.phase("info")
            tr.live = True
            pump(stick, tr, INFO_SECONDS)
            tr.live = False
            say(T("Максимум за это время: {v:.1f} км/ч, {cad} об/мин, {w} Вт.",
                  v=tr.max_speed, cad=tr.max_cadence, w=tr.max_power))
            if tr.max_speed == 0 and tr.max_cadence == 0:
                say(T("Скорость и каденс были нулевые: педали не крутились? Для проверки данных покрути и запусти снова."))
            elif tr.max_cadence == 0:
                say(T("Каденс станок не передаёт, это нормально: для калибровки он не нужен."))
            log_only(T("  страницы от станка: {pages}",
                       pages=", ".join(f"0x{k:02x}×{v}" for k, v in sorted(tr.page_counts.items()))))
            say(T("Связь со станком по ANT+ есть, калибровку можно запускать."))
            ui.finish(True, T("по ANT+, максимум {v:.1f} км/ч и {w} Вт", v=tr.max_speed, w=tr.max_power),
                      title=T("СВЯЗЬ ЕСТЬ"))
            return

        if tr.calib_flags and tr.calib_flags & 0x4:
            send_user_config(stick, tr)

        if warmup:
            ant_warmup(stick, tr)
        else:
            ui.wait_ready(T("Сядь на станок, начни спокойно крутить"))
        while True:
            msg = stick.next_msg(0.01)
            if msg is None:
                break
            tr.handle(msg)

        tr.state = "requested"
        if not send_calibration_request(stick, tr):
            say(T("Станок не подтвердил запрос калибровки. Поднеси донгл ближе и повтори."))
            return
        if tr.state == "requested":
            say(T("Запрос калибровки принят станком, жду начала..."))
            ui.phase("prepare", T("Жду, когда станок начнёт калибровку…"))
        tr.live = True
        start = time.monotonic()
        last_log = 0.0
        while tr.result is None:
            check_stop()
            msg = stick.next_msg(0.1)
            if msg:
                tr.handle(msg)
            if tr.usb_error:
                raise RuntimeError(T("ошибка USB: {e}", e=tr.usb_error))
            tr.show_live()
            now = time.monotonic()
            if now - last_log >= 1.0:
                last_log = now
                log_only(T("  +{t:5.1f} с: {v} км/ч, {cad} об/мин, {w} Вт, состояние {state}, скорость {sc}, "
                           "температура {tc}", t=now - start, v=fmt(tr.speed, 1), cad=fmt(tr.cadence),
                           w=fmt(tr.power), state=tr.state, sc=tr.speed_cond, tc=tr.temp_cond))
            if tr.search_timeout:
                say(T("Связь со станком потеряна, калибровка прервана. Поднеси донгл ближе и повтори."))
                return
            elapsed = time.monotonic() - start
            if tr.state == "requested" and elapsed > START_TIMEOUT:
                say(T("Станок принял запрос, но калибровку не начал. Пришли hammer log.txt."))
                return
            if elapsed > CAL_TIMEOUT:
                say(T("Станок не сообщил результат за {sec:.0f} сек. Калибровка не завершена, повтори.", sec=CAL_TIMEOUT))
                return
        tr.live = False

        if tr.result[0] == "ok":
            _, sd, temp = tr.result
            msg = T("КАЛИБРОВКА УСПЕШНА.")
            if sd is not None:
                msg += " " + T("Время выбега {v:.1f} с.", v=sd / 1000)
            if temp is not None:
                msg += " " + T("Температура {v:.1f} °C.", v=temp)
            say(msg)
            if tr.coast_at is not None:
                log_only(T("  от команды «перестань крутить» до ответа: {v:.1f} с", v=time.monotonic() - tr.coast_at))
            say(T("Станок меряет замедление на отрезке скоростей, поэтому маховик ещё может крутиться, это нормально."))
            pump(stick, tr, 3.0)
            log_only(T("  флаги станка после калибровки: {flags}", flags=tr.calib_flags))
            if tr.calib_flags is not None and tr.calib_flags & 0x2:
                say(T("Станок всё ещё выставляет флаг «нужна калибровка». Повтори калибровку."))
                ui.finish(False, T("станок всё ещё просит калибровку, повтори"))
                return
            if tr.calib_flags is not None:
                say(T("Станок снял флаг «нужна калибровка»: результат принят и записан в станок."))
            say(T("Можно запускать Zwift."))
            detail = T("время выбега") + (T(", температура {v:.1f} °C", v=temp) if temp is not None else "")
            ui.finish(True, detail + ". " + T("Можно запускать Zwift."),
                      value=(f"{sd / 1000:.1f}", T("с")) if sd is not None else None)
        else:
            say(T("Станок сообщил, что калибровка не удалась (флаги 0x{flags:02x}).", flags=tr.result[1]))
            say(T("Повтори: держи целевую скорость, а после «ПЕРЕСТАНЬ КРУТИТЬ» не трогай педали до конца выбега."))
            ui.finish(False, T("держи целевую скорость, а после «перестань крутить» не трогай педали"))
    finally:
        stick.close()


# ============================== Bluetooth ==============================

def uuid16(short):
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


CPS_SERVICE = uuid16(0x1818)
CPS_MEASUREMENT = uuid16(0x2A63)
FTMS_SERVICE = uuid16(0x1826)
CO_SERVICE = "c0f4013a-a837-4165-bab9-654ef70747c6"
CO_CONTROL = "ca31a533-a858-4dc7-a650-fdeb6dad4c14"
DEVICE_INFO = {
    uuid16(0x2A29): "производитель",
    uuid16(0x2A24): "модель",
    uuid16(0x2A25): "серийный номер",
    uuid16(0x2A27): "железо",
    uuid16(0x2A26): "прошивка",
}
TRAINER_NAMES = ("hammer", "saris", "cycleops", "magnus")

# Фирменный протокол CycleOps (из приложения Saris 4.0.55).
# Запрос: 10 байт [тип, 0x10, данные...]. Ответ приходит индикацией на ту же характеристику:
# [0x01, номер, тип запроса, 0x10, данные...]; первый байт не 0x01 означает отказ.
CO_SET_MODE = 0x00        # [00 10 режим параметр_lo параметр_hi ...]
CO_GET_MODE = 0x01        # ответ: [4] режим, [9] этап калибровки
CO_CAL_STATUS = 0x09      # станок сам присылает этап калибровки, [9] как у CO_GET_MODE
CO_GET_ROLLDOWN = 0x0A    # ответ: [4..5] время выбега в мс, если [8] == 0

MODE_HEADLESS = 0
MODE_ERG = 1
MODE_CAL_ENTER = 4
MODE_CAL_TEST = 5
MODE_NAMES = {MODE_HEADLESS: "свободный", MODE_ERG: "ERG", MODE_CAL_ENTER: "калибровка", MODE_CAL_TEST: "тест выбега"}

CAL_INIT = 3              # станок готовит тест, крутить дальше
CAL_COAST = 4             # перестать крутить, идёт выбег
CAL_OK = 5
CAL_FAIL = 6


def co_frame(kind, data=()):
    b = bytearray(10)
    b[0] = kind
    b[1] = 0x10
    b[2:2 + len(data)] = bytes(data)
    return bytes(b)


# ---------------- Bluetooth: станок ----------------

class BleTrainer:
    def __init__(self):
        self.got_cps = False
        self.has_wheel = False
        self.speed = None
        self.power = None
        self.speed_at = 0.0
        self.last_wheel = None
        self.mode = None
        self.state = "idle"        # idle / waiting / test / init / coast
        self.result = None         # ("ok", сырое значение) / ("fail",) / ("done",)
        self.disconnected = False
        self.live = False
        self.last_print = 0.0
        self.last_co = None
        self.max_speed = 0.0
        self.max_power = 0

    def on_disconnect(self, _client):
        self.disconnected = True

    def on_cps(self, _char, data):
        d = bytes(data)
        if len(d) < 4:
            return
        if not self.got_cps:
            log_only(T("  первые данные Cycling Power: {hex}", hex=d.hex(" ")))
            self.got_cps = True
        flags = u16(d[0], d[1])
        self.power = int.from_bytes(d[2:4], "little", signed=True)
        self.max_power = max(self.max_power, self.power)
        off = 4
        if flags & 0x01:          # баланс педалей
            off += 1
        if flags & 0x04:          # накопленный момент
            off += 2
        if not flags & 0x10 or len(d) < off + 6:
            return
        self.has_wheel = True
        revs = int.from_bytes(d[off:off + 4], "little")
        event = u16(d[off + 4], d[off + 5])     # время в 1/2048 сек
        now = time.monotonic()
        if self.last_wheel is not None:
            dr = (revs - self.last_wheel[0]) & 0xFFFFFFFF
            dt = ((event - self.last_wheel[1]) & 0xFFFF) / 2048
            if dt > 0 and dr < 1000:
                v = dr * WHEEL_M / dt * 3.6
                if v < 95:                      # приложение Saris тоже отбрасывает выбросы от 95 км/ч
                    self.speed = v
                    self.speed_at = now
                    self.max_speed = max(self.max_speed, v)
            elif dt == 0 and now - self.speed_at > 2.0:
                self.speed = 0.0
        self.last_wheel = (revs, event)

    def handle_co(self, v):
        if v != self.last_co:
            self.last_co = v
            log_only(f"  [{self.state}] <- {v.hex(' ')}")
        if len(v) != 10 or v[0] != 0x01 or v[3] != 0x10:
            return
        if v[2] == CO_GET_MODE:
            self.mode = v[4]
            if v[4] == MODE_CAL_TEST:
                self._cal_status(v)
            elif self.state != "waiting" and self.state != "idle" and v[1] == 0 and not any(v[4:]):
                # станок вернулся в свободный режим: приложение Saris считает это окончанием теста
                self.result = self.result or ("done",)
        elif v[2] == CO_CAL_STATUS:
            self._cal_status(v)

    def _cal_status(self, v):
        code = v[9]
        if code == CAL_INIT and self.state in ("waiting", "test"):
            self.state = "init"
            say(T(">>> Станок готовит тест: крути дальше, держи скорость."))
        elif code == CAL_COAST and self.state != "coast":
            self.state = "coast"
            say(T(">>> ПЕРЕСТАНЬ КРУТИТЬ: отпусти педали и жди, станок выбегает."))
            ui.phase("coast")
        elif code == CAL_OK and self.result is None:
            self.result = ("ok", u16(v[4], v[5]))
        elif code == CAL_FAIL and self.result is None:
            self.result = ("fail",)

    def show_live(self):
        if not self.live:
            return
        now = time.monotonic()
        if now - self.last_print < LIVE_PERIOD:
            return
        self.last_print = now
        target = phase = None
        if self.state == "waiting":
            target = CAL_SPEED
            phase = "accelerate" if self.speed is None or self.speed < CAL_SPEED else "hold"
        elif self.state in ("test", "init"):
            phase = "hold"
        elif self.state == "coast":
            phase = "coast"
        ui.live(self.speed, target, self.power, phase)


class CycleOps:
    """Запрос-ответ по фирменной характеристике CycleOps."""

    def __init__(self, client, char, tr):
        self.client = client
        self.char = char
        self.tr = tr
        self.lock = asyncio.Lock()
        self.waiting = None        # (тип запроса, future)
        self.fallback = None

    def on_indication(self, _char, data):
        v = bytes(data)
        self.tr.handle_co(v)
        if self.waiting is None or self.waiting[1].done():
            return
        kind, fut = self.waiting
        if len(v) >= 4 and v[0] == 0x01:
            if v[2] == kind:
                fut.set_result(v)
            elif v[2] != CO_CAL_STATUS and self.fallback is None:
                self.fallback = v
        elif len(v) >= 4:
            fut.set_result(v)       # отказ

    async def request(self, kind, data=(), what=None, quick=False):
        """quick: одна короткая попытка без повторов (для опроса), без ответа вернёт None."""
        what = what or T("команду")
        msg = co_frame(kind, data)
        async with self.lock:
            for attempt, delay in enumerate(RETRY_DELAYS[:1] if quick else RETRY_DELAYS, 1):
                check_stop()
                fut = asyncio.get_running_loop().create_future()
                self.waiting = (kind, fut)
                self.fallback = None
                log_only(f"  -> {what}: {msg.hex(' ')}")
                try:
                    await self.client.write_gatt_char(self.char, msg, response=True)
                    v = await asyncio.wait_for(fut, POLL_TIMEOUT if quick else CO_RESPONSE_TIMEOUT)
                except asyncio.TimeoutError:
                    # приложение Saris принимает любой ответ, лишь бы начинался с 0x01
                    v = self.fallback
                    if v is not None:
                        log_only(T("  ответ на {what} пришёл с другим типом, принимаю", what=what))
                finally:
                    self.waiting = None
                if self.tr.disconnected:
                    raise RuntimeError(T("связь со станком по Bluetooth пропала"))
                if v is None:
                    if quick:
                        log_only(T("  нет ответа на {what}", what=what))
                        return None
                    say(T("  попытка {n}: станок не ответил на {what}, повторяю", n=attempt, what=what))
                    await asyncio.sleep(delay)
                    continue
                if v[0] != 0x01:
                    raise RuntimeError(T("станок отклонил {what} (ответ {hex})", what=what, hex=v.hex(" ")))
                return v
        raise RuntimeError(T("станок не ответил на {what}", what=what))

    async def set_mode(self, mode, value=0, what=None):
        await self.request(CO_SET_MODE, [mode, value & 0xFF, value >> 8],
                           what or T("режим {name}", name=T(MODE_NAMES[mode])))

    async def get_mode(self):
        v = await self.request(CO_GET_MODE, what=T("запрос режима"))
        return v[4]

    async def get_rolldown(self):
        """Время выбега, записанное в станке, сек; None, если его нет."""
        v = await self.request(CO_GET_ROLLDOWN, what=T("запрос времени выбега"))
        p = v[4:]
        if len(p) >= 5 and p[4] == 0:
            ms = u16(p[0], p[1])
            return ms / 1000 if ms else None
        log_only(T("  время выбега в незнакомом формате: {hex}", hex=p.hex(" ")))
        return None


# ---------------- Bluetooth: подключение и калибровка ----------------

async def find_ble_trainer():
    found = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_adv(dev, adv):
        name = adv.local_name or dev.name or ""
        uuids = [u.lower() for u in adv.service_uuids]
        if CO_SERVICE in uuids or any(k in name.lower() for k in TRAINER_NAMES):
            score = 2
        elif CPS_SERVICE in uuids or FTMS_SERVICE in uuids:
            score = 1
        else:
            return
        if dev.address not in found:
            log_only(T("  найдено: {name} {addr}, сигнал {rssi} дБм, сервисы {uuids}", name=name or "?",
                       addr=dev.address, rssi=adv.rssi, uuids=", ".join(uuids) or "-"))
            if score == 1 and not any(f[0] == 1 for f in found.values()):
                loop.call_later(3.0, stop.set)  # чужой датчик мощности тоже подойдёт, но подождём станок
        found[dev.address] = (score, adv.rssi or -999, dev, name)
        if score == 2:
            stop.set()

    async with BleakScanner(on_adv):
        end = time.monotonic() + SEARCH_TIMEOUT
        while not stop.is_set() and time.monotonic() < end:
            check_stop()
            await asyncio.sleep(0.2)
    if not found:
        return None, None
    _, _, dev, name = max(found.values(), key=lambda f: (f[0], f[1]))
    return dev, name


async def ble_connect(dev, tr):
    for attempt in (1, 2):
        client = BleakClient(dev, disconnected_callback=tr.on_disconnect, timeout=CONNECT_TIMEOUT)
        try:
            await client.connect()
            return client
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            log_only(T("  подключение, попытка {n}: {err}: {e}", n=attempt, err=type(e).__name__, e=e))
            if attempt == 1:
                say(T("  Не подключилось, пробую ещё раз..."))
                await asyncio.sleep(2.0)
    raise RuntimeError(T("не удалось подключиться к станку по Bluetooth. Закрой Zwift и приложения на телефоне."))


async def describe_ble(client):
    """Пишет в лог все сервисы станка и показывает модель и прошивку."""
    for s in client.services:
        log_only(T("  сервис {uuid}", uuid=s.uuid))
        for c in s.characteristics:
            log_only(f"    {c.uuid} [{', '.join(c.properties)}]")
    info = []
    for uuid, label in DEVICE_INFO.items():
        c = client.services.get_characteristic(uuid)
        if c is None:
            continue
        try:
            value = (await client.read_gatt_char(c)).decode("utf-8", "replace").strip("\x00 ")
        except (BleakError, OSError) as e:
            log_only(T("  не прочиталось {what}: {e}", what=T(label), e=e))
            continue
        if value:
            info.append(f"{T(label)} {value}")
    if info:
        say(T("Станок: {info}.", info=", ".join(info)))


async def ble_pump(tr, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await asyncio.sleep(0.1)
        check_stop()
        if tr.disconnected:
            raise RuntimeError(T("связь со станком по Bluetooth пропала"))
        tr.show_live()


async def ble_warmup(tr):
    wu = Warmup()
    # скорость, которая давно не обновлялась, считаем нулём: педали стоят
    while not wu.tick(tr.speed if time.monotonic() - tr.speed_at < 3.0 else 0.0, tr.power):
        await ble_pump(tr, 0.25)


async def poll_mode(link):
    # ответ разбирает handle_co; станок иногда присылает вместо него своё сообщение, тогда ждём недолго
    while True:
        await link.request(CO_GET_MODE, what=T("запрос режима"), quick=True)
        await asyncio.sleep(POLL_INTERVAL)


async def ble_calibrate(link, tr, before):
    say(T("Ставлю нагрузку {w} Вт и включаю режим калибровки (так делает приложение Saris)...", w=CAL_WATTS))
    ui.phase("prepare", T("Ставлю нагрузку {w} Вт…", w=CAL_WATTS))
    await link.set_mode(MODE_ERG, CAL_WATTS, T("нагрузку {w} Вт", w=CAL_WATTS))
    await ble_pump(tr, 5.0)
    await link.set_mode(MODE_CAL_ENTER)
    await ble_pump(tr, 1.0)
    say(T("Крути спокойно, пока не разгоняйся..."))
    ui.phase("prepare", T("Крути спокойно, пока не разгоняйся"))
    tr.live = True
    await ble_pump(tr, 3.0)
    tr.state = "waiting"
    say(T("Разгоняйся до {v:.0f} км/ч. Скрипт сам запустит тест и скажет, когда перестать крутить.", v=CAL_SPEED))

    poller = asyncio.create_task(poll_mode(link))
    starter = None
    start = time.monotonic()
    last_log = 0.0
    try:
        while tr.result is None:
            await asyncio.sleep(0.1)
            check_stop()
            for task in (poller, starter):
                if task is not None and task.done() and not task.cancelled():
                    task.result()   # пробросить ошибку опроса или запуска теста
            if tr.disconnected:
                raise RuntimeError(T("связь со станком по Bluetooth пропала, калибровка прервана"))
            tr.show_live()
            now = time.monotonic()
            if now - last_log >= 1.0:
                last_log = now
                log_only(T("  +{t:5.1f} с: {v} км/ч, {w} Вт, режим {mode}, состояние {state}", t=now - start,
                           v=fmt(tr.speed, 1), w=fmt(tr.power), mode=tr.mode, state=tr.state))
            if tr.state == "waiting" and tr.speed is not None and tr.speed >= CAL_SPEED:
                tr.state = "test"
                say(T(">>> {v:.0f} км/ч есть, запускаю тест. Крути дальше, пока не появится «ПЕРЕСТАНЬ КРУТИТЬ».",
                      v=CAL_SPEED))
                # тест уходит сразу, не в очереди за опросом, а окно продолжает показывать скорость
                poller.cancel()
                starter = asyncio.create_task(link.set_mode(MODE_CAL_TEST, what=T("запуск теста выбега")))
            if starter is not None and starter.done() and poller.done():
                poller = asyncio.create_task(poll_mode(link))
            if now - start > CAL_TIMEOUT:
                say(T("Станок не сообщил результат за {sec:.0f} сек. Калибровка не завершена, повтори.", sec=CAL_TIMEOUT))
                return
    finally:
        for task in (poller, starter):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass        # ошибку уже пробросили выше
    tr.live = False

    if tr.result[0] == "fail":
        say(T("Станок сообщил, что калибровка не удалась."))
        say(T("Повтори: разгонись до {v:.0f} км/ч, а после «ПЕРЕСТАНЬ КРУТИТЬ» не трогай педали до конца выбега.",
              v=CAL_SPEED))
        ui.finish(False, T("разгонись до {v:.0f} км/ч, а после «перестань крутить» не трогай педали", v=CAL_SPEED))
        return
    log_only(T("  результат от станка: {r}", r=tr.result))
    after = await link.get_rolldown()
    if tr.state != "coast" and after == before:
        say(T("Станок вышел из теста, не дойдя до выбега, и время выбега не изменилось. Повтори калибровку."))
        ui.finish(False, T("станок вышел из теста раньше выбега, повтори"))
        return
    msg = T("КАЛИБРОВКА УСПЕШНА.")
    if after is not None:
        msg += " " + (T("Время выбега {v:.2f} с (было {old:.2f} с).", v=after, old=before) if before is not None
                      else T("Время выбега {v:.2f} с.", v=after))
    say(msg)
    say(T("Можно запускать Zwift."))
    detail = T("время выбега") + (T(", было {old:.2f} с", old=before) if before is not None else "")
    ui.finish(True, detail + ". " + T("Можно запускать Zwift."),
              value=(f"{after:.2f}", T("с")) if after is not None else None)


async def main_ble(info_only, warmup=True):
    say(T("Ищу станок по Bluetooth (до {sec:.0f} сек). Покрути педали, чтобы он проснулся.", sec=SEARCH_TIMEOUT))
    ui.phase("search")
    dev, name = await find_ble_trainer()
    if dev is None:
        say(T("Станок по Bluetooth не найден. Он включён? Zwift и приложения на телефоне закрыты?\n"
              "Если Bluetooth станка занят телефоном, выключи на телефоне Bluetooth."))
        return
    say(T("Нашёл {name}, подключаюсь...", name=name or dev.address))
    ui.phase("prepare", T("Подключаюсь…"))
    tr = BleTrainer()
    client = await ble_connect(dev, tr)
    try:
        say(T("Подключено."))
        ui.phase("prepare", T("Станок найден"))
        await describe_ble(client)
        cps = client.services.get_characteristic(CPS_MEASUREMENT)
        co = client.services.get_characteristic(CO_CONTROL)
        if co is None:
            say(T("У этого устройства нет фирменной характеристики CycleOps: это не Hammer, или прошивка другая."))
            if client.services.get_service(FTMS_SERVICE) is not None:
                say(T("Зато есть стандартный FTMS: такой станок калибруй по ANT+ этим скриптом или из Zwift по Bluetooth."))
            return
        link = CycleOps(client, co, tr)
        if cps is not None:
            await client.start_notify(cps, tr.on_cps)
        await client.start_notify(co, link.on_indication)

        before = await link.get_rolldown()
        if before is not None:
            say(T("Сейчас в станке записано время выбега {v:.2f} с.", v=before))
        else:
            say(T("Время выбега в станке пока не записано."))
        mode = await link.get_mode()
        log_only(T("  режим станка: {name}", name=T(MODE_NAMES[mode]) if mode in MODE_NAMES else mode))

        if info_only:
            say(T("Показываю данные {sec:.0f} сек, покрути педали:", sec=INFO_SECONDS))
            ui.phase("info")
            tr.live = True
            await ble_pump(tr, INFO_SECONDS)
            tr.live = False
            say(T("Максимум за это время: {v:.1f} км/ч, {w} Вт.", v=tr.max_speed, w=tr.max_power))
            if cps is None or not tr.got_cps:
                say(T("Станок не прислал данные мощности. Без них скрипт не увидит скорость и не запустит тест."))
            elif not tr.has_wheel:
                say(T("Станок не передаёт скорость по Bluetooth: калибровка по этому скрипту не запустится. "
                      "Пришли hammer log.txt."))
            elif tr.max_speed == 0:
                say(T("Скорость была нулевая: педали не крутились? Для проверки данных покрути и запусти снова."))
            say(T("Связь со станком по Bluetooth есть, калибровку можно запускать."))
            recorded = T(", в станке записан выбег {v:.2f} с", v=before) if before is not None else ""
            ui.finish(True, T("по Bluetooth{recorded}, максимум {v:.1f} км/ч и {w} Вт", recorded=recorded,
                              v=tr.max_speed, w=tr.max_power), title=T("СВЯЗЬ ЕСТЬ"))
            return

        if cps is None:
            say(T("У станка нет Cycling Power: скрипт не увидит скорость. Пришли hammer log.txt."))
            return
        if warmup:
            await ble_warmup(tr)
        else:
            await asyncio.to_thread(ui.wait_ready, T("Сядь на станок"))
        await ble_calibrate(link, tr, before)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ================================ запуск ================================

def lib_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "?"


def choose_link(args):
    """Как подключаться: ("ant", донгл), ("ble", None) или (None, None), если нечем."""
    if "--ble" in args:
        if bleak is None:
            say(T("Нет библиотеки bleak для Bluetooth. Установи командой: python -m pip install bleak"))
            return None, None
        return "ble", None
    dev = find_stick() if usb is not None else None
    if "--ant" in args or bleak is None:
        if usb is None:
            say(T("Нет библиотеки pyusb для ANT+. Установи командой: python -m pip install pyusb libusb-package"))
            return None, None
        if dev is None:
            say(T("ANT+ донгл не найден. Вставь его в USB (лучше через удлинитель поближе к станку)."))
            return None, None
        return "ant", dev
    if dev is None:
        say(T("ANT+ донгл не найден, подключаюсь по Bluetooth."))
        return "ble", None
    try:
        answer = input(T("Как подключаться к станку? 1 = ANT+ донгл, 2 = Bluetooth [Enter = 1]: ")).strip()
    except EOFError:
        answer = ""
    how = "ble" if answer == "2" else "ant"
    log_only(T("  выбрано: {how}", how="Bluetooth" if how == "ble" else "ANT+"))
    return how, (dev if how == "ant" else None)


def session(info_only, args):
    """Одна проверка связи или калибровка. Ошибки пишет на экран и в лог, наружу не бросает."""
    try:
        say(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {T('проверка связи') if info_only else T('калибровка')} =====")
        no = T("нет")
        say(f"Python {sys.version.split()[0]} ({'64' if _IS_64 else '32'}-bit), "
            f"pyusb {lib_version('pyusb') if usb else no}, bleak {lib_version('bleak') if bleak else no}")
        how, dev = choose_link(args)
        warmup = "--no-warmup" not in args
        if how == "ant":
            main_ant(info_only, dev, warmup)
        elif how == "ble":
            asyncio.run(main_ble(info_only, warmup))
    except (KeyboardInterrupt, Stopped):
        say(T("\nПрервано."))
        ui.finish(None)
    except RuntimeError as e:
        say(T("ОШИБКА: {e}", e=e))
        ui.finish(False, str(e))
    except Exception as e:
        say(T("ОШИБКА: {e}", e=f"{type(e).__name__}: {e}"))
        say(traceback.format_exc().rstrip())
        say(T("Проверь, что Zwift закрыт, донгл вставлен, а Bluetooth включён."))
        ui.finish(False, f"{type(e).__name__}: {e}")


# ================================ окно ================================

# фаза: (цвет фона, крупная надпись)
PHASES = {
    "idle": ("#455a64", "Выбери связь и нажми «Калибровать»"),
    "search": ("#1565c0", "Ищу станок… Покрути педали, чтобы он проснулся"),
    "prepare": ("#1565c0", ""),
    "ready": ("#1565c0", ""),
    "info": ("#1565c0", "Покрути педали: проверяю данные"),
    "prewarm": ("#00695c", "ПРОГРЕВ: КРУТИ СПОКОЙНО"),
    "prewarm_pause": ("#455a64", "ПРОГРЕВ НА ПАУЗЕ: КРУТИ ПЕДАЛИ"),
    "accelerate": ("#e65100", "РАЗГОНЯЙСЯ"),
    "hold": ("#2e7d32", "ДЕРЖИ СКОРОСТЬ"),
    "warmup": ("#2e7d32", "ДЕРЖИ, СТАНОК ПРОГРЕВАЕТСЯ"),
    "coast": ("#c62828", "ПЕРЕСТАНЬ КРУТИТЬ"),
    "ok": ("#2e7d32", "ГОТОВО"),
    "fail": ("#7b1f1f", "НЕ ПОЛУЧИЛОСЬ"),
    "stopped": ("#455a64", "Остановлено"),
}
LIVE_PHASES = ("accelerate", "hold", "warmup", "coast")
GO_PHASES = ("ready", "prewarm", "prewarm_pause")    # фазы, где видна большая кнопка; в остальных она прячется


class GuiUI:
    """Окно: крупная скорость и цветная команда, которые видно с велосипеда.

    Связь со станком идёт в отдельном потоке (session), а окно получает от него события
    через очередь: tkinter можно трогать только из главного потока."""

    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # чёткий текст при масштабе экрана 125–150 %
        except Exception:
            pass
        self.events = queue.Queue()
        self.go = threading.Event()
        self.worker = None
        self.finished = False
        self.closing = False
        self.last_msg = ""

        root = self.root = tk.Tk()
        root.geometry("860x780")
        root.minsize(560, 520)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self.f_big = tkfont.Font(family="Segoe UI", size=-160, weight="bold")
        self.f_cmd = tkfont.Font(family="Segoe UI", size=-40, weight="bold")
        self.f_unit = tkfont.Font(family="Segoe UI", size=-32)
        self.f_detail = tkfont.Font(family="Segoe UI", size=-24)

        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        self.l_link = ttk.Label(top)
        self.l_link.pack(side="left", padx=(0, 6))
        self.link = tk.StringVar(value="ant")
        self.rb_ant = ttk.Radiobutton(top, value="ant", variable=self.link)
        self.rb_ble = ttk.Radiobutton(top, text="Bluetooth", value="ble", variable=self.link)
        self.rb_ant.pack(side="left", padx=(0, 10))
        self.rb_ble.pack(side="left")
        self.warm = tk.BooleanVar(value=True)
        self.cb_warm = ttk.Checkbutton(top, variable=self.warm)
        self.cb_warm.pack(side="left", padx=(16, 0))
        self.b_stop = ttk.Button(top, command=self._stop)
        self.b_stop.pack(side="right")
        self.b_cal = ttk.Button(top, command=lambda: self._start(False))
        self.b_cal.pack(side="right", padx=(0, 6), ipady=3)
        self.b_check = ttk.Button(top, command=lambda: self._start(True))
        self.b_check.pack(side="right", padx=(0, 6), ipady=3)

        self.panel = tk.Frame(root)
        self.panel.pack(fill="both", expand=True, padx=12)
        self.l_cmd = tk.Label(self.panel, font=self.f_cmd, fg="white", justify="center")
        self.l_cmd.pack(pady=(24, 0))
        row = tk.Frame(self.panel)
        row.pack(expand=True)
        self.l_big = tk.Label(row, font=self.f_big, fg="white")
        self.l_big.pack(side="left")
        self.l_unit = tk.Label(row, font=self.f_unit, fg="white")
        self.l_unit.pack(side="left", anchor="s", pady=(0, 30))
        self.l_detail = tk.Label(self.panel, font=self.f_detail, fg="white", justify="center")
        self.l_detail.pack(pady=(0, 16))
        self.b_go = tk.Button(self.panel, font=self.f_cmd, command=self._go,
                              relief="flat", bg="white", padx=48, pady=6, cursor="hand2")
        self.tinted = (self.panel, self.l_cmd, row, self.l_big, self.l_unit, self.l_detail)
        self.panel.bind("<Configure>", self._resize)
        root.bind("<space>", self._key_go)
        root.bind("<Return>", self._key_go)

        bottom = ttk.Frame(root, padding=(12, 8, 12, 12))
        bottom.pack(fill="x")
        head = ttk.Frame(bottom)
        head.pack(fill="x")
        self.l_journal = ttk.Label(head)
        self.l_journal.pack(side="left")
        self.b_log = ttk.Button(head, command=self._open_log)
        self.b_log.pack(side="right")
        self.cb_lang = ttk.Combobox(head, values=list(LANG_NAMES.values()), state="readonly", width=9)
        self.cb_lang.set(LANG_NAMES[LANG])
        self.cb_lang.bind("<<ComboboxSelected>>", self._on_lang)
        self.cb_lang.pack(side="right", padx=(0, 8))
        box = ttk.Frame(bottom)
        box.pack(fill="x", pady=(4, 0))
        self.log = tk.Text(box, height=7, font=("Consolas", 9), wrap="word", state="disabled",
                           relief="flat", bg="#f3f3f3", padx=6, pady=4)
        sb = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="x", expand=True)

        self._apply_lang()
        self._set_phase("idle")
        self._controls(running=True)    # пока ищем донгл, кнопки неактивны
        threading.Thread(target=self._detect, daemon=True).start()
        root.after(50, self._poll)

    # ---- вызывается из потока связи ----

    def message(self, msg):
        self.events.put(("message", msg))

    def live(self, speed, target, power, phase, cadence=None, show_cadence=False):
        self.events.put(("live", speed, target, power, phase))

    def phase(self, key, text=None):
        if key == "coast":
            beep("coast")
        self.events.put(("phase", key, text))

    def finish(self, ok, detail="", value=None, title=None):
        if ok is not None:
            beep("ok" if ok else "fail")
        self.events.put(("finish", ok, detail, value, title))

    def wait_ready(self, prompt):
        self.go.clear()
        self.events.put(("ready", prompt))
        while not self.go.wait(0.1):
            check_stop()

    def begin_warmup(self):
        self.go.clear()         # кнопка «Калибровать сейчас» — та же, что «Поехали»
        self.message(T("Пробел или Enter — калибровать сейчас, не дожидаясь конца прогрева."))

    def warmup(self, left, speed, power, riding):
        self.events.put(("warmup", left, speed, power, riding))

    def skip_pressed(self):
        return self.go.is_set()

    # ---- главный поток ----

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                getattr(self, "_on_" + ev[0])(*ev[1:])
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _detect(self):
        dev = None
        if usb is not None:
            try:
                dev = find_stick(quiet=True)
            except Exception as e:
                log_only(T("  поиск донгла: {e}", e=e))
        self.events.put(("detected", dev is not None))

    def _on_detected(self, has_dongle):
        self.link.set("ant" if has_dongle or bleak is None else "ble")
        if has_dongle:
            self._note(T("ANT+ донгл найден."))
        elif bleak is not None:
            self._note(T("ANT+ донгл не найден, выбран Bluetooth."))
        if usb is None:
            self._note(T("Нет библиотеки pyusb: ANT+ недоступен."))
        if bleak is None:
            self._note(T("Нет библиотеки bleak: Bluetooth недоступен."))
        self._controls(running=False)

    def _on_lang(self, _event=None):
        global LANG
        LANG = next(k for k, v in LANG_NAMES.items() if v == self.cb_lang.get())
        save_settings(lang=LANG)
        self._apply_lang()
        self._set_phase("idle")
        self._show_value(None, "")

    def _apply_lang(self):
        self.root.title(T("Калибровка Hammer"))
        self.l_link.configure(text=T("Связь:"))
        self.rb_ant.configure(text=T("ANT+ донгл"))
        self.cb_warm.configure(text=T("Прогрев {min:.0f} мин", min=WARMUP_SECONDS / 60))
        self.b_stop.configure(text=T("Стоп"))
        self.b_cal.configure(text=T("Калибровать"))
        self.b_check.configure(text=T("Проверить связь"))
        self.l_journal.configure(text=T("Журнал"))
        self.b_log.configure(text=T("Открыть файл лога"))

    def _on_message(self, msg):
        self._note(msg)
        if msg.strip():
            self.last_msg = msg.strip()

    def _on_live(self, speed, target, power, phase):
        self.l_big.configure(text="–" if speed is None else f"{speed:.1f}")
        self.l_unit.configure(text=" " + T("км/ч"))
        parts = []
        if target:
            parts.append(T("цель {v:.1f} км/ч", v=target))
        if power is not None:
            parts.append(f"{power} " + T("Вт"))
        self.l_detail.configure(text="  ·  ".join(parts))
        if phase:
            self._set_phase(phase, target=target)

    def _on_phase(self, key, text):
        self._set_phase(key, text)
        if key not in LIVE_PHASES:
            self._show_value(None, "")

    def _on_ready(self, prompt):
        self._set_phase("ready", T("{prompt} и нажми «Поехали» или пробел", prompt=prompt))
        self._show_value(None, "")
        self.b_go.configure(text=T("Поехали"))
        self.b_go.pack(pady=(0, 28))

    def _on_warmup(self, left, speed, power, riding):
        self._set_phase("prewarm" if riding else "prewarm_pause")
        parts = [("–" if speed is None else f"{speed:.1f}") + " " + T("км/ч")]
        if power is not None:
            parts.append(f"{power} " + T("Вт"))
        self._show_value((mmss(left, up=True), T("осталось")), "  ·  ".join(parts))
        # событие могло прийти уже после нажатия: тогда кнопку не возвращаем
        if not self.go.is_set() and not self.b_go.winfo_ismapped():
            self.b_go.configure(text=T("Калибровать сейчас"))
            self.b_go.pack(pady=(0, 28))

    def _on_finish(self, ok, detail, value, title):
        self.finished = True
        self._set_phase("stopped" if ok is None else "ok" if ok else "fail", title)
        self._show_value(value, detail)

    def _on_done(self):
        if not self.finished:
            self._set_phase("fail")
            self._show_value(None, self.last_msg)
        self._controls(running=False)
        if self.closing:
            self.root.destroy()

    def _note(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg.strip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_phase(self, key, text=None, target=None):
        color, title = PHASES[key]
        title = T(title) if title else ""
        if key == "accelerate" and target:
            title = T("РАЗГОНЯЙСЯ ДО {v:.0f} КМ/Ч", v=target)
        for w in self.tinted:
            w.configure(bg=color)
        self.l_cmd.configure(text=text or title)
        self.b_go.configure(fg=color, activeforeground=color)
        if key not in GO_PHASES:
            self.b_go.pack_forget()

    def _show_value(self, value, detail):
        number, unit = value if value else ("", "")
        self.l_big.configure(text=number)
        self.l_unit.configure(text=f" {unit}" if unit else "")
        self.l_detail.configure(text=detail)

    def _resize(self, e):
        # цифры растут вместе с окном: разверни его на весь экран, и скорость видно издалека
        self.f_big.configure(size=-max(60, int(min(e.height * 0.40, e.width * 0.24))))
        self.f_cmd.configure(size=-max(18, int(min(e.height * 0.075, e.width * 0.05))))
        self.f_unit.configure(size=-max(14, int(e.height * 0.06)))
        self.f_detail.configure(size=-max(12, int(e.height * 0.04)))
        self.l_cmd.configure(wraplength=max(200, e.width - 40))
        self.l_detail.configure(wraplength=max(200, e.width - 40))

    def _controls(self, running):
        idle = "disabled" if running else "!disabled"
        for b in (self.b_check, self.b_cal):
            b.state([idle])
        self.rb_ant.state(["disabled" if running or usb is None else "!disabled"])
        self.rb_ble.state(["disabled" if running or bleak is None else "!disabled"])
        self.cb_warm.state([idle])
        self.cb_lang.configure(state="disabled" if running else "readonly")
        self.b_stop.state(["!disabled" if running and self.worker is not None else "disabled"])

    def _start(self, info_only):
        if self.worker is not None and self.worker.is_alive():
            return
        STOP.clear()
        self.finished = False
        self.last_msg = ""
        args = ["--ant" if self.link.get() == "ant" else "--ble"] + ([] if self.warm.get() else ["--no-warmup"])
        self.worker = threading.Thread(target=self._work, args=(info_only, args), daemon=True)
        self._controls(running=True)
        self._set_phase("search")
        self._show_value(None, "")
        self.worker.start()

    def _work(self, info_only, args):
        try:
            session(info_only, args)
        finally:
            self.events.put(("done",))

    def _go(self):
        self._set_phase("prepare", T("Поехали!"))
        self.go.set()

    def _key_go(self, _event=None):
        # пробел или Enter — та же большая кнопка, чтобы с велосипеда не тянуться к мыши
        if self.b_go.winfo_ismapped() and not self.go.is_set() and not STOP.is_set():
            self._go()

    def _stop(self):
        STOP.set()
        self._set_phase("stopped", T("Останавливаю…"))

    def _close(self):
        if self.worker is None or not self.worker.is_alive():
            self.root.destroy()
            return
        # сначала отпустить донгл или Bluetooth, потом закрыть окно
        self.closing = True
        self._stop()
        self.root.after(10000, self.root.destroy)

    def _open_log(self):
        try:
            os.startfile(LOG_PATH)
        except OSError as e:
            self._note(T("Не открылся лог: {e}", e=e))


def run():
    global ui, LANG
    args = sys.argv[1:]
    LANG = pick_lang(args)
    # у exe без консоли (sys.stdout пустой) консольный режим некуда выводить: всегда окно
    if sys.stdout is None or ("--info" not in args and "--console" not in args):
        try:
            ui = GuiUI()
        except Exception as e:     # нет tkinter или нет экрана: работаем в консоли
            ui = ConsoleUI()
            log_only(T("  окно не открылось ({e}), работаю в консоли", e=f"{type(e).__name__}: {e}"))
        else:
            ui.root.mainloop()
            return
    session("--info" in args, args)
    say(T("\nЛог сохранён: {path}", path=LOG_PATH))
    close_window()


# ================================ English ================================
# Ключ — русский текст из T(), значение — перевод с теми же {подстановками}.
# Нет перевода — показывается русский текст.

EN = {
    "\nНажми Enter, чтобы закрыть...": "\nPress Enter to close...",
    "быстрее": "faster",
    "держи": "hold",
    "держи, прогрев": "hold, warming up",
    "НЕ КРУТИ, жди": "DON'T PEDAL, wait",
    "км/ч": "km/h",
    "об/мин": "rpm",
    "Вт": "W",
    "с": "s",
    "нет": "none",
    "цель {v:.1f}": "target {v:.1f}",
    "\n{prompt} и нажми Enter, чтобы начать калибровку...": "\n{prompt} and press Enter to start the calibration...",

    # прогрев
    "Нажми Enter, чтобы закончить прогрев и калибровать сейчас.": "Press Enter to end the warm-up and calibrate now.",
    "Пробел или Enter — калибровать сейчас, не дожидаясь конца прогрева.":
        "Space or Enter: calibrate now without waiting for the warm-up to end.",
    "осталось": "left",
    "прогрев: осталось {left}": "warm-up: {left} left",
    "крути педали": "keep pedaling",
    "Прогрев {min:.0f} мин: крути спокойно, в любом темпе, время идёт, только пока крутишь. "
    "Если станок уже тёплый, прогрев можно пропустить.":
        "Warm-up {min:.0f} min: pedal easily at any pace, the timer runs only while you pedal. "
        "If the trainer is already warm, you can skip the warm-up.",
    "педали крутились {ridden} из {total}": "pedaled {ridden} of {total}",
    "Прогрев пропущен ({took}).": "Warm-up skipped ({took}).",
    ">>> Прогрев закончен ({took}), начинаю калибровку.": ">>> Warm-up done ({took}), starting the calibration.",
    "Прогрев закончен, начинаю калибровку…": "Warm-up done, starting the calibration…",
    "  прогрев: {done} мин из {total:.0f}": "  warm-up: {done} of {total:.0f} min",

    # ANT+ донгл
    "  libusb0 недоступен: {e}": "  libusb0 not available: {e}",
    "  libusb-1.0 недоступен: {e}": "  libusb-1.0 not available: {e}",
    "  не нашлась ни одна библиотека libusb, работать с донглом нечем":
        "  no libusb library found, cannot talk to the dongle",
    "  {name}: ошибка поиска USB ({e})": "  {name}: USB search error ({e})",
    "Донгл: {model} (0fcf:{pid:04x}) через {name}": "Dongle: {model} (0fcf:{pid:04x}) via {name}",
    "  {name}: ANT+ донгл не виден": "  {name}: ANT+ dongle not visible",
    "донгл занят другой программой, закрой Zwift ({e})": "the dongle is busy with another program, close Zwift ({e})",
    "у донгла не найдены USB-эндпоинты": "the dongle has no USB endpoints",
    "донгл отклонил команду 0x{id:02x} (код {code})": "the dongle rejected command 0x{id:02x} (code {code})",
    "донгл не ответил на команду 0x{id:02x}": "the dongle did not answer command 0x{id:02x}",
    "ошибка USB: {e}": "USB error: {e}",

    # ANT+ станок
    ">>> Связь со станком пропала, ищу снова...": ">>> Lost the trainer, searching again...",
    "  первая стр. 0x{page:02x}: {hex}": "  first page 0x{page:02x}: {hex}",
    "  флаги станка: {flags:#x} (1 мощность, 2 сопротивление, 4 настройка пользователя)":
        "  trainer flags: {flags:#x} (1 power, 2 resistance, 4 user configuration)",
    "  [{state}] стр. 0x{page:02x}: {hex}": "  [{state}] page 0x{page:02x}: {hex}",
    ">>> Станок начал калибровку.": ">>> The trainer started the calibration.",
    "Целевая скорость {v:.1f} км/ч.": "Target speed {v:.1f} km/h.",
    "Разгоняйся до целевой скорости и держи её, пока не появится «ПЕРЕСТАНЬ КРУТИТЬ».":
        "Speed up to the target speed and hold it until you see “STOP PEDALING”.",
    ">>> Станок ещё холодный: держи целевую скорость, он прогреется. Скрипт скажет, когда отпускать.":
        ">>> The trainer is still cold: hold the target speed, it will warm up. The script will tell you when to stop.",
    ">>> Станок перегрет: перестань крутить, дай ему остыть 10 минут и запусти калибровку заново.":
        ">>> The trainer is too hot: stop pedaling, let it cool down for 10 minutes and run the calibration again.",
    ">>> Станок прогрелся.": ">>> The trainer has warmed up.",
    "  команда отпускать на скорости {v} км/ч, цель {target}": "  stop command at {v} km/h, target {target}",
    ">>> ПЕРЕСТАНЬ КРУТИТЬ: отпусти педали и жди, станок выбегает.":
        ">>> STOP PEDALING: let go of the pedals and wait while the trainer spins down.",
    "  попытка {n}: станок не подтвердил {what}, повторяю": "  attempt {n}: the trainer did not confirm the {what}, retrying",
    "запрос калибровки": "calibration request",
    "Станок просит настройки пользователя, отправляю стандартные ({user:.0f} кг, велосипед {bike:.0f} кг, "
    "колесо 700c). На калибровку не влияют.":
        "The trainer asks for user settings, sending the defaults ({user:.0f} kg rider, {bike:.0f} kg bike, "
        "700c wheel). They do not affect the calibration.",
    "настройки": "settings",
    "  Станок не подтвердил настройки, продолжаю без них.": "  The trainer did not confirm the settings, continuing without them.",
    "  после настроек флаги станка: {flags:#x}": "  trainer flags after the settings: {flags:#x}",
    "связь со станком по ANT+ потеряна во время прогрева": "lost the ANT+ connection to the trainer during the warm-up",
    "Ищу станок по ANT+ (до {sec:.0f} сек). Покрути педали, чтобы он проснулся.":
        "Looking for the trainer over ANT+ (up to {sec:.0f} s). Turn the pedals to wake it up.",
    "Станок по ANT+ не найден. Он включён? Zwift закрыт? Поднеси донгл ближе к станку.":
        "No trainer found over ANT+. Is it plugged in? Is Zwift closed? Move the dongle closer to the trainer.",
    "Станок найден, ANT+ ID {id}.": "Trainer found, ANT+ ID {id}.",
    "Станок найден": "Trainer found",
    "калибровку мощности": "power calibration",
    "калибровку сопротивления": "resistance calibration",
    "настройку пользователя": "user configuration",
    "Станок сам просит: {what}.": "The trainer asks for: {what}.",
    "Показываю данные {sec:.0f} сек, покрути педали:": "Showing data for {sec:.0f} s, turn the pedals:",
    "Максимум за это время: {v:.1f} км/ч, {cad} об/мин, {w} Вт.": "Maximum over this time: {v:.1f} km/h, {cad} rpm, {w} W.",
    "Скорость и каденс были нулевые: педали не крутились? Для проверки данных покрути и запусти снова.":
        "Speed and cadence were zero: were the pedals turning? To check the data, pedal and run it again.",
    "Каденс станок не передаёт, это нормально: для калибровки он не нужен.":
        "The trainer does not send cadence. That is normal, the calibration does not need it.",
    "  страницы от станка: {pages}": "  pages from the trainer: {pages}",
    "Связь со станком по ANT+ есть, калибровку можно запускать.":
        "The ANT+ connection to the trainer works, you can run the calibration.",
    "по ANT+, максимум {v:.1f} км/ч и {w} Вт": "over ANT+, maximum {v:.1f} km/h and {w} W",
    "СВЯЗЬ ЕСТЬ": "CONNECTED",
    "Сядь на станок, начни спокойно крутить": "Get on the bike and start pedaling easily",
    "Станок не подтвердил запрос калибровки. Поднеси донгл ближе и повтори.":
        "The trainer did not confirm the calibration request. Move the dongle closer and try again.",
    "Запрос калибровки принят станком, жду начала...": "The trainer accepted the calibration request, waiting for it to start...",
    "Жду, когда станок начнёт калибровку…": "Waiting for the trainer to start the calibration…",
    "  +{t:5.1f} с: {v} км/ч, {cad} об/мин, {w} Вт, состояние {state}, скорость {sc}, температура {tc}":
        "  +{t:5.1f} s: {v} km/h, {cad} rpm, {w} W, state {state}, speed {sc}, temperature {tc}",
    "Связь со станком потеряна, калибровка прервана. Поднеси донгл ближе и повтори.":
        "Lost the trainer, calibration aborted. Move the dongle closer and try again.",
    "Станок принял запрос, но калибровку не начал. Пришли hammer log.txt.":
        "The trainer accepted the request but did not start the calibration. Send hammer log.txt.",
    "Станок не сообщил результат за {sec:.0f} сек. Калибровка не завершена, повтори.":
        "The trainer sent no result within {sec:.0f} s. The calibration did not finish, try again.",
    "Время выбега {v:.1f} с.": "Spindown time {v:.1f} s.",
    "Температура {v:.1f} °C.": "Temperature {v:.1f} °C.",
    "  от команды «перестань крутить» до ответа: {v:.1f} с": "  from the “stop pedaling” command to the result: {v:.1f} s",
    "Станок меряет замедление на отрезке скоростей, поэтому маховик ещё может крутиться, это нормально.":
        "The trainer measures the slowdown over a speed range, so the flywheel may still be spinning. That is normal.",
    "  флаги станка после калибровки: {flags}": "  trainer flags after the calibration: {flags}",
    "Станок всё ещё выставляет флаг «нужна калибровка». Повтори калибровку.":
        "The trainer still sets the “calibration needed” flag. Run the calibration again.",
    "станок всё ещё просит калибровку, повтори": "the trainer still asks for calibration, try again",
    "Станок снял флаг «нужна калибровка»: результат принят и записан в станок.":
        "The trainer cleared the “calibration needed” flag: the result was accepted and saved in the trainer.",
    ", температура {v:.1f} °C": ", temperature {v:.1f} °C",
    "Станок сообщил, что калибровка не удалась (флаги 0x{flags:02x}).":
        "The trainer reported that the calibration failed (flags 0x{flags:02x}).",
    "Повтори: держи целевую скорость, а после «ПЕРЕСТАНЬ КРУТИТЬ» не трогай педали до конца выбега.":
        "Try again: hold the target speed, and after “STOP PEDALING” don't touch the pedals until the spindown ends.",
    "держи целевую скорость, а после «перестань крутить» не трогай педали":
        "hold the target speed, and after “stop pedaling” don't touch the pedals",

    # Bluetooth
    "производитель": "manufacturer",
    "модель": "model",
    "серийный номер": "serial number",
    "железо": "hardware",
    "прошивка": "firmware",
    "свободный": "free ride",
    "калибровка": "calibration",
    "тест выбега": "spindown test",
    "  первые данные Cycling Power: {hex}": "  first Cycling Power data: {hex}",
    ">>> Станок готовит тест: крути дальше, держи скорость.": ">>> The trainer is preparing the test: keep pedaling, hold the speed.",
    "команду": "command",
    "  ответ на {what} пришёл с другим типом, принимаю": "  the answer to the {what} came with another type, accepting it",
    "  нет ответа на {what}": "  no answer to the {what}",
    "  попытка {n}: станок не ответил на {what}, повторяю": "  attempt {n}: the trainer did not answer the {what}, retrying",
    "станок отклонил {what} (ответ {hex})": "the trainer rejected the {what} (answer {hex})",
    "станок не ответил на {what}": "the trainer did not answer the {what}",
    "режим {name}": "{name} mode",
    "запрос режима": "mode request",
    "запрос времени выбега": "spindown time request",
    "  время выбега в незнакомом формате: {hex}": "  spindown time in an unknown format: {hex}",
    "  найдено: {name} {addr}, сигнал {rssi} дБм, сервисы {uuids}": "  found: {name} {addr}, signal {rssi} dBm, services {uuids}",
    "  подключение, попытка {n}: {err}: {e}": "  connecting, attempt {n}: {err}: {e}",
    "  Не подключилось, пробую ещё раз...": "  Could not connect, trying again...",
    "не удалось подключиться к станку по Bluetooth. Закрой Zwift и приложения на телефоне.":
        "could not connect to the trainer over Bluetooth. Close Zwift and the apps on your phone.",
    "  сервис {uuid}": "  service {uuid}",
    "  не прочиталось {what}: {e}": "  could not read the {what}: {e}",
    "Станок: {info}.": "Trainer: {info}.",
    "связь со станком по Bluetooth пропала": "lost the Bluetooth connection to the trainer",
    "Ставлю нагрузку {w} Вт и включаю режим калибровки (так делает приложение Saris)...":
        "Setting a {w} W load and entering calibration mode (as the Saris app does)...",
    "Ставлю нагрузку {w} Вт…": "Setting a {w} W load…",
    "нагрузку {w} Вт": "{w} W load",
    "Крути спокойно, пока не разгоняйся...": "Pedal easily, don't speed up yet...",
    "Крути спокойно, пока не разгоняйся": "Pedal easily, don't speed up yet",
    "Разгоняйся до {v:.0f} км/ч. Скрипт сам запустит тест и скажет, когда перестать крутить.":
        "Speed up to {v:.0f} km/h. The script will start the test and tell you when to stop pedaling.",
    "связь со станком по Bluetooth пропала, калибровка прервана":
        "lost the Bluetooth connection to the trainer, calibration aborted",
    "  +{t:5.1f} с: {v} км/ч, {w} Вт, режим {mode}, состояние {state}":
        "  +{t:5.1f} s: {v} km/h, {w} W, mode {mode}, state {state}",
    ">>> {v:.0f} км/ч есть, запускаю тест. Крути дальше, пока не появится «ПЕРЕСТАНЬ КРУТИТЬ».":
        ">>> {v:.0f} km/h reached, starting the test. Keep pedaling until you see “STOP PEDALING”.",
    "запуск теста выбега": "spindown test start",
    "Станок сообщил, что калибровка не удалась.": "The trainer reported that the calibration failed.",
    "Повтори: разгонись до {v:.0f} км/ч, а после «ПЕРЕСТАНЬ КРУТИТЬ» не трогай педали до конца выбега.":
        "Try again: speed up to {v:.0f} km/h, and after “STOP PEDALING” don't touch the pedals until the spindown ends.",
    "разгонись до {v:.0f} км/ч, а после «перестань крутить» не трогай педали":
        "speed up to {v:.0f} km/h, and after “stop pedaling” don't touch the pedals",
    "  результат от станка: {r}": "  result from the trainer: {r}",
    "Станок вышел из теста, не дойдя до выбега, и время выбега не изменилось. Повтори калибровку.":
        "The trainer left the test before the spindown, and the spindown time did not change. Run the calibration again.",
    "станок вышел из теста раньше выбега, повтори": "the trainer left the test before the spindown, try again",
    "КАЛИБРОВКА УСПЕШНА.": "CALIBRATION SUCCESSFUL.",
    "Время выбега {v:.2f} с (было {old:.2f} с).": "Spindown time {v:.2f} s (was {old:.2f} s).",
    "Время выбега {v:.2f} с.": "Spindown time {v:.2f} s.",
    "Можно запускать Zwift.": "You can start Zwift now.",
    "время выбега": "spindown time",
    ", было {old:.2f} с": ", was {old:.2f} s",
    "Ищу станок по Bluetooth (до {sec:.0f} сек). Покрути педали, чтобы он проснулся.":
        "Looking for the trainer over Bluetooth (up to {sec:.0f} s). Turn the pedals to wake it up.",
    "Станок по Bluetooth не найден. Он включён? Zwift и приложения на телефоне закрыты?\n"
    "Если Bluetooth станка занят телефоном, выключи на телефоне Bluetooth.":
        "No trainer found over Bluetooth. Is it plugged in? Are Zwift and the phone apps closed?\n"
        "If your phone holds the trainer's Bluetooth connection, turn Bluetooth off on the phone.",
    "Нашёл {name}, подключаюсь...": "Found {name}, connecting...",
    "Подключаюсь…": "Connecting…",
    "Подключено.": "Connected.",
    "У этого устройства нет фирменной характеристики CycleOps: это не Hammer, или прошивка другая.":
        "This device has no CycleOps characteristic: it is not a Hammer, or its firmware is different.",
    "Зато есть стандартный FTMS: такой станок калибруй по ANT+ этим скриптом или из Zwift по Bluetooth.":
        "It does have standard FTMS: calibrate such a trainer over ANT+ with this script, or from Zwift over Bluetooth.",
    "Сейчас в станке записано время выбега {v:.2f} с.": "Spindown time stored in the trainer: {v:.2f} s.",
    "Время выбега в станке пока не записано.": "No spindown time is stored in the trainer yet.",
    "  режим станка: {name}": "  trainer mode: {name}",
    "Максимум за это время: {v:.1f} км/ч, {w} Вт.": "Maximum over this time: {v:.1f} km/h, {w} W.",
    "Станок не прислал данные мощности. Без них скрипт не увидит скорость и не запустит тест.":
        "The trainer sent no power data. Without it the script cannot see the speed or start the test.",
    "Станок не передаёт скорость по Bluetooth: калибровка по этому скрипту не запустится. Пришли hammer log.txt.":
        "The trainer does not send speed over Bluetooth, so this script cannot calibrate it. Send hammer log.txt.",
    "Скорость была нулевая: педали не крутились? Для проверки данных покрути и запусти снова.":
        "Speed was zero: were the pedals turning? To check the data, pedal and run it again.",
    "Связь со станком по Bluetooth есть, калибровку можно запускать.":
        "The Bluetooth connection to the trainer works, you can run the calibration.",
    ", в станке записан выбег {v:.2f} с": ", stored spindown {v:.2f} s",
    "по Bluetooth{recorded}, максимум {v:.1f} км/ч и {w} Вт": "over Bluetooth{recorded}, maximum {v:.1f} km/h and {w} W",
    "У станка нет Cycling Power: скрипт не увидит скорость. Пришли hammer log.txt.":
        "The trainer has no Cycling Power service, so the script cannot see the speed. Send hammer log.txt.",
    "Сядь на станок": "Get on the bike",

    # запуск
    "Нет библиотеки bleak для Bluetooth. Установи командой: python -m pip install bleak":
        "The bleak library for Bluetooth is missing. Install it with: python -m pip install bleak",
    "Нет библиотеки pyusb для ANT+. Установи командой: python -m pip install pyusb libusb-package":
        "The pyusb library for ANT+ is missing. Install it with: python -m pip install pyusb libusb-package",
    "ANT+ донгл не найден. Вставь его в USB (лучше через удлинитель поближе к станку).":
        "No ANT+ dongle found. Plug it into USB (ideally on an extension cable close to the trainer).",
    "ANT+ донгл не найден, подключаюсь по Bluetooth.": "No ANT+ dongle found, connecting over Bluetooth.",
    "Как подключаться к станку? 1 = ANT+ донгл, 2 = Bluetooth [Enter = 1]: ":
        "How to connect to the trainer? 1 = ANT+ dongle, 2 = Bluetooth [Enter = 1]: ",
    "  выбрано: {how}": "  selected: {how}",
    "проверка связи": "connection check",
    "\nПрервано.": "\nStopped.",
    "ОШИБКА: {e}": "ERROR: {e}",
    "Проверь, что Zwift закрыт, донгл вставлен, а Bluetooth включён.":
        "Check that Zwift is closed, the dongle is plugged in and Bluetooth is on.",
    "  окно не открылось ({e}), работаю в консоли": "  the window did not open ({e}), using the console",
    "\nЛог сохранён: {path}": "\nLog saved: {path}",

    # окно
    "Выбери связь и нажми «Калибровать»": "Choose the connection and press “Calibrate”",
    "Ищу станок… Покрути педали, чтобы он проснулся": "Looking for the trainer… Turn the pedals to wake it up",
    "Покрути педали: проверяю данные": "Turn the pedals: checking the data",
    "ПРОГРЕВ: КРУТИ СПОКОЙНО": "WARM-UP: PEDAL EASILY",
    "ПРОГРЕВ НА ПАУЗЕ: КРУТИ ПЕДАЛИ": "WARM-UP PAUSED: KEEP PEDALING",
    "РАЗГОНЯЙСЯ": "SPEED UP",
    "РАЗГОНЯЙСЯ ДО {v:.0f} КМ/Ч": "SPEED UP TO {v:.0f} KM/H",
    "ДЕРЖИ СКОРОСТЬ": "HOLD THE SPEED",
    "ДЕРЖИ, СТАНОК ПРОГРЕВАЕТСЯ": "HOLD IT, THE TRAINER IS WARMING UP",
    "ПЕРЕСТАНЬ КРУТИТЬ": "STOP PEDALING",
    "ГОТОВО": "DONE",
    "НЕ ПОЛУЧИЛОСЬ": "FAILED",
    "Остановлено": "Stopped",
    "  поиск донгла: {e}": "  dongle search: {e}",
    "ANT+ донгл найден.": "ANT+ dongle found.",
    "ANT+ донгл не найден, выбран Bluetooth.": "No ANT+ dongle found, Bluetooth selected.",
    "Нет библиотеки pyusb: ANT+ недоступен.": "The pyusb library is missing: ANT+ is not available.",
    "Нет библиотеки bleak: Bluetooth недоступен.": "The bleak library is missing: Bluetooth is not available.",
    "Калибровка Hammer": "Hammer Calibration",
    "Связь:": "Connection:",
    "ANT+ донгл": "ANT+ dongle",
    "Прогрев {min:.0f} мин": "Warm-up {min:.0f} min",
    "Стоп": "Stop",
    "Калибровать": "Calibrate",
    "Проверить связь": "Check connection",
    "Журнал": "Log",
    "Открыть файл лога": "Open log file",
    "цель {v:.1f} км/ч": "target {v:.1f} km/h",
    "{prompt} и нажми «Поехали» или пробел": "{prompt} and press “Go” or Space",
    "Поехали": "Go",
    "Поехали!": "Go!",
    "Калибровать сейчас": "Calibrate now",
    "Останавливаю…": "Stopping…",
    "Не открылся лог: {e}": "Could not open the log: {e}",
}


if __name__ == "__main__":
    run()
