/*
 * littlelx - Arduino Mega 2560 firmware.
 *
 * The Mega is the only thing plugged into the computer. It:
 *   - reports every input pin change and analog value over USB, so the wiring
 *     does not need to be known in advance (the computer "learns" it),
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
 *     ><text>             line from the Pi
 *   computer -> Mega
 *     ?                   HELLO, PI and a dump of every input
 *     M<pin> <mode>       0 off, 1 input, 2 input+pullup, 3 analog (A0-A15 only)
 *                         add 8 for no debounce (rotary encoders)
 *     ><text>             send line to the Pi (streamed through byte by byte,
 *                         so lines of any length and full-speed updates work)
 *
 * Defaults: D2-D53 = input+pullup (debounced), A0-A15 = analog.
 * Pins 0/1 (USB) and 14/15 (Pi) are not scanned; everything else is.
 */

#define NPINS 70
#define DEBOUNCE_MS 4
#define ANALOG_STEP 4
#define PI_BAUD 500000
#define PISER Serial3
#define PI_TX 14
#define PI_RX 15

enum { M_OFF = 0, M_INPUT = 1, M_PULLUP = 2, M_ANALOG = 3, M_RAW = 8 };

static uint8_t mode[NPINS];
static volatile uint8_t *pinreg[NPINS];
static uint8_t pinmask[NPINS];
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

static void dump_all()
{
	Serial.println(F("HELLO littlelx-mega 1"));
	Serial.println(F("PI 3"));
	for (uint8_t p = 0; p < NPINS; p++) {
		uint8_t m = mode[p] & 7;
		if (pin_reserved(p) || m == M_OFF)
			continue;
		if (m == M_ANALOG)
			report_analog(p - 54);
		else
			report_digital(p);
	}
}

static void usb_line(char *s)
{
	if (s[0] == '?') {
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

/* Non-blocking ADC: one conversion per call; two per channel (first one
 * after switching the mux is thrown away). */
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
			if (out <= 2)
				out = 0;
			if (out >= 1021)
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
		return;
	}
	ADMUX = _BV(REFS0) | (ach & 7);
	ADCSRB = (ach & 8) ? _BV(MUX5) : 0;
	ADCSRA |= _BV(ADSC);
	adc_busy = 1;
}

void setup()
{
	Serial.begin(500000);
	PISER.begin(PI_BAUD);
	for (uint8_t p = 0; p < NPINS; p++) {
		pinreg[p] = portInputRegister(digitalPinToPort(p));
		pinmask[p] = digitalPinToBitMask(p);
		mode[p] = p >= 54 ? M_ANALOG : M_PULLUP;
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
	scan_analog();
}
