#!/usr/bin/env python
#
# (c) Copyright 2018 by Coinkite Inc. This file is covered by license found in COPYING-CC.
#
# Simulate the hardware of a Coldcard. Particularly the OLED display (128x32) and 
# the number pad. 
#
# This is a normal python3 program, not micropython. It communicates with a running
# instance of micropython that simulates the micropython that would be running in the main
# chip.
#
# Limitations:
# - USB light not fully implemented, because happens at irq level on real product
#
import os, sys, tty, pty, termios, time, pdb, tempfile, struct
import subprocess
import sdl2.ext
from PIL import Image, ImageSequence
from select import select
import fcntl
from binascii import b2a_hex, a2b_hex
from bare import BareMetal
from sdl2.scancode import *     # SDL_SCANCODE_F1.. etc

MPY_UNIX = 'l-port/micropython'

UNIX_SOCKET_PATH = '/tmp/ckcc-simulator.sock'


class SimulatedScreen:
    # a base class

    def snapshot(self):
        fn = time.strftime('../snapshot-%j-%H%M%S.png')
        with tempfile.NamedTemporaryFile() as tmp:
            sdl2.SDL_SaveBMP(self.sprite.surface, tmp.name.encode('ascii'))
            tmp.file.seek(0)
            img = Image.open(tmp.file)
            img.save(fn)

        print("Snapshot saved: %s" % fn.split('/', 1)[1])

    def movie_start(self):
        self.movie = []
        self.last_frame = time.time() - 0.1
        print("Movie recording started.")
        self.new_frame()

    def movie_end(self):
        fn = time.strftime('../movie-%j-%H%M%S.gif')

        if not self.movie: return

        dt0, img = self.movie[0]

        img.save(fn, save_all=True, append_images=[fr for _,fr in self.movie[1:]],
                        duration=[max(dt, 20) for dt,_ in self.movie], loop=50)

        print("Movie saved: %s (%d frames)" % (fn.split('/', 1)[1], len(self.movie)))

        self.movie = None

    def new_frame(self):
        dt = int((time.time() - self.last_frame) * 1000)
        self.last_frame = time.time()

        with tempfile.NamedTemporaryFile() as tmp:
            sdl2.SDL_SaveBMP(self.sprite.surface, tmp.name.encode('ascii'))
            tmp.file.seek(0)
            img = Image.open(tmp.file)
            img = img.convert('P')
            self.movie.append((dt, img))

class LCDSimulator(SimulatedScreen):
    # where the simulated screen is, relative to fixed background
    TOPLEFT = (65, 60)
    background_img = 'q1-images/background.png'

    # see stm32/COLDCARD_Q1/modckcc.c where this pallet is defined.
    palette_colours = [
            '#000', '#fff',  # black/white, must be 0/1
            '#f00', '#0f0', '#00f',     # RGB demos
            # some greys: 5 .. 12
            '#555', '#999', '#ddd', '#111111', '#151515', '#191919', '#1d1d1d',
            # tbd/unused
            '#200', '#400', '#800',
            # #15: Coinkite brand
            '#f16422'
        ]

    def __init__(self, factory):
        self.movie = None

        self.sprite = s = factory.create_software_sprite( (320,240), bpp=32)
        s.x, s.y = self.TOPLEFT
        s.depth = 100

        self.palette = [sdl2.ext.prepare_color(code, s) for code in self.palette_colours]
        assert len(self.palette) == 16

        sdl2.ext.fill(s, self.palette[0])

        self.mv = sdl2.ext.PixelView(self.sprite)
    
        # for any LED's .. no position implied
        self.led_red = factory.from_image("q1-images/led-red.png")
        self.led_green = factory.from_image("q1-images/led-green.png")

    def new_contents(self, readable):
        # got bytes for new update. expect a header and packed pixels
        while 1:
            prefix = readable.read(8)
            if not prefix: return
            X,Y, w, h = struct.unpack('<4H', prefix)

            assert X>=0 and Y>=0
            assert X+w <= 320
            assert Y+h <= 240

            sz = w*h
            here = readable.read(sz)
            assert len(here) == sz

            pos = 0
            for y in range(Y, Y+h):
                for x in range(X, X+w):
                    val = here[pos]
                    pos += 1
                    self.mv[y][x] = self.palette[val & 0xf]

        if self.movie is not None:
            self.new_frame()

    def click_to_key(self, x, y):
        # take a click on image => keypad key if valid
        # - not planning to support, tedious
        return None

    def draw_leds(self, spriterenderer, active_set=0):
        # always draw SE led, since one is always on
        GEN_LED = 0x1
        SD_LED = 0x2
        USB_LED = 0x4

        spriterenderer.render(self.led_green if (active_set & GEN_LED) else self.led_red)

        if active_set & SD_LED:
            spriterenderer.render(self.led_sdcard)
        if active_set & USB_LED:
            spriterenderer.render(self.led_usb)

