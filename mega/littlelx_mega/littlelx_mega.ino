/*
 * littlelx - Arduino Mega 2560 firmware.
 *
 * The Mega is the only thing plugged into the computer. It:
 *   - reports every input pin change and analog value over USB, so the wiring
 *     does not need to be known in advance (the computer "learns" it),
 *   - relays the Raspberry Pi touchscreen, which hangs off one of the Mega's
 *     hardware serial ports (auto-detected: Serial1, Serial2 or Serial3).
 *
 * USB protocol (115200 baud, text lines):
 *   Mega -> computer
 *     HELLO littlelx-mega 1
 *     PI <n>              Pi found on Serial<n> (0 = not found yet)
 *     D<pin> <0|1>        digital pin changed (pullups: 0 = pressed)
 *     A<ch> <0..1023>     analog input A<ch> changed
 *     ><text>             line from the Pi
 *   computer -> Mega
 *     ?                   HELLO, PI and a dump of every input
 *     M<pin> <mode>       0 off, 1 input, 2 input+pullup, 3 analog (A0-A15 only)
 *                         add 8 for no debounce (rotary encoders)
 *     ><text>             send line to the Pi
 *
 * Defaults: D2-D53 = input+pullup (debounced), A0-A15 = analog.
 * Pins 0/1 (USB) and 14-19 (serial ports) are left alone until the Pi is found;
 * after that the two unused serial ports' pins are scanned as normal inputs.
 */
#include <EEPROM.h>

#define NPINS 70
#define DEBOUNCE_MS 4
#define ANALOG_STEP 4
#define PI_BAUD 500000
#define EE_MAGIC 0x4c
#define EE_ADDR 0

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

static HardwareSerial *ports[4] = { 0, &Serial1, &Serial2, &Serial3 };
static const uint8_t port_pins[4][2] = { { 0, 1 }, { 19, 18 }, { 17, 16 }, { 15, 14 } }; /* rx, tx */
static uint8_t pi_port = 0;

static char usb_buf[160];
static uint8_t usb_len;
static char pi_buf[4][160];
static uint8_t pi_len[4];

static bool pin_reserved(uint8_t p)
{
	if (p <= 1)
		return true;
	if (p >= 14 && p <= 19) {
		if (!pi_port)
			return true;
		return p == port_pins[pi_port][0] || p == port_pins[pi_port][1];
	}
	return false;
}

static void tx_enable(uint8_t n, bool on)
{
	volatile uint8_t *ucsrb = n == 1 ? &UCSR1B : n == 2 ? &UCSR2B : &UCSR3B;
	uint8_t bit = n == 1 ? TXEN1 : n == 2 ? TXEN2 : TXEN3;
	if (on)
		*ucsrb |= _BV(bit);
	else
		*ucsrb &= ~_BV(bit);
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
	Serial.print(F("PI "));
	Serial.println(pi_port);
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

static void lock_pi(uint8_t n)
{
	if (pi_port == n)
		return;
	if (pi_port)
		tx_enable(pi_port, false);
	pi_port = n;
	tx_enable(n, true);
	/* the other two ports' pins become ordinary inputs */
	for (uint8_t p = 14; p <= 19; p++)
		apply_mode(p);
	if (EEPROM.read(EE_ADDR) != EE_MAGIC || EEPROM.read(EE_ADDR + 1) != n) {
		EEPROM.write(EE_ADDR, EE_MAGIC);
		EEPROM.write(EE_ADDR + 1, n);
	}
	Serial.print(F("PI "));
	Serial.println(n);
}

static void usb_line(char *s)
{
	if (s[0] == '>') {
		if (pi_port) {
			ports[pi_port]->print(s + 1);
			ports[pi_port]->print('\n');
		}
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

static void pi_line(uint8_t n, char *s)
{
	if (n != pi_port) {
		/* only lock onto a port that really speaks our protocol */
		if (strncmp(s, "HELLO littlelx-pi", 17))
			return;
		lock_pi(n);
	}
	Serial.print('>');
	Serial.println(s);
}

static void poll_serial()
{
	while (Serial.available()) {
		char c = Serial.read();
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
	for (uint8_t n = 1; n <= 3; n++) {
		HardwareSerial *s = ports[n];
		while (s->available()) {
			char c = s->read();
			if (c == '\r')
				continue;
			if (c == '\n') {
				pi_buf[n][pi_len[n]] = 0;
				if (pi_len[n])
					pi_line(n, pi_buf[n]);
				pi_len[n] = 0;
			} else if ((uint8_t)c >= 32 && pi_len[n] < sizeof(pi_buf[n]) - 1) {
				pi_buf[n][pi_len[n]++] = c;
			} else if ((uint8_t)c >= 128) {
				pi_len[n] = 0; /* garbage: floating pin or wrong baud */
			}
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
	Serial.begin(115200);
	for (uint8_t n = 1; n <= 3; n++) {
		ports[n]->begin(PI_BAUD);
		tx_enable(n, false); /* listen only until we know where the Pi is */
	}
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
	if (EEPROM.read(EE_ADDR) == EE_MAGIC) {
		uint8_t n = EEPROM.read(EE_ADDR + 1);
		if (n >= 1 && n <= 3) {
			pi_port = n;
			tx_enable(n, true);
			for (uint8_t p = 14; p <= 19; p++)
				apply_mode(p);
		}
	}
	Serial.println(F("HELLO littlelx-mega 1"));
	Serial.print(F("PI "));
	Serial.println(pi_port);
	if (pi_port)
		ports[pi_port]->print("?\n");
}

void loop()
{
	poll_serial();
	scan_digital();
	scan_analog();
}
