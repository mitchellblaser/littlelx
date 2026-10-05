/* Arbitrary serial speeds (e.g. 250000) need termios2, whose header can't be
 * mixed with <termios.h> - so it lives in its own file. */
#include <asm/termbits.h>
#include <asm/ioctls.h>

int ioctl(int fd, unsigned long request, ...);

int set_baud(int fd, int baud)
{
	struct termios2 t;
	if (ioctl(fd, TCGETS2, &t))
		return -1;
	t.c_cflag &= ~CBAUD;
	t.c_cflag |= BOTHER;
	t.c_ispeed = t.c_ospeed = baud;
	return ioctl(fd, TCSETS2, &t);
}
