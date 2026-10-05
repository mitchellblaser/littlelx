/*
 * littlelx - Arduino Mega 2560 firmware.
 *
 * The Mega is the only thing plugged into the computer. It:
 *   - reports every input pin change and analog value over USB, so the wiring
 *     does not need to be known in advance (the computer "learns" it),
 *   - scans key matrices without knowing their layout: it pulls one pin LOW
 *     at a time and reports any other pin that follows (a pressed key joins
 *     the two). Any number/size of matrices, with or without diodes,
 *   - relays the Raspberry Pi touchscreen on Serial3:
 *       Mega TX3 (pin 14) -> divider (1k + 2k) -> Pi GPIO15 / RXD (header pin 10)
 *       Mega RX3 (pin 15) <- Pi GPIO14 / TXD (header pin 8), plus GND.
 *
 * USB protocol (500000 baud, text lines):
 *   Mega -> computer
 *     HELLO littlelx-mega 1
 *     PI 3                touchscreen port (Serial3)
 *     D<pin> <0|1>        digital pin changed (pullups: 0 = pressed)
 *     A<ch> <0..1023>     analog input A<ch> changed
 *     K<a> <b> <1|0>      matrix key between pins a < b pressed / released
 *     MX <pins...>        (after ?) pins in the matrix scan
 *     AN <channels...>    (after ?) analog inputs (faders)
 *     ><text>             line from the Pi
 *   computer -> Mega
 *     ?                   HELLO, PI and a dump of every input
 *     M<pin> <mode>       0 off, 1 input, 2 input+pullup, 3 analog (A0-A15 only)
 *                         add 8 for no debounce (rotary encoders)
 *     ><text>             send line to the Pi (streamed through byte by byte,
 *                         so lines of any length and full-speed updates work)
 *     X                   matrix probe (diagnostics, raw, no debounce):
 *                         XR <pins LOW at rest>, X<p> <pins that follow p
 *                         when p is pulled LOW>..., XE
 *     R<addr> <len>       read EEPROM (len <= 32)       -> E<addr> <hex>
 *     W<addr> <hex>       write EEPROM (<= 32 bytes)    -> W<addr> OK
 *
 * EEPROM (written by the computer, so the setup lives on the controller):
 *     0-1    'L' 'X'      pin mode table present
 *     2-71   pin modes    applied at power-up (0xff = default)
 *     128-   learned setup blob (the computer's format; the Mega only stores it)
 *
 * Defaults: D2-D53 = input+pullup (debounced). A0-A15 are tested at power-up:
 * a pin driven by something (a fader wiper) stays analog; a floating one (a
 * matrix line, a button to GND, or nothing) becomes input+pullup, so key
 * matrices on analog pins are scanned too. A learned setup in EEPROM wins.
 * Matrix scanning uses every pin in mode 2 (pullup, debounced); encoders
 * (mode 10) and analog pins are left out.
 * Pins 0/1 (USB) and 14/15 (Pi) are not scanned; everything else is.
 */

#include <EEPROM.h>

#define NPINS 70
#define DEBOUNCE_MS 4
#define ANALOG_STEP 6     /* counts; the bridge sends whole % (~10 counts) */
#define PI_BAUD 250000    /* exact on the AVR and the Pi; tolerant of so-so wiring */
#define PISER Serial3
#define PI_TX 14
#define PI_RX 15

enum { M_OFF = 0, M_INPUT = 1, M_PULLUP = 2, M_ANALOG = 3, M_RAW = 8 };

static uint8_t mode[NPINS];
static volatile uint8_t *pinreg[NPINS];
static uint8_t pinmask[NPINS];
static volatile uint8_t *portreg[NPINS], *ddrreg[NPINS];

/* matrix scanning */
#define MX_MAX 24                 /* keys tracked at once */
#define MX_PRESS 2                /* full scans a key must be seen to count */
#define MX_RELEASE 2              /* full scans it must be gone to release */
static uint8_t mx_pins[NPINS], mx_n, mx_idx;
static bool mx_dirty = true;
static uint8_t mx_found[MX_MAX][2], mx_nfound;
static struct { uint8_t a, b, seen, missed, down; } mx_keys[MX_MAX];
static uint8_t mx_nkeys;
static uint8_t state[NPINS];      /* reported state */
static uint8_t lastread[NPINS];
static uint16_t changed_at[NPINS];