class OLEDSimulator(SimulatedScreen):
    # top-left coord of OLED area; size is 1:1 with real pixels... 128x64 pixels
    OLED_ACTIVE = (46, 85)

    # keypad touch buttons
    KEYPAD_LEFT = 52
    KEYPAD_TOP = 216
    KEYPAD_PITCH = 73

    background_img = 'mk4-images/background.png'

    def __init__(self, factory):
        self.movie = None

        s = factory.create_software_sprite( (128,64), bpp=32)
        self.sprite = s
        s.x, s.y = self.OLED_ACTIVE
        s.depth = 100

        self.fg = sdl2.ext.prepare_color('#ccf', s)
        self.bg = sdl2.ext.prepare_color('#111', s)
        sdl2.ext.fill(s, self.bg)

        self.mv = sdl2.ext.PixelView(self.sprite)
    
        # for genuine/caution lights and other LED's
        self.led_red = factory.from_image("mk4-images/led-red.png")
        self.led_green = factory.from_image("mk4-images/led-green.png")
        self.led_sdcard = factory.from_image("mk4-images/led-sd.png")
        self.led_usb = factory.from_image("mk4-images/led-usb.png")

    def new_contents(self, readable):
        # got bytes for new update.

        # Must be bigger than a full screen update.
        buf = readable.read(1024*1000)
        if not buf:
            return

        buf = buf[-1024:]       # ignore backlogs, get final state
        assert len(buf) == 1024, len(buf)

        for y in range(0, 64, 8):
            line = buf[y*128//8:]
            for x in range(128):
                val = buf[(y*128//8) + x]
                mask = 0x01
                for i in range(8):
                    self.mv[y+i][x] = self.fg if (val & mask) else self.bg
                    mask <<= 1

        if self.movie is not None:
            self.new_frame()

    def click_to_key(self, x, y):
        # take a click on image => keypad key if valid
        col = ((x - self.KEYPAD_LEFT) // self.KEYPAD_PITCH)
        row = ((y - self.KEYPAD_TOP) // self.KEYPAD_PITCH)

        #print('rc= %d,%d' % (row,col))
        if not (0 <= row < 4): return None
        if not (0 <= col < 3): return None

        return '123456789x0y'[(row*3) + col]

    def draw_leds(self, spriterenderer, active_set=0):
        # always draw SE led, since one is always on
        GEN_LED = 0x1
        SD_LED = 0x2
        USB_LED = 0x4

        spriterenderer.render(self.led_green if (active_set & GEN_LED) else self.led_red)

        if active_set & SD_LED:
            spriterenderer.render(self.led_sdcard)
        if active_set & USB_LED:
            spriterenderer.render(self.led_usb)

def shift_up(ch):
    # what ascii code for ascii key, ch, when shift also pressed?
    # IMPORTANT: this has nothing to do with Q1's keyboard layout
    if 'a' <= ch <= 'z':
        return ch.upper()

    f,t = '1234567890-=\`[];\',./', \
          '!@#$%^&*()_+|~{}:"<>?'

    idx = f.find(ch)
    return t[idx] if idx != -1 else ch

def load_shared_mod(name, path):
    # load indicated file.py as a module
    # from <https://stackoverflow.com/questions/67631/how-to-import-a-module-given-the-full-path>
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

q1_charmap = load_shared_mod('charcodes', '../shared/charcodes.py')

def scancode_remap(sc):
    # return an ACSII (non standard) char to represent arrows and other similar
    # special keys on Q1 only.
    # - see ENV/lib/python3.10/site-packages/sdl2/scancode.py
    # - select/cancel/tab/bs all handled already 
    # - NFC, lamp, QR buttons in alt_up()

    m = {
        SDL_SCANCODE_RIGHT: q1_charmap.KEY_RIGHT,
        SDL_SCANCODE_LEFT: q1_charmap.KEY_LEFT,
        SDL_SCANCODE_DOWN: q1_charmap.KEY_DOWN,
        SDL_SCANCODE_UP: q1_charmap.KEY_UP,
        SDL_SCANCODE_HOME: q1_charmap.KEY_HOME,
        SDL_SCANCODE_END: q1_charmap.KEY_END,
        SDL_SCANCODE_PAGEDOWN: q1_charmap.KEY_PAGE_DOWN,
        SDL_SCANCODE_PAGEUP: q1_charmap.KEY_PAGE_UP,

        SDL_SCANCODE_F1: q1_charmap.KEY_F1,
        SDL_SCANCODE_F2: q1_charmap.KEY_F2,
        SDL_SCANCODE_F3: q1_charmap.KEY_F3,
        SDL_SCANCODE_F4: q1_charmap.KEY_F4,
        SDL_SCANCODE_F5: q1_charmap.KEY_F5,
        SDL_SCANCODE_F6: q1_charmap.KEY_F6,
    }

    return m[sc] if sc in m else None

def alt_up(ch):
    # ALT+(ch) => special needs of Q1
    print(f"Alt: {ch}")
    if ch == 'n':
        return q1_charmap.KEY_NFC
    if ch == 'q':
        return q1_charmap.KEY_QR
    if ch == 'l':
        return q1_charmap.KEY_LAMP

    return None


def start():
    print('''\nColdcard Simulator: Commands (over simulated window):
  - Control-Q to quit
  - ^Z to snapshot screen.
  - ^S/^E to start/end movie recording
  - ^N to capture NFC data (tap it)
''')
    sdl2.ext.init()
    sdl2.SDL_EnableScreenSaver()

    is_q1 = ('--q1' in sys.argv)

    factory = sdl2.ext.SpriteFactory(sdl2.ext.SOFTWARE)
    simdis = (OLEDSimulator if not is_q1 else LCDSimulator)(factory)
    bg = factory.from_image(simdis.background_img)

    window = sdl2.ext.Window("Coldcard Simulator", size=bg.size, position=(100, 100))
    window.show()

    ico = factory.from_image('program-icon.png')
    sdl2.SDL_SetWindowIcon(window.window, ico.surface)

    spriterenderer = factory.create_sprite_render_system(window)

    # initial state
    spriterenderer.render(bg)
    spriterenderer.render(simdis.sprite)
    genuine_state = False
    simdis.draw_leds(spriterenderer)

    # capture exec path and move into intended working directory
    env = os.environ.copy()
    env['MICROPYPATH'] = ':' + os.path.realpath('../shared')

    display_r, display_w = os.pipe()      # fancy OLED display
    led_r, led_w = os.pipe()        # genuine LED
    numpad_r, numpad_w = os.pipe()  # keys

    # manage unix socket cleanup for client
    def sock_cleanup():
        import os
        fp = UNIX_SOCKET_PATH
        if os.path.exists(fp):
            os.remove(fp)
    sock_cleanup()
    import atexit
    atexit.register(sock_cleanup)

    # handle connection to real hardware, on command line
    # - open the serial device
    # - get buffering/non-blocking right
    # - pass in open fd numbers
    pass_fds = [display_w, numpad_r, led_w]

    if '--metal' in sys.argv:
        # bare-metal access: use a real Coldcard's bootrom+SE.
        metal_req_r, metal_req_w = os.pipe()
        metal_resp_r, metal_resp_w = os.pipe()

        bare_metal = BareMetal(metal_req_r, metal_resp_w)
        pass_fds.append(metal_req_w)
        pass_fds.append(metal_resp_r)
        metal_args = [ '--metal', str(metal_req_w), str(metal_resp_r) ]
        sys.argv.remove('--metal')
    else:
        metal_args = []
        bare_metal = None

    os.chdir('./work')
    cc_cmd = ['../coldcard-mpy', 
                        '-X', 'heapsize=9m',
                        '-i', '../sim_boot.py',
                        str(display_w), str(numpad_r), str(led_w)] \
                        + metal_args + sys.argv[1:]
    xterm = subprocess.Popen(['xterm', '-title', 'Coldcard Simulator REPL',
                                '-geom', '132x40+650+40', '-e'] + cc_cmd,
                                env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                pass_fds=pass_fds, shell=False)


    # reopen as binary streams
    display_rx = open(display_r, 'rb', closefd=0, buffering=0)
    led_rx = open(led_r, 'rb', closefd=0, buffering=0)
    numpad_tx = open(numpad_w, 'wb', closefd=0, buffering=0)

    # setup no blocking
    for r in [display_rx, led_rx]:
        fl = fcntl.fcntl(r, fcntl.F_GETFL)
        fcntl.fcntl(r, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    readables = [display_rx, led_rx]
    if bare_metal:
        readables.append(bare_metal.request)

    running = True
    pressed = set()

    def send_event(ch, is_down):
        #print(f'{ch} down={is_down}')
        if is_down:
            if ch not in pressed:
                numpad_tx.write(ch.encode())
                pressed.add(ch)
        else:
            pressed.discard(ch)
            if not pressed:
                numpad_tx.write(b'\0')      # all up signal


    while running:
        events = sdl2.ext.get_events()
        for event in events:
            if event.type == sdl2.SDL_QUIT:
                running = False
                break

            if event.type == sdl2.SDL_KEYUP or event.type == sdl2.SDL_KEYDOWN:
                try:
                    ch = chr(event.key.keysym.sym)
                    #print('0x%0x => chr %s  mod=0x%x'%(event.key.keysym.sym, ch, event.key.keysym.mod))
                    if event.key.keysym.mod & 0x3:      # left or right shift
                        ch = shift_up(ch)
                    if event.key.keysym.mod & 0x300:      # left or right ALT
                        ch = alt_up(ch)
                except:
                    # things like 'shift' by itself and anything not really ascii

                    scancode = event.key.keysym.sym & 0xffff
                    #print(f'keysym=0x%0x => {scancode}' % event.key.keysym.sym)
                    if is_q1:
                        ch = scancode_remap(scancode)
                        if not ch: continue
                    elif SDL_SCANCODE_RIGHT <= scancode <= SDL_SCANCODE_UP:
                        # arrow keys remap for Mk4
                        ch = '9785'[scancode - SDL_SCANCODE_RIGHT]
                    else:
                        print('Ignore: 0x%0x' % event.key.keysym.sym)
                        continue

                # control+KEY => for our use
                if event.key.keysym.mod == 0x40 and event.type == sdl2.SDL_KEYDOWN:
                    if ch == 'q':
                        # control-Q
                        running = False
                        break

                    if ch == 'n':
                        # see sim_nfc.py
                        try:
                            nfc = open('nfc-dump.ndef', 'rb').read()
                            fn = time.strftime('../nfc-%j-%H%M%S.bin')
                            open(fn, 'wb').write(nfc)
                            print(f"Simulated NFC read: {len(nfc)} bytes into {fn}")
                        except FileNotFoundError:
                            print("NFC not ready")

                    if ch in 'zse':
                        if ch == 'z':
                            simdis.snapshot()
                        if ch == 's':
                            simdis.movie_start()
                        if ch == 'e':
                            simdis.movie_end()
                        continue

                    if ch == 'm':
                        # do many OK's in a row ... for word nest menu
                        for i in range(30):
                            numpad_tx.write(b'y\n')
                            numpad_tx.write(b'\n')
                        continue

                if event.key.keysym.mod == 0x40:
                    # control key releases: ignore
                    continue

                # remap ESC/Enter 
                if not is_q1:
                    if ch == '\x1b':
                        ch = 'x'
                    elif ch == '\x0d':
                        ch = 'y'

                    if ch not in '0123456789xy':
                        if ch.isprintable():
                            print("Invalid key: '%s'" % ch)
                        continue
                    
                # need this to kill key-repeat
                send_event(ch, event.type == sdl2.SDL_KEYDOWN)

            if event.type == sdl2.SDL_MOUSEBUTTONDOWN:
                #print('xy = %d, %d' % (event.button.x, event.button.y))
                ch = simdis.click_to_key(event.button.x, event.button.y)
                if ch is not None:
                    send_event(ch, True)

            if event.type == sdl2.SDL_MOUSEBUTTONUP:
                for ch in list(pressed):
                    send_event(ch, False)

        rs, ws, es = select(readables, [], [], .001)
        for r in rs:

            if bare_metal and r == bare_metal.request:
                bare_metal.readable()
                continue
        
            if r is display_rx:
                simdis.new_contents(r)
                spriterenderer.render(simdis.sprite)
                window.refresh()
            elif r is led_rx:
                # XXX 8+8 bits
                c = r.read(1)
                if not c:
                    break

                c = c[0]
                if 1:
                    #print("LED change: 0x%02x" % c[0])

                    mask = (c >> 4) & 0xf
                    lset = c & 0xf

                    active_set = (mask & lset)

                    #print("Genuine LED: %r" % genuine_state)
                    spriterenderer.render(bg)
                    spriterenderer.render(simdis.sprite)
                    simdis.draw_leds(spriterenderer, active_set)

                window.refresh()
            else:
                pass

        if xterm.poll() != None:
            print("\r\n<xterm stopped: %s>\r\n" % xterm.poll())
            break

    xterm.kill()
    

if __name__ == '__main__':
    start()
