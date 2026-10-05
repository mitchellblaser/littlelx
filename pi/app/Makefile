CC ?= gcc
CFLAGS ?= -O2
CFLAGS += -Wall -Wextra -std=gnu11
LDFLAGS ?=

littlelx: littlelx.c baud.c font.h
	$(CC) $(CFLAGS) -o $@ littlelx.c baud.c $(LDFLAGS) -lm
	-$(STRIP) $@ 2>/dev/null || true

clean:
	rm -f littlelx