static uint16_t aval[16];         /* filtered x16 */
static int16_t asent[16];
static uint8_t ach = 0, adc_pass = 0, adc_busy = 0;


static char usb_buf[160];
static uint8_t usb_len;
static bool usb_to_pi;   /* inside a ">..." line: pass bytes straight through */
static char pi_buf[160];
static uint8_t pi_len;

static bool pin_reserved(uint8_t p)
{
	return p <= 1 || p == PI_TX || p == PI_RX;
}

static void apply_mode(uint8_t p)
{
	mx_dirty = true;
	uint8_t m = mode[p] & 7;
	if (pin_reserved(p))
		return;
	if (p >= 54) { /* analog-capable pin: digital input buffer on/off */
		uint8_t ch = p - 54;
		volatile uint8_t *didr = ch < 8 ? &DIDR0 : &DIDR2;
		if (m == M_ANALOG)
			*didr |= _BV(ch & 7);
		else
			*didr &= ~_BV(ch & 7);
	}
	pinMode(p, m == M_PULLUP ? INPUT_PULLUP : INPUT);
	state[p] = lastread[p] = (*pinreg[p] & pinmask[p]) ? 1 : 0;
}

static void report_digital(uint8_t p)
{
	Serial.print('D');
	Serial.print(p);
	Serial.print(' ');
	Serial.println(state[p]);
}

static void report_analog(uint8_t ch)
{
	Serial.print('A');
	Serial.print(ch);
	Serial.print(' ');
	Serial.println(asent[ch]);
}

static void report_key(uint8_t i)
{
	Serial.print('K');
	Serial.print(mx_keys[i].a);
	Serial.print(' ');
	Serial.print(mx_keys[i].b);
	Serial.print(' ');
	Serial.println(mx_keys[i].down);
}

static void dump_all()
{
	Serial.println(F("HELLO littlelx-mega 1"));
	Serial.println(F("PI 3"));
	Serial.print(F("MX"));
	for (uint8_t p = 2; p < NPINS; p++)
		if (mode[p] == M_PULLUP && !pin_reserved(p)) {
			Serial.print(' ');
			Serial.print(p);
		}
	Serial.println();
	Serial.print(F("AN"));
	for (uint8_t ch = 0; ch < 16; ch++)
		if ((mode[54 + ch] & 7) == M_ANALOG) {
			Serial.print(' ');
			Serial.print(ch);
		}
	Serial.println();
	for (uint8_t p = 0; p < NPINS; p++) {
		uint8_t m = mode[p] & 7;
		if (pin_reserved(p) || m == M_OFF)
			continue;
		if (m == M_ANALOG)
			report_analog(p - 54);
		else
			report_digital(p);
	}
	for (uint8_t i = 0; i < mx_nkeys; i++)
		if (mx_keys[i].down)
			report_key(i);
}

static uint8_t hexval(char c)
{
	return c <= '9' ? c - '0' : (c | 0x20) - 'a' + 10;
}

static bool drive_low_and_read(uint8_t p, uint8_t *follow, uint8_t *nf);

static void probe()
{
	Serial.print(F("XR"));
	for (uint8_t p = 2; p < NPINS; p++)
		if (mode[p] == M_PULLUP && !pin_reserved(p) && !(*pinreg[p] & pinmask[p])) {
			Serial.print(' ');
			Serial.print(p);
		}
	Serial.println();
	uint8_t follow[NPINS], nf;
	for (uint8_t p = 2; p < NPINS; p++) {
		if (mode[p] != M_PULLUP || pin_reserved(p) || !drive_low_and_read(p, follow, &nf) || !nf)
			continue;
		Serial.print('X');
		Serial.print(p);
		for (uint8_t i = 0; i < nf; i++) {
			Serial.print(' ');
			Serial.print(follow[i]);
		}
		Serial.println();
	}
	Serial.println(F("XE"));
}

static void usb_line(char *s)
{
	if (s[0] == 'X') {
		probe();
	} else if (s[0] == 'R') {
		char *sp;
		long a = strtol(s + 1, &sp, 10);
		long n = strtol(sp, NULL, 10);
		if (a < 0 || n < 0 || n > 32 || a + n > (long)EEPROM.length())
			return;
		Serial.print('E');
		Serial.print(a);
		Serial.print(' ');
		for (long i = 0; i < n; i++) {
			uint8_t b = EEPROM.read(a + i);
			Serial.print("0123456789abcdef"[b >> 4]);
			Serial.print("0123456789abcdef"[b & 15]);
		}
		Serial.println();
	} else if (s[0] == 'W') {
		char *sp;
		long a = strtol(s + 1, &sp, 10);
		if (*sp == ' ')
			sp++;
		long n = strlen(sp) / 2;
		if (a < 0 || n > 32 || a + n > (long)EEPROM.length())
			return;
		for (long i = 0; i < n; i++)
			EEPROM.update(a + i, (hexval(sp[2 * i]) << 4) | hexval(sp[2 * i + 1]));
		Serial.print('W');
		Serial.print(a);
		Serial.println(F(" OK"));
	} else if (s[0] == '?') {
		dump_all();
	} else if (s[0] == 'M') {
		char *sp;
		long p = strtol(s + 1, &sp, 10);
		long m = strtol(sp, NULL, 10);
		if (p >= 0 && p < NPINS) {
			if ((m & 7) == M_ANALOG && p < 54)
				m = M_PULLUP;
			mode[p] = m;
			apply_mode(p);
			if ((m & 7) == M_ANALOG)
				asent[p - 54] = -100; /* force a report */
		}
	}
}

static void pi_line(char *s)
{
	Serial.print('>');
	Serial.println(s);
}

static void poll_serial()
{
	while (Serial.available()) {
		char c = Serial.read();
		if (usb_to_pi) {
			PISER.write(c);
			if (c == '\n')
				usb_to_pi = false;
			continue;
		}
		if (c == '>' && usb_len == 0) {
			usb_to_pi = true;
			continue;
		}
		if (c == '\r')
			continue;
		if (c == '\n') {
			usb_buf[usb_len] = 0;
			if (usb_len)
				usb_line(usb_buf);
			usb_len = 0;
		} else if (usb_len < sizeof(usb_buf) - 1) {
			usb_buf[usb_len++] = c;
		}
	}
	while (PISER.available()) {
		char c = PISER.read();
		if (c == '\r')
			continue;
		if (c == '\n') {
			pi_buf[pi_len] = 0;
			if (pi_len)
				pi_line(pi_buf);
			pi_len = 0;
		} else if ((uint8_t)c >= 32 && pi_len < sizeof(pi_buf) - 1) {
			pi_buf[pi_len++] = c;
		} else if ((uint8_t)c >= 128) {
			pi_len = 0; /* garbage: Pi booting / unplugged */
		}
	}
}

static void scan_digital()
{
	uint16_t now = millis();
	for (uint8_t p = 2; p < NPINS; p++) {
		uint8_t m = mode[p];
		if ((m & 7) == M_OFF || (m & 7) == M_ANALOG || pin_reserved(p))
			continue;
		uint8_t v = (*pinreg[p] & pinmask[p]) ? 1 : 0;
		if (m & M_RAW) {
			if (v != state[p]) {
				state[p] = v;
				report_digital(p);
			}
			continue;
		}
		if (v != lastread[p]) {
			lastread[p] = v;
			changed_at[p] = now;
		} else if (v != state[p] && (uint16_t)(now - changed_at[p]) >= DEBOUNCE_MS) {
			state[p] = v;
			report_digital(p);
		}
	}
}

static void mx_note(uint8_t a, uint8_t b)
{
	if (a > b) { uint8_t t = a; a = b; b = t; }
	for (uint8_t i = 0; i < mx_nfound; i++)
		if (mx_found[i][0] == a && mx_found[i][1] == b)
			return;
	if (mx_nfound < MX_MAX) {
		mx_found[mx_nfound][0] = a;
		mx_found[mx_nfound][1] = b;
		mx_nfound++;
	}
}

/* End of a full scan: debounce what was found against what is tracked. */
static void mx_cycle_done()
{
	for (uint8_t i = 0; i < mx_nkeys; i++) {
		bool hit = false;
		for (uint8_t j = 0; j < mx_nfound; j++)
			if (mx_found[j][0] == mx_keys[i].a && mx_found[j][1] == mx_keys[i].b) {
				hit = true;
				mx_found[j][0] = 0xff; /* consumed */
			}
		if (hit) {
			mx_keys[i].missed = 0;
			if (mx_keys[i].seen < 255)
				mx_keys[i].seen++;
		} else {
			mx_keys[i].seen = 0;
			mx_keys[i].missed++;
		}
	}
	for (uint8_t j = 0; j < mx_nfound; j++) /* newly seen pairs */
		if (mx_found[j][0] != 0xff && mx_nkeys < MX_MAX) {
			mx_keys[mx_nkeys].a = mx_found[j][0];
			mx_keys[mx_nkeys].b = mx_found[j][1];
			mx_keys[mx_nkeys].seen = 1;
			mx_keys[mx_nkeys].missed = 0;
			mx_keys[mx_nkeys].down = 0;
			mx_nkeys++;
		}
	for (uint8_t i = 0; i < mx_nkeys;) {
		if (!mx_keys[i].down && mx_keys[i].seen >= MX_PRESS) {
			mx_keys[i].down = 1;
			report_key(i);
		} else if (mx_keys[i].down && mx_keys[i].missed >= MX_RELEASE) {
			mx_keys[i].down = 0;
			report_key(i);
		}
		if (!mx_keys[i].down && mx_keys[i].missed >= MX_RELEASE)
			mx_keys[i] = mx_keys[--mx_nkeys]; /* forget it */
		else
			i++;
	}
	mx_nfound = 0;
}

/* Pull pin p LOW for a moment and list the other candidate pins (high at
 * rest) that follow it. Returns false if p itself is low at rest. */
static bool drive_low_and_read(uint8_t p, uint8_t *follow, uint8_t *nf)
{
	*nf = 0;
	if (!(*pinreg[p] & pinmask[p]))
		return false;
	uint8_t rest[NPINS / 8 + 1] = { 0 };
	for (uint8_t q = 2; q < NPINS; q++)
		if (mode[q] == M_PULLUP && !pin_reserved(q) && (*pinreg[q] & pinmask[q]))
			rest[q >> 3] |= 1 << (q & 7);
	uint8_t m = pinmask[p];
	uint8_t sreg = SREG;
	cli();
	*portreg[p] &= ~m; /* pull-up off ... */
	*ddrreg[p] |= m;   /* ... then drive low */
	SREG = sreg;
	delayMicroseconds(10);
	for (uint8_t q = 2; q < NPINS; q++)
		if (q != p && (rest[q >> 3] & (1 << (q & 7))) && !(*pinreg[q] & pinmask[q]))
			follow[(*nf)++] = q;
	sreg = SREG;
	cli();
	*ddrreg[p] &= ~m;  /* back to input ... */
	*portreg[p] |= m;  /* ... with pull-up */
	SREG = sreg;
	delayMicroseconds(10); /* let the line recover before anything reads it */
	return true;
}

/* Drive one candidate pin LOW per call and see which others follow. */
static void scan_matrix()
{
	if (mx_dirty) {
		mx_n = 0;
		for (uint8_t p = 2; p < NPINS; p++)
			if (mode[p] == M_PULLUP && !pin_reserved(p))
				mx_pins[mx_n++] = p;
		mx_idx = 0;
		mx_nfound = 0;
		mx_dirty = false;
	}
	if (mx_n < 2)
		return;
	uint8_t p = mx_pins[mx_idx];
	uint8_t follow[NPINS], nf;
	if (drive_low_and_read(p, follow, &nf))
		for (uint8_t i = 0; i < nf; i++)
			if (mode[follow[i]] == M_PULLUP)
				mx_note(p, follow[i]);
	if (++mx_idx >= mx_n) {
		mx_idx = 0;
		mx_cycle_done();
	}
}

/* Non-blocking ADC, two conversions per channel:
 *   pass 0: convert the internal 0 V channel. This empties the ADC's sample
 *           capacitor, so an unconnected (floating) pin is pulled to 0 and
 *           stays quiet instead of wandering / copying the previous channel.
 *   pass 1: convert the real pin. A fader (low impedance) recharges the
 *           sample capacitor to its true value in well under the sample time.
 */
static void scan_analog()
{
	if (adc_busy) {
		if (ADCSRA & _BV(ADSC))
			return;
		uint16_t v = ADC;
		adc_busy = 0;
		if (adc_pass == 1) {
			uint16_t f = aval[ach];
			f = f - (f >> 2) + (v << 2); /* IIR, value x16 */
			aval[ach] = f;
			int16_t out = (f + 8) >> 4;
			if (out > 1023)
				out = 1023;
			if (out <= 24)  /* floating pins settle ~10-20: treat as 0 */
				out = 0;
			if (out >= 1000) /* ~2% at each end so faders hit clean 0/100% */
				out = 1023;
			int16_t d = out - asent[ach];
			if (d >= ANALOG_STEP || d <= -ANALOG_STEP ||
			    (out != asent[ach] && (out == 0 || out == 1023))) {
				asent[ach] = out;
				report_analog(ach);
			}
			adc_pass = 0;
			/* next analog channel */
			for (uint8_t i = 0; i < 16; i++) {
				ach = (ach + 1) & 15;
				if ((mode[54 + ach] & 7) == M_ANALOG)
					break;
			}
		} else {
			adc_pass = 1;
		}
	}
	if ((mode[54 + ach] & 7) != M_ANALOG) {
		ach = (ach + 1) & 15;
		adc_pass = 0;
		return;
	}
	if (adc_pass == 0) {
		ADCSRB = 0;
		ADMUX = _BV(REFS0) | 0x1f; /* MUX5:0 = 011111: 0 V (GND) */
	} else {
		ADCSRB = (ach & 8) ? _BV(MUX5) : 0;
		ADMUX = _BV(REFS0) | (ach & 7);
	}
	ADCSRA |= _BV(ADSC);
	adc_busy = 1;
}

/* Blocking conversion for the power-up test. mux 0x1f = internal 0 V. */
static uint16_t adc_once(uint8_t mux)
{
	ADCSRB = (mux != 0x1f && (mux & 8)) ? _BV(MUX5) : 0;
	ADMUX = _BV(REFS0) | (mux == 0x1f ? 0x1f : (mux & 7));
	ADCSRA |= _BV(ADSC);
	while (ADCSRA & _BV(ADSC))
		;
	return ADC;
}

/* Is analog input ch floating (nothing driving it)? A fader wiper reads the
 * same with or without the pull-up; a floating pin sits near 0 after the
 * sample capacitor is emptied, but jumps to the top with the pull-up on. */
static bool analog_floats(uint8_t ch)
{
	uint8_t p = 54 + ch;
	uint16_t lo = 0, hi = 0;
	pinMode(p, INPUT);
	for (uint8_t i = 0; i < 16; i++) {
		adc_once(0x1f);
		lo = adc_once(ch);
	}
	pinMode(p, INPUT_PULLUP);
	delay(2);
	adc_once(ch);
	hi = adc_once(ch);
	pinMode(p, INPUT);
	return lo <= 100 && hi >= 1000;
}

void setup()
{
	Serial.begin(500000);
	PISER.begin(PI_BAUD);
	for (uint8_t p = 0; p < NPINS; p++) {
		pinreg[p] = portInputRegister(digitalPinToPort(p));
		pinmask[p] = digitalPinToBitMask(p);
		portreg[p] = portOutputRegister(digitalPinToPort(p));
		ddrreg[p] = portModeRegister(digitalPinToPort(p));
		mode[p] = p >= 54 ? M_ANALOG : M_PULLUP;
		bool learned = EEPROM.read(0) == 'L' && EEPROM.read(1) == 'X';
		if (p >= 54 && !learned && analog_floats(p - 54))
			mode[p] = M_PULLUP;
		if (learned) {
			uint8_t m = EEPROM.read(2 + p); /* learned setup */
			if (m != 0xff && (m & 7) <= M_ANALOG && !((m & 7) == M_ANALOG && p < 54))
				mode[p] = m;
		}
		apply_mode(p);
	}
	for (uint8_t i = 0; i < 16; i++) {
		aval[i] = 0;
		asent[i] = -100;
	}
	Serial.println(F("HELLO littlelx-mega 1"));
	Serial.println(F("PI 3"));
	PISER.print("?\n"); /* ask the touchscreen to say hello */
}

void loop()
{
	poll_serial();
	scan_digital();
	scan_matrix();
	scan_analog();
}
